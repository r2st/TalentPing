"""Profiles: the several jobs one candidate would take.

Covers the CRUD surface, the invariants that keep the pipeline able to resolve a
profile at all (exactly one default, a resume to score with), and the rule that
stops the preferences form and the profile screen from drifting apart.
"""
from __future__ import annotations

import pytest

from app.models.profile import Profile
from app.services import profile_service


def _create(client, **overrides):
    payload = {"name": "DevOps Engineer", "target_roles": ["DevOps Engineer"]}
    payload.update(overrides)
    return client.post("/api/v1/profiles", json=payload)


class TestProfileCrud:
    def test_create_read_update_delete(self, auth_client, resume):
        created = _create(
            auth_client,
            resume_id=resume.id,
            location_preferences=["Berlin"],
            salary_min=90_000,
            salary_max=120_000,
            experience_level="senior",
        ).json()
        assert created["name"] == "DevOps Engineer"
        assert created["location_preferences"] == ["Berlin"]
        assert created["resume_label"] == resume.display_label

        fetched = auth_client.get(f"/api/v1/profiles/{created['id']}").json()
        assert fetched == created

        patched = auth_client.patch(
            f"/api/v1/profiles/{created['id']}",
            json={"name": "Platform Engineer", "remote_only": True},
        ).json()
        assert patched["name"] == "Platform Engineer"
        assert patched["remote_only"] is True
        # An untouched field is not reset by a partial patch.
        assert patched["salary_min"] == 90_000

        assert auth_client.delete(f"/api/v1/profiles/{created['id']}").status_code == 204
        assert auth_client.get(f"/api/v1/profiles/{created['id']}").status_code == 404

    def test_another_users_profile_is_not_reachable(
        self, auth_client, client, db_session, resume
    ):
        mine = _create(auth_client).json()

        client.post(
            "/api/v1/auth/register",
            json={"email": "other@example.com", "password": "supersecret123"},
        )
        token = client.post(
            "/api/v1/auth/login",
            data={"username": "other@example.com", "password": "supersecret123"},
        ).json()["access_token"]
        client.headers.update({"Authorization": f"Bearer {token}"})

        assert client.get(f"/api/v1/profiles/{mine['id']}").status_code == 404
        assert client.delete(f"/api/v1/profiles/{mine['id']}").status_code == 404

    def test_a_resume_belonging_to_someone_else_is_refused(self, auth_client):
        assert _create(auth_client, resume_id=9999).status_code == 404

    def test_a_backwards_salary_band_is_refused(self, auth_client):
        resp = _create(auth_client, salary_min=120_000, salary_max=90_000)
        assert resp.status_code == 422

    def test_a_patch_cannot_invert_the_band_against_a_stored_value(self, auth_client):
        """Raising only the floor past a stored ceiling is the same inversion."""
        created = _create(auth_client, salary_min=80_000, salary_max=100_000).json()
        resp = auth_client.patch(
            f"/api/v1/profiles/{created['id']}", json={"salary_min": 150_000}
        )
        assert resp.status_code == 422

    def test_an_unknown_experience_level_is_refused(self, auth_client):
        assert _create(auth_client, experience_level="wizard").status_code == 422


class TestFromResume:
    def test_a_profile_is_built_from_the_resumes_own_parse(self, auth_client, resume):
        """The file already says all of this; retyping it is the parser's job."""
        body = auth_client.post(
            "/api/v1/profiles/from-resume", json={"resume_id": resume.id}
        ).json()
        assert body["resume_id"] == resume.id
        assert "Senior Backend Engineer" in body["target_roles"]
        assert body["skills"] == resume.skills
        assert body["experience_level"] == "senior"
        assert body["location_preferences"] == ["San Francisco, CA"]

    def test_the_name_can_be_chosen(self, auth_client, resume):
        body = auth_client.post(
            "/api/v1/profiles/from-resume",
            json={"resume_id": resume.id, "name": "Backend, but in Berlin"},
        ).json()
        assert body["name"] == "Backend, but in Berlin"


