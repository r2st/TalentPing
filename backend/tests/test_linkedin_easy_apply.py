"""The Easy Apply adapter, driven over a fake browser.

Easy Apply acts on the candidate's own LinkedIn account, and the two things it
can get wrong are not symmetric: failing to apply costs one application, while
submitting something half-filled — or quietly following an employer the
candidate did not choose to follow — happens on their real profile, under their
real name, and cannot be taken back.

So what is pinned here is the refusals: no Easy Apply button means *say so*
rather than apply somewhere else, an unanswerable required question stops the
run, and ``submit=False`` never presses the button.
"""
from __future__ import annotations

import pytest

from app.models.form_apply import ATSPlatform
from app.services import linkedin_service as svc
from app.services.ats_adapters import ApplyContext
from app.services.browser_runner import StepLog
from app.services.career_apply_service import ApplicantProfile
from app.services.form_answers import AnswerBank
from tests.fake_browser import FakePage, Step, control

JOB_URL = "https://www.linkedin.com/jobs/view/1234567890/"

EASY_APPLY = "button.jobs-apply-button"
NEXT = "button[aria-label='Continue to next step']"
SUBMIT = "button[aria-label='Submit application']"
FOLLOW = "input#follow-company-checkbox"


@pytest.fixture(autouse=True)
def _no_login(monkeypatch):
    """Signing in is tested in test_linkedin.py; this file is about the modal."""
    monkeypatch.setattr(svc, "ensure_login", lambda page, account, password: None)


def contact_controls() -> list[dict]:
    return [
        control(name="name", label="Full name"),
        control(name="email", label="Email", field_type="email"),
        control(name="phone", label="Phone", field_type="tel"),
    ]


def _ctx(*, submit: bool = False) -> ApplyContext:
    return ApplyContext(
        url=JOB_URL,
        applicant=ApplicantProfile(
            full_name="Jordan Candidate",
            first_name="Jordan",
            last_name="Candidate",
            email="jordan@example.com",
            phone="+1 415 555 0142",
        ),
        bank=AnswerBank(
            work_authorized=True,
            requires_sponsorship=False,
            years_experience=8,
            llm_enabled=False,
        ),
        submit=submit,
        log=StepLog("test", enabled=False),
    )


def _adapter() -> svc.EasyApplyAdapter:
    class _Account:
        email = "candidate@example.com"
        status = "active"

    return svc.EasyApplyAdapter(_Account(), "hunter2")


def _modal(**step_kwargs) -> FakePage:
    """A job page whose Easy Apply button opens a one-step modal."""
    return FakePage(
        [
            Step(clickable=(EASY_APPLY,), advances_on=(EASY_APPLY,)),
            Step(**step_kwargs),
        ],
        url=JOB_URL,
    )


class TestPlatform:
    def test_the_adapter_declares_linkedin(self):
        assert _adapter().platform is ATSPlatform.LINKEDIN


class TestNoEasyApply:
    def test_a_job_without_the_button_is_not_applied_to_elsewhere(self):
        """Half-applying on the employer's own site is worse than not applying."""
        page = FakePage([Step(controls=contact_controls())], url=JOB_URL)

        result = _adapter().run(page, _ctx(submit=True))

        assert result.status == "no_form"
        assert "no Easy Apply" in (result.note or "")
        assert SUBMIT not in page.clicked


class TestFillAndSubmit:
    def test_fills_the_modal_and_stops_when_submit_was_not_asked_for(self):
        page = _modal(controls=contact_controls(), visible=(SUBMIT,))

        result = _adapter().run(page, _ctx())

        assert result.status == "filled"
        assert page.filled['[name="email"]'] == "jordan@example.com"
        assert SUBMIT not in page.clicked

    def test_submits_when_asked(self):
        page = _modal(controls=contact_controls(), visible=(SUBMIT,))

        result = _adapter().run(page, _ctx(submit=True))

        assert result.status == "submitted"
        assert SUBMIT in page.clicked

    def test_walks_a_multi_step_modal(self):
        page = FakePage(
            [
                Step(clickable=(EASY_APPLY,), advances_on=(EASY_APPLY,)),
                Step(controls=contact_controls(), clickable=(NEXT,), advances_on=(NEXT,)),
                Step(
                    controls=[
                        control(
                            name="sponsorship",
                            label="Will you require visa sponsorship?",
                            tag="select",
                            options=("Yes", "No"),
                        )
                    ],
                    visible=(SUBMIT,),
                ),
            ],
            url=JOB_URL,
        )

        result = _adapter().run(page, _ctx(submit=True))

        assert result.status == "submitted"
        assert page.selected['[name="sponsorship"]'] == "No"

    def test_an_unanswerable_required_question_stops_the_run(self):
        page = _modal(
            controls=[
                *contact_controls(),
                control(
                    name="clearance",
                    label="What is your active security clearance number?",
                    required=True,
                ),
            ],
            visible=(SUBMIT,),
        )

        result = _adapter().run(page, _ctx(submit=True))

        assert result.status == "needs_input"
        assert SUBMIT not in page.clicked

    def test_a_modal_that_never_ends_stops_at_the_step_ceiling(self):
        """A wizard that keeps offering Next must not loop forever."""
        forever = [Step(clickable=(EASY_APPLY,), advances_on=(EASY_APPLY,))] + [
            Step(controls=contact_controls(), clickable=(NEXT,))
            for _ in range(svc.EasyApplyAdapter.MAX_STEPS + 4)
        ]
        page = FakePage(forever, url=JOB_URL)

        result = _adapter().run(page, _ctx(submit=True))

        assert result.status in {"filled", "needs_input", "no_form"}
        assert page.clicked.count(NEXT) <= svc.EasyApplyAdapter.MAX_STEPS


