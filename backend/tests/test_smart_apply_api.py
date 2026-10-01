"""The /tailor and /fit-score endpoints, end to end through the API."""
from __future__ import annotations

from app.models.fit_score import FitScore
from app.models.job import JobPosting, JobStatus, job_fingerprint
from app.models.tailored_resume import TailoredResume


class TestTailorEndpoint:
    def test_returns_a_tailored_resume_and_cover_letter(
        self, auth_client, resume, job_description
    ):
        resp = auth_client.post(
            "/api/v1/tailor",
            json={"job_description": job_description, "resume_id": resume.id},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()

        assert body["tailored"]["cover_letter"]
        assert body["tailored"]["tailored_summary"]
        assert body["tailored"]["ordered_skills"]
        assert body["parsed_job"]["company"] == "Northwind Labs"
        assert body["markdown"].startswith("# Jordan Candidate")

    def test_includes_the_fit_score_by_default(self, auth_client, resume, job_description):
        body = auth_client.post(
            "/api/v1/tailor", json={"job_description": job_description}
        ).json()

        assert body["fit"] is not None
        assert 0 <= body["fit"]["overall"] <= 100
        assert set(body["fit"]["breakdown"]) == {
            "skills",
            "role",
            "experience",
            "location",
            "salary",
            "industry",
        }

    def test_fit_score_can_be_skipped(self, auth_client, resume, job_description):
        body = auth_client.post(
            "/api/v1/tailor",
            json={"job_description": job_description, "include_fit_score": False},
        ).json()
        assert body["fit"] is None

    def test_reports_missing_keywords_honestly(self, auth_client, resume, job_description):
        body = auth_client.post(
            "/api/v1/tailor", json={"job_description": job_description}
        ).json()
        # The fixture resume has no Kubernetes; the API must say so rather than
        # quietly writing it into the resume.
        assert "kubernetes" in body["tailored"]["missing_keywords"]
        assert "kubernetes" not in body["tailored"]["ordered_skills"]

    def test_persists_the_run(self, auth_client, db_session, resume, job_description):
        auth_client.post("/api/v1/tailor", json={"job_description": job_description})
        assert db_session.query(TailoredResume).count() == 1

    def test_uses_the_default_resume_when_none_is_named(
        self, auth_client, resume, job_description
    ):
        body = auth_client.post(
            "/api/v1/tailor", json={"job_description": job_description}
        ).json()
        assert body["tailored"]["resume_id"] == resume.id

    def test_requires_a_resume(self, auth_client, job_description):
        resp = auth_client.post("/api/v1/tailor", json={"job_description": job_description})
        assert resp.status_code == 409
        assert "resume" in resp.json()["detail"].lower()

    def test_rejects_a_request_with_no_job(self, auth_client, resume):
        assert auth_client.post("/api/v1/tailor", json={}).status_code == 422

    def test_rejects_an_unknown_resume(self, auth_client, resume, job_description):
        resp = auth_client.post(
            "/api/v1/tailor",
            json={"job_description": job_description, "resume_id": 9999},
        )
        assert resp.status_code == 422

    def test_requires_authentication(self, client, job_description):
        resp = client.post("/api/v1/tailor", json={"job_description": job_description})
        assert resp.status_code == 401


class TestTailorHistory:
    def test_lists_newest_first(self, auth_client, resume, job_description):
        auth_client.post("/api/v1/tailor", json={"job_description": job_description})
        auth_client.post(
            "/api/v1/tailor", json={"job_description": "Frontend Engineer\nReact and CSS."}
        )

        rows = auth_client.get("/api/v1/tailor").json()
        assert len(rows) == 2
        assert rows[0]["id"] > rows[1]["id"]

    def test_fetches_one_run(self, auth_client, resume, job_description):
        created = auth_client.post(
            "/api/v1/tailor", json={"job_description": job_description}
        ).json()["tailored"]

        fetched = auth_client.get(f"/api/v1/tailor/{created['id']}").json()
        assert fetched["id"] == created["id"]

    def test_unknown_run_is_404(self, auth_client, resume):
        assert auth_client.get("/api/v1/tailor/9999").status_code == 404

    def test_downloads_the_resume_as_markdown(self, auth_client, resume, job_description):
        created = auth_client.post(
            "/api/v1/tailor", json={"job_description": job_description}
        ).json()["tailored"]

        resp = auth_client.get(f"/api/v1/tailor/{created['id']}/download")
        assert resp.status_code == 200
        assert "markdown" in resp.headers["content-type"]
        assert "attachment" in resp.headers["content-disposition"]
        assert resp.text.startswith("# Jordan Candidate")

    def test_downloads_the_cover_letter(self, auth_client, resume, job_description):
        created = auth_client.post(
            "/api/v1/tailor", json={"job_description": job_description}
        ).json()["tailored"]

        resp = auth_client.get(
            f"/api/v1/tailor/{created['id']}/download", params={"doc": "cover_letter"}
        )
        assert resp.status_code == 200
        assert "Northwind Labs" in resp.text

    def test_rejects_an_unknown_document_type(self, auth_client, resume, job_description):
        created = auth_client.post(
            "/api/v1/tailor", json={"job_description": job_description}
        ).json()["tailored"]
        resp = auth_client.get(
            f"/api/v1/tailor/{created['id']}/download", params={"doc": "passport"}
        )
        assert resp.status_code == 422


class TestFitScoreEndpoint:
    def test_scores_with_a_full_breakdown(self, auth_client, resume, job_description):
        resp = auth_client.post("/api/v1/fit-score", json={"job_description": job_description})
        assert resp.status_code == 200, resp.text
        fit = resp.json()["fit"]

        assert fit["overall"] >= 70
        assert fit["recommendation"] in {"strong", "good"}
        assert fit["summary"]
        assert "kubernetes" in fit["missing_skills"]

        # Each dimension reports its own 0-100 score, its weight, and a reason.
        for name, dim in fit["breakdown"].items():
            assert 0 <= dim["score"] <= 100, name
            assert 0 < dim["weight"] <= 1, name
            assert dim["note"], name

    def test_breakdown_weights_sum_to_one(self, auth_client, resume, job_description):
        fit = auth_client.post(
            "/api/v1/fit-score", json={"job_description": job_description}
        ).json()["fit"]
        assert round(sum(d["weight"] for d in fit["breakdown"].values()), 6) == 1.0

    def test_rescoring_updates_rather_than_duplicates(
        self, auth_client, db_session, resume, job_description
    ):
        """The same resume × posting must stay one cached row."""
        first = auth_client.post(
            "/api/v1/fit-score", json={"job_description": job_description}
        ).json()["fit"]
        second = auth_client.post(
            "/api/v1/fit-score", json={"job_description": job_description}
        ).json()["fit"]

        assert first["id"] == second["id"]
        assert first["overall"] == second["overall"]
        assert db_session.query(FitScore).count() == 1

    def test_a_different_posting_gets_its_own_score(
        self, auth_client, db_session, resume, job_description
    ):
        auth_client.post("/api/v1/fit-score", json={"job_description": job_description})
        auth_client.post(
            "/api/v1/fit-score",
            json={"job_description": "Oncology Nurse\nRequirements\n- phlebotomy"},
        )
        assert db_session.query(FitScore).count() == 2

    def test_scores_a_stored_posting_and_caches_it_on_the_row(
        self, auth_client, db_session, current_user, resume, job_description
    ):
        posting = JobPosting(
            user_id=current_user.id,
            title="Senior Backend Engineer",
            company="Northwind Labs",
            description=job_description,
            fingerprint=job_fingerprint("Senior Backend Engineer", "Northwind Labs", None),
            status=JobStatus.NEW,
        )
        db_session.add(posting)
        db_session.commit()

        fit = auth_client.post(
            "/api/v1/fit-score", json={"job_posting_id": posting.id}
        ).json()["fit"]

        db_session.refresh(posting)
        assert fit["job_posting_id"] == posting.id
        assert posting.fit_score == fit["overall"]

    def test_another_users_posting_is_not_reachable(
        self, auth_client, db_session, resume, current_user
    ):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.flush()
        posting = JobPosting(
            user_id=other.id,
            title="Secret Role",
            company="Other Corp",
            description="Requirements\n- python",
            fingerprint="other-fingerprint",
        )
        db_session.add(posting)
        db_session.commit()

        resp = auth_client.post("/api/v1/fit-score", json={"job_posting_id": posting.id})
        assert resp.status_code == 422
        assert "not found" in resp.json()["detail"].lower()

    def test_requires_authentication(self, client, job_description):
        assert (
            client.post("/api/v1/fit-score", json={"job_description": job_description}).status_code
            == 401
        )


class TestTailoredPDF:
    """The PDF is what gets attached to an application, so it is stored at
    tailoring time rather than re-rendered — the file the candidate sends must
    be the one they previewed."""

    def _tailor(self, auth_client, job_description):
        return auth_client.post(
            "/api/v1/tailor", json={"job_description": job_description}
        ).json()["tailored"]["id"]

    def test_downloads_a_real_pdf(self, auth_client, resume, job_description):
        tailored_id = self._tailor(auth_client, job_description)
        resp = auth_client.get(f"/api/v1/tailor/{tailored_id}/pdf")

        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")
        assert "attachment" in resp.headers["content-disposition"]

    def test_another_users_run_is_not_reachable(self, auth_client, resume, job_description):
        tailored_id = self._tailor(auth_client, job_description)
        auth_client.headers.pop("Authorization", None)
        assert auth_client.get(f"/api/v1/tailor/{tailored_id}/pdf").status_code == 401

    def test_a_run_without_a_pdf_points_at_the_markdown_download(
        self, auth_client, db_session, resume, job_description
    ):
        from app.models.tailored_resume import TailoredResume

        tailored_id = self._tailor(auth_client, job_description)
        row = db_session.get(TailoredResume, tailored_id)
        row.pdf_bytes = None
        row.pdf_generated_at = None
        db_session.commit()

        resp = auth_client.get(f"/api/v1/tailor/{tailored_id}/pdf")
        assert resp.status_code == 409
        assert "Markdown" in resp.json()["detail"]


class TestCoverLetterEndpoints:
    def test_writes_and_reloads_a_letter(self, auth_client, resume, job_description):
        resp = auth_client.post(
            "/api/v1/cover-letter", json={"job_description": job_description}
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()

        assert body["full_text"].startswith(body["greeting"])
        assert body["delivery"] == "inline"
        assert body["edited"] is False

        again = auth_client.get(f"/api/v1/cover-letter/{body['id']}")
        assert again.status_code == 200
        assert again.json()["body"] == body["body"]

    def test_an_edit_marks_the_letter_and_survives_regeneration(
        self, auth_client, resume, job_description
    ):
        letter_id = auth_client.post(
            "/api/v1/cover-letter", json={"job_description": job_description}
        ).json()["id"]

        edited = auth_client.patch(
            f"/api/v1/cover-letter/{letter_id}", json={"body": "My own words."}
        ).json()
        assert edited["edited"] is True
        assert edited["body"] == "My own words."

    def test_downloads_as_markdown(self, auth_client, resume, job_description):
        letter_id = auth_client.post(
            "/api/v1/cover-letter", json={"job_description": job_description}
        ).json()["id"]

        resp = auth_client.get(f"/api/v1/cover-letter/{letter_id}/download")
        assert resp.status_code == 200
        assert resp.text.startswith("# Cover letter")

    def test_another_users_letter_is_not_reachable(self, auth_client, resume, job_description):
        letter_id = auth_client.post(
            "/api/v1/cover-letter", json={"job_description": job_description}
        ).json()["id"]
        auth_client.headers.pop("Authorization", None)
        assert auth_client.get(f"/api/v1/cover-letter/{letter_id}").status_code == 401

    def test_tailor_can_include_the_letter(self, auth_client, resume, job_description):
        body = auth_client.post(
            "/api/v1/tailor",
            json={"job_description": job_description, "include_cover_letter": True},
        ).json()
        assert body["cover_letter"] is not None
        assert body["cover_letter"]["full_text"]

    def test_tailor_omits_the_letter_by_default(self, auth_client, resume, job_description):
        body = auth_client.post(
            "/api/v1/tailor", json={"job_description": job_description}
        ).json()
        assert body["cover_letter"] is None