class TestDefaults:
    def test_the_first_profile_becomes_the_default_unasked(self, auth_client, resume):
        """With one profile, "the default" and "the only one" are the same row."""
        assert _create(auth_client).json()["is_default"] is True

    def test_a_second_profile_does_not_steal_the_default(self, auth_client, resume):
        first = _create(auth_client, name="Backend").json()
        second = _create(auth_client, name="DevOps").json()
        assert first["is_default"] is True
        assert second["is_default"] is False

    def test_promoting_one_demotes_the_other(self, auth_client, resume):
        first = _create(auth_client, name="Backend").json()
        second = _create(auth_client, name="DevOps").json()

        auth_client.patch(f"/api/v1/profiles/{second['id']}", json={"is_default": True})
        listed = {p["id"]: p for p in auth_client.get("/api/v1/profiles").json()}
        assert listed[second["id"]]["is_default"] is True
        assert listed[first["id"]]["is_default"] is False

    def test_deleting_the_default_promotes_a_survivor(self, auth_client, resume):
        """Something must always answer "which profile?"."""
        first = _create(auth_client, name="Backend").json()
        second = _create(auth_client, name="DevOps").json()

        auth_client.delete(f"/api/v1/profiles/{first['id']}")
        listed = auth_client.get("/api/v1/profiles").json()
        assert [p["id"] for p in listed] == [second["id"]]
        assert listed[0]["is_default"] is True

    def test_switching_the_default_off_moves_the_crown_to_a_live_profile(
        self, auth_client, resume
    ):
        first = _create(auth_client, name="Backend").json()
        second = _create(auth_client, name="DevOps").json()

        auth_client.patch(f"/api/v1/profiles/{first['id']}", json={"is_default": False})
        listed = {p["id"]: p for p in auth_client.get("/api/v1/profiles").json()}
        assert listed[second["id"]]["is_default"] is True


class TestLazyFirstProfile:
    def test_listing_writes_down_the_search_the_user_already_had(
        self, auth_client, connected_gmail, resume
    ):
        """The management screen opens on what they have, not a blank slate."""
        auth_client.put(
            "/api/v1/autopilot",
            json={"target_roles": ["Backend Engineer"], "locations": ["Berlin"]},
        )
        listed = auth_client.get("/api/v1/profiles").json()
        assert len(listed) == 1
        assert listed[0]["target_roles"] == ["Backend Engineer"]
        assert listed[0]["location_preferences"] == ["Berlin"]
        assert listed[0]["resume_id"] == resume.id

    def test_a_user_with_nothing_to_describe_gets_nothing(self, auth_client):
        """An empty profile would claim an intent they never expressed."""
        assert auth_client.get("/api/v1/profiles").json() == []

    def test_listing_twice_does_not_create_two(self, auth_client, resume):
        auth_client.get("/api/v1/profiles")
        assert len(auth_client.get("/api/v1/profiles").json()) == 1


class TestPreferencesStayInSync:
    """One intent must not be editable from two screens that disagree."""

    def test_editing_preferences_writes_through_to_a_single_profile(
        self, auth_client, connected_gmail, resume, db_session
    ):
        auth_client.put("/api/v1/autopilot", json={"target_roles": ["Backend Engineer"]})
        auth_client.put(
            "/api/v1/autopilot",
            json={"target_roles": ["Data Engineer"], "locations": ["Amsterdam"]},
        )
        profile = db_session.query(Profile).one()
        assert profile.target_roles == ["Data Engineer"]
        assert profile.location_preferences == ["Amsterdam"]

    def test_a_second_profile_makes_the_profiles_authoritative(
        self, auth_client, connected_gmail, resume, db_session
    ):
        """With two, "my roles" is ambiguous — so the form stops overwriting."""
        auth_client.put("/api/v1/autopilot", json={"target_roles": ["Backend Engineer"]})
        _create(auth_client, name="DevOps", target_roles=["DevOps Engineer"])

        auth_client.put("/api/v1/autopilot", json={"target_roles": ["Data Engineer"]})
        roles = sorted(
            tuple(p.target_roles) for p in db_session.query(Profile).all()
        )
        assert roles == [("Backend Engineer",), ("DevOps Engineer",)]