class TestFollowCheckbox:
    """Applying is not following, and a toggle read wrong does the opposite."""

    def test_a_ticked_follow_box_is_cleared(self):
        page = _modal(
            controls=contact_controls(), visible=(SUBMIT, FOLLOW), ticked=(FOLLOW,)
        )

        _adapter().run(page, _ctx(submit=True))

        assert FOLLOW in page.clicked
        assert page.is_checked(FOLLOW) is False

    def test_an_already_clear_box_is_left_alone(self):
        """Clicking it would turn following *on* — the opposite of the intent."""
        page = _modal(controls=contact_controls(), visible=(SUBMIT, FOLLOW))

        _adapter().run(page, _ctx(submit=True))

        assert FOLLOW not in page.clicked
        assert page.is_checked(FOLLOW) is False

    def test_a_box_that_is_not_there_at_all_is_not_an_error(self):
        page = _modal(controls=contact_controls(), visible=(SUBMIT,))

        assert _adapter().run(page, _ctx(submit=True)).status == "submitted"


# --------------------------------------------------------------------------- #
# The endpoint                                                                 #
# --------------------------------------------------------------------------- #


def _posting(db_session, user, url: str):
    from app.models.job import JobPosting

    row = JobPosting(
        user_id=user.id,
        title="Senior Backend Engineer",
        company="Northwind",
        url=url,
        fingerprint=f"fp-{abs(hash(url))}",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(
        svc.settings, "linkedin_easy_apply_enabled", True, raising=False
    )


@pytest.fixture()
def connected(db_session, current_user):
    return svc.store_credentials(
        db_session, current_user, email="candidate@example.com", password="hunter2"
    )


class TestEasyApplyEndpoint:
    def _url(self, posting) -> str:
        return f"/api/v1/linkedin/easy-apply/{posting.id}"

    def test_switched_off_on_this_server_is_a_409(
        self, auth_client, db_session, current_user, monkeypatch
    ):
        monkeypatch.setattr(
            svc.settings, "linkedin_easy_apply_enabled", False, raising=False
        )
        posting = _posting(db_session, current_user, JOB_URL)

        resp = auth_client.post(self._url(posting))
        assert resp.status_code == 409
        assert "switched off" in resp.json()["detail"]

    def test_an_unknown_job_is_a_404(self, auth_client, enabled):
        assert auth_client.post("/api/v1/linkedin/easy-apply/9999").status_code == 404

    def test_no_connected_account_is_a_409(
        self, auth_client, db_session, current_user, enabled
    ):
        posting = _posting(db_session, current_user, JOB_URL)

        resp = auth_client.post(self._url(posting))
        assert resp.status_code == 409
        assert "Connect your LinkedIn account" in resp.json()["detail"]

    def test_a_non_linkedin_posting_is_refused(
        self, auth_client, db_session, current_user, enabled, connected
    ):
        posting = _posting(
            db_session, current_user, "https://boards.greenhouse.io/acme/jobs/1"
        )

        resp = auth_client.post(self._url(posting))
        assert resp.status_code == 422
        assert "Form apply" in resp.json()["detail"]

    def test_a_refused_posting_leaves_no_queued_run_behind(
        self, auth_client, db_session, current_user, enabled, connected
    ):
        """The refusal used to land *after* the run row was committed, so every
        wrong-platform click left a QUEUED application nothing would ever run."""
        from app.models.form_apply import FormApplication

        posting = _posting(
            db_session, current_user, "https://boards.greenhouse.io/acme/jobs/2"
        )
        auth_client.post(self._url(posting))

        assert db_session.query(FormApplication).count() == 0

    def test_a_used_up_daily_budget_is_a_429(
        self, auth_client, db_session, current_user, enabled, connected, monkeypatch
    ):
        from datetime import UTC, datetime

        monkeypatch.setattr(
            svc.settings, "linkedin_easy_apply_daily_limit", 1, raising=False
        )
        connected.daily_apply_count = 1
        connected.daily_count_reset_at = datetime.now(UTC)
        db_session.commit()
        posting = _posting(db_session, current_user, JOB_URL)

        resp = auth_client.post(self._url(posting), json={"submit": True})
        assert resp.status_code == 429
        assert "daily cap" in resp.json()["detail"]

    def test_another_users_posting_is_not_reachable(
        self, auth_client, db_session, enabled, connected
    ):
        from app.models.user import User

        other = User(email="someone@else.test", hashed_password="x")
        db_session.add(other)
        db_session.commit()
        posting = _posting(db_session, other, JOB_URL)

        assert auth_client.post(self._url(posting)).status_code == 404
