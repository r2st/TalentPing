"""Career-page form auto-apply — the pure field-matching core and the endpoint.

The browser driving needs Playwright and a real page, so it isn't exercised here;
:func:`plan_fills` is the decision-making core and *is* pure, so that's what these
tests pin down. The endpoint is covered for its graceful-degradation contract.
"""
from __future__ import annotations

from app.services.career_apply_service import (
    ApplicantProfile,
    FormField,
    autofill_application,
    is_available,
    plan_fills,
)


def _profile() -> ApplicantProfile:
    return ApplicantProfile(
        full_name="Jordan Candidate",
        first_name="Jordan",
        last_name="Candidate",
        email="jordan@example.com",
        phone="+1 415 555 0142",
        location="San Francisco, CA",
        linkedin_url="https://linkedin.com/in/jordanc",
        website="https://jordan.dev",
    )


class TestPlanFills:
    def test_matches_the_common_fields(self):
        fields = [
            FormField(name="first_name", label="First name"),
            FormField(name="last_name", label="Last name"),
            FormField(name="email", field_type="email", label="Email"),
            FormField(name="phone", field_type="tel", label="Phone number"),
            FormField(name="linkedin", label="LinkedIn URL"),
        ]
        plan = {f.attr: f.value for f in plan_fills(fields, _profile())}
        assert plan["first_name"] == "Jordan"
        assert plan["last_name"] == "Candidate"
        assert plan["email"] == "jordan@example.com"
        assert plan["phone"] == "+1 415 555 0142"
        assert plan["linkedin_url"] == "https://linkedin.com/in/jordanc"

    def test_first_name_wins_over_bare_name(self):
        # "First name" must not be captured by the generic "name" rule.
        fields = [FormField(name="first_name", label="First Name")]
        plan = plan_fills(fields, _profile())
        assert len(plan) == 1 and plan[0].attr == "first_name"

    def test_a_lone_name_field_gets_the_full_name(self):
        plan = plan_fills([FormField(name="name", label="Your name")], _profile())
        assert plan[0].attr == "full_name" and plan[0].value == "Jordan Candidate"

    def test_sensitive_fields_are_never_filled(self):
        fields = [
            FormField(name="salary", label="Desired salary"),
            FormField(name="ssn", label="Social Security Number"),
            FormField(name="sponsorship", label="Do you require visa sponsorship?"),
            FormField(name="cover", field_type="textarea", label="Cover letter"),
        ]
        assert plan_fills(fields, _profile()) == []

    def test_file_and_select_inputs_are_left_for_dedicated_handling(self):
        fields = [
            FormField(name="resume", field_type="file", label="Upload resume"),
            FormField(name="country", tag="select", label="Country"),
        ]
        assert plan_fills(fields, _profile()) == []

    def test_each_value_is_used_at_most_once(self):
        # Two name-ish fields shouldn't both get the full name.
        fields = [
            FormField(name="name", label="Name"),
            FormField(name="applicant_name", label="Applicant name"),
        ]
        plan = plan_fills(fields, _profile())
        assert len(plan) == 1

    def test_missing_profile_values_are_skipped(self):
        thin = ApplicantProfile(full_name="Jordan", email=None)
        fields = [FormField(name="email", field_type="email", label="Email")]
        assert plan_fills(fields, thin) == []


class TestProfileFromResume:
    def test_splits_name_and_picks_linkedin(self, resume):
        resume.full_name = "Jordan Candidate"
        resume.links = ["https://linkedin.com/in/jordanc", "https://jordan.dev"]
        profile = ApplicantProfile.from_resume(resume)
        assert profile.first_name == "Jordan"
        assert profile.last_name == "Candidate"
        assert profile.linkedin_url == "https://linkedin.com/in/jordanc"
        assert profile.website == "https://jordan.dev"


class TestGracefulDegradation:
    def test_autofill_reports_unsupported_when_playwright_is_absent(self):
        # No browser binary is installed in the test env — the contract is that
        # the call returns cleanly rather than raising.
        result = autofill_application("https://example.com/apply", _profile())
        if not is_available():
            assert result.status == "unsupported"
        else:  # pragma: no cover - only when a browser is installed
            assert result.status in {"filled", "no_form", "failed", "submitted"}


class TestApplyFormEndpoint:
    def test_reports_status_and_records_it_on_the_posting(
        self, auth_client, resume, db_session, current_user
    ):
        from app.models.job import JobPosting, JobStatus, job_fingerprint

        posting = JobPosting(
            user_id=current_user.id,
            title="Backend Engineer",
            company="Acme",
            url="https://acme.com/apply",
            fingerprint=job_fingerprint("Backend Engineer", "Acme", None),
            status=JobStatus.NEW,
        )
        db_session.add(posting)
        db_session.commit()
        db_session.refresh(posting)

        resp = auth_client.post(f"/api/v1/jobs/{posting.id}/apply-form")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        db_session.refresh(posting)
        assert posting.form_apply_status == body["status"]

    def test_a_posting_without_a_url_is_rejected(
        self, auth_client, resume, db_session, current_user
    ):
        from app.models.job import JobPosting, JobStatus, job_fingerprint

        posting = JobPosting(
            user_id=current_user.id,
            title="Backend Engineer",
            company="Acme",
            fingerprint=job_fingerprint("Backend Engineer", "Acme", "x"),
            status=JobStatus.NEW,
        )
        db_session.add(posting)
        db_session.commit()
        db_session.refresh(posting)
        assert auth_client.post(f"/api/v1/jobs/{posting.id}/apply-form").status_code == 422