class TestScoringTargets:
    def test_only_active_profiles_are_scored_against(
        self, db_session, current_user, resume
    ):
        db_session.add_all(
            [
                Profile(user_id=current_user.id, resume_id=resume.id, name="On",
                        target_roles=["Backend Engineer"], is_active=True,
                        is_default=True),
                Profile(user_id=current_user.id, resume_id=resume.id, name="Off",
                        target_roles=["DevOps Engineer"], is_active=False),
            ]
        )
        db_session.commit()

        targets = profile_service.active_targets(db_session, current_user)
        assert [t.label for t in targets] == ["On"]

    def test_a_profile_whose_resume_was_deleted_falls_back_to_the_default(
        self, db_session, current_user, resume
    ):
        """Losing a file shouldn't cost the candidate an intent they still hold."""
        db_session.add(
            Profile(user_id=current_user.id, resume_id=None, name="DevOps",
                    target_roles=["DevOps Engineer"], is_active=True, is_default=True)
        )
        db_session.commit()

        targets = profile_service.active_targets(db_session, current_user)
        assert len(targets) == 1
        assert targets[0].resume.id == resume.id

    def test_a_user_with_no_profiles_falls_back_to_their_preferences(
        self, db_session, current_user, resume
    ):
        from app.models.autopilot import AutopilotPreference

        db_session.add(
            AutopilotPreference(
                user_id=current_user.id, target_roles=["Backend Engineer"],
                locations=["Berlin"]
            )
        )
        db_session.commit()
        db_session.refresh(current_user)

        targets = profile_service.active_targets(db_session, current_user)
        assert len(targets) == 1
        assert targets[0].profile is None
        assert targets[0].targeting.locations == ["Berlin"]

    def test_a_user_with_no_resume_has_nothing_to_score_with(
        self, db_session, current_user
    ):
        assert profile_service.active_targets(db_session, current_user) == []


@pytest.fixture()
def two_profiles(db_session, current_user, resume):
    """A backend profile in Berlin and a DevOps profile that's remote-only."""
    backend = Profile(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Backend Engineer",
        target_roles=["Backend Engineer"],
        location_preferences=["Berlin"],
        is_active=True,
        is_default=True,
    )
    devops = Profile(
        user_id=current_user.id,
        resume_id=resume.id,
        name="DevOps Engineer",
        target_roles=["DevOps Engineer"],
        remote_only=True,
        is_active=True,
    )
    db_session.add_all([backend, devops])
    db_session.commit()
    return backend, devops


class TestBestProfileWins:
    def test_each_job_is_scored_against_every_profile_and_the_best_one_wins(
        self, db_session, current_user, two_profiles
    ):
        from app.services.jd_parser import parse_job

        targets = profile_service.active_targets(db_session, current_user)

        devops_job = parse_job(
            "DevOps Engineer\nWe run Kubernetes and Terraform. Remote.",
            page_title="DevOps Engineer",
            use_llm=False,
        )
        devops_job.title = "DevOps Engineer"
        devops_job.remote = True
        match = profile_service.score_against_targets(targets, devops_job)
        assert match.target.label == "DevOps Engineer"

        backend_job = parse_job(
            "Backend Engineer\nPython and FastAPI services.",
            page_title="Backend Engineer",
            use_llm=False,
        )
        backend_job.title = "Backend Engineer"
        backend_job.location = "Berlin"
        match = profile_service.score_against_targets(targets, backend_job)
        assert match.target.label == "Backend Engineer"

    def test_every_profiles_verdict_is_kept_not_just_the_winners(
        self, db_session, current_user, two_profiles
    ):
        """The runner-up answers "would my other profile have liked this?"."""
        from app.services.jd_parser import parse_job

        targets = profile_service.active_targets(db_session, current_user)
        job = parse_job("Backend Engineer", page_title="Backend Engineer", use_llm=False)
        job.title = "Backend Engineer"

        match = profile_service.score_against_targets(targets, job)
        assert set(match.all_scores) == {p.id for p in two_profiles}

    def test_a_tie_goes_to_the_default_profile(
        self, db_session, current_user, resume
    ):
        """Two profiles sharing a resume tie constantly; the user's pick breaks it."""
        from app.services.jd_parser import parse_job

        db_session.add_all(
            [
                Profile(user_id=current_user.id, resume_id=resume.id, name="First",
                        target_roles=["Backend Engineer"], is_active=True),
                Profile(user_id=current_user.id, resume_id=resume.id, name="Second",
                        target_roles=["Backend Engineer"], is_active=True,
                        is_default=True),
            ]
        )
        db_session.commit()

        targets = profile_service.active_targets(db_session, current_user)
        job = parse_job("Backend Engineer", page_title="Backend Engineer", use_llm=False)
        job.title = "Backend Engineer"
        assert profile_service.score_against_targets(targets, job).target.label == "Second"
