"""The form-apply agent: adapters, retries, the service, and the endpoints.

The browser is faked (see :mod:`tests.fake_browser`) rather than skipped. The
adapters only ever speak to a page through ``Form``'s small vocabulary, so a
scripted fake exercises the real Workday state machine, the real Greenhouse
fill, and the real decision to stop rather than submit — with no Chromium
anywhere.

What these pin down, in order of how much it would cost to get wrong:

* a run stops instead of submitting when a required question is unanswered;
* a posting already applied to is never applied to twice;
* submit is opt-in, per run;
* the retry window closes before the irreversible click.
"""
from __future__ import annotations

import pytest

from app.models.form_apply import (
    ATSPlatform,
    FormApplication,
    FormApplyProfile,
    FormApplyStatus,
)
from app.models.job import JobPosting, JobStatus, job_fingerprint
from app.services import browser_runner, form_apply_service
from app.services.ats_adapters import (
    ApplyContext,
    GenericAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    WorkdayAdapter,
    adapter_for,
    goto,
)
from app.services.browser_runner import (
    BrowserUnavailable,
    Form,
    StepLog,
    TransientBrowserError,
    with_retries,
)
from app.services.career_apply_service import ApplicantProfile
from app.services.form_answers import AnswerBank
from app.services.form_apply_service import FormApplyRefused
from tests.fake_browser import FakePage, Step, control, selector_for

GREENHOUSE_URL = "https://boards.greenhouse.io/acme/jobs/4567"
WORKDAY_URL = "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Engineer_R-1"
LEVER_URL = "https://jobs.lever.co/acme/1a2b3c4d"

SUBMIT = 'button[type="submit"]'
NEXT = "button:has-text('Continue')"


def contact_controls() -> list[dict]:
    return [
        control(name="first_name", label="First name"),
        control(name="last_name", label="Last name"),
        control(name="email", label="Email", field_type="email"),
        control(name="phone", label="Phone", field_type="tel"),
        control(name="resume", label="Resume", field_type="file"),
    ]


def _bank(**overrides) -> AnswerBank:
    base = {
        "work_authorized": True,
        "requires_sponsorship": False,
        "years_experience": 8,
        # The adapters under test should never need a model; if one does, the
        # test says so rather than reaching for the network.
        "llm_enabled": False,
    }
    base.update(overrides)
    return AnswerBank(**base)


def _ctx(url: str, *, submit: bool = False, resume_path: str | None = None, **kwargs):
    return ApplyContext(
        url=url,
        applicant=ApplicantProfile(
            full_name="Jordan Candidate",
            first_name="Jordan",
            last_name="Candidate",
            email="jordan@example.com",
            phone="+1 415 555 0142",
            resume_path=resume_path,
        ),
        bank=kwargs.pop("bank", _bank()),
        submit=submit,
        resume_path=resume_path,
        log=StepLog("test", enabled=False),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Greenhouse / Lever / generic — the single-page shape                         #
# --------------------------------------------------------------------------- #


class TestGreenhouseAdapter:
    def test_fills_contact_details_and_stops_at_the_button(self):
        page = FakePage([Step(controls=contact_controls(), visible=(SUBMIT,))])
        result = GreenhouseAdapter().run(page, _ctx(GREENHOUSE_URL))

        assert result.status == "filled"
        assert page.filled['[name="first_name"]'] == "Jordan"
        assert page.filled['[name="email"]'] == "jordan@example.com"
        # Nothing was submitted — that is the default, and it is the point.
        assert SUBMIT not in page.clicked

    def test_uploads_the_resume_when_there_is_a_file_to_upload(self, tmp_path):
        resume_file = tmp_path / "resume.txt"
        resume_file.write_text("Jordan Candidate")
        page = FakePage([Step(controls=contact_controls())])

        result = GreenhouseAdapter().run(
            page, _ctx(GREENHOUSE_URL, resume_path=str(resume_file))
        )
        assert result.resume_uploaded
        assert page.uploaded['[name="resume"]'] == str(resume_file)

    def test_submits_when_asked_and_notices_the_confirmation(self):
        page = FakePage(
            [
                Step(controls=contact_controls(), clickable=(SUBMIT,), advances_on=(SUBMIT,)),
                Step(text="Thank you for applying to Acme."),
            ]
        )
        result = GreenhouseAdapter().run(page, _ctx(GREENHOUSE_URL, submit=True))

        assert result.status == "submitted"
        assert SUBMIT in page.clicked
        assert "confirmed" in (result.note or "")

    def test_a_submission_with_no_confirmation_says_so(self):
        page = FakePage([Step(controls=contact_controls(), clickable=(SUBMIT,))])
        result = GreenhouseAdapter().run(page, _ctx(GREENHOUSE_URL, submit=True))

        assert result.status == "submitted"
        assert "no confirmation" in (result.note or "")

    def test_a_page_with_no_controls_is_reported_as_no_form(self):
        page = FakePage([Step(controls=[], text="This role is closed.")])
        assert GreenhouseAdapter().run(page, _ctx(GREENHOUSE_URL)).status == "no_form"

    def test_a_sign_in_wall_stops_the_run(self):
        page = FakePage([Step(controls=contact_controls(), text="Please sign in to apply")])
        result = GreenhouseAdapter().run(page, _ctx(GREENHOUSE_URL))
        assert result.status == "needs_input"

    def test_answers_a_screening_question_from_the_candidates_own_answer(self):
        controls = [
            *contact_controls(),
            control(
                name="sponsorship",
                label="Will you require visa sponsorship?",
                tag="select",
                options=("Yes", "No"),
                required=True,
            ),
        ]
        page = FakePage([Step(controls=controls)])
        result = GreenhouseAdapter().run(page, _ctx(GREENHOUSE_URL))

        assert page.selected['[name="sponsorship"]'] == "No"
        assert result.status == "filled"
        answered = {a["question"]: a for a in result.answers}
        assert answered["Will you require visa sponsorship?"]["source"] == "bank"

    def test_an_unanswerable_required_question_stops_the_run(self):
        controls = [
            *contact_controls(),
            control(
                name="clearance",
                label="What is your active security clearance number?",
                required=True,
            ),
        ]
        page = FakePage([Step(controls=controls, clickable=(SUBMIT,))])
        result = GreenhouseAdapter().run(page, _ctx(GREENHOUSE_URL, submit=True))

        assert result.status == "needs_input"
        assert "security clearance" in result.unanswered[0]
        # The crucial part: nothing was submitted with a blank on the form.
        assert SUBMIT not in page.clicked

    def test_an_optional_unanswered_question_does_not_stop_anything(self):
        controls = [
            *contact_controls(),
            control(name="referral", label="Who referred you?", required=False),
        ]
        page = FakePage([Step(controls=controls, clickable=(SUBMIT,))])
        assert GreenhouseAdapter().run(page, _ctx(GREENHOUSE_URL)).status == "filled"

    def test_a_demographic_question_is_declined_not_answered(self):
        controls = [
            *contact_controls(),
            control(
                name="gender",
                label="Gender",
                tag="select",
                options=("Male", "Female", "Decline to self-identify"),
                required=True,
            ),
        ]
        page = FakePage([Step(controls=controls)])
        result = GreenhouseAdapter().run(page, _ctx(GREENHOUSE_URL))

        assert page.selected['[name="gender"]'] == "Decline to self-identify"
        assert result.status == "filled"

    def test_one_stubborn_field_does_not_abandon_the_others(self):
        controls = contact_controls()
        page = FakePage([Step(controls=controls)])
        original_fill = page.fill

        def fill(selector, value, timeout=None):
            if selector == '[name="email"]':
                raise RuntimeError("element is not editable")
            return original_fill(selector, value, timeout)

        page.fill = fill
        result = GreenhouseAdapter().run(page, _ctx(GREENHOUSE_URL))

        assert result.status == "filled"
        assert "first_name" in result.filled_fields
        assert "email" not in result.filled_fields


class TestLeverAdapter:
    def test_goes_straight_to_the_apply_page(self):
        page = FakePage([Step(controls=contact_controls())])
        LeverAdapter().run(page, _ctx(LEVER_URL))
        assert page.visited == [f"{LEVER_URL}/apply"]

    def test_an_apply_url_is_used_as_given(self):
        page = FakePage([Step(controls=contact_controls())])
        LeverAdapter().run(page, _ctx(f"{LEVER_URL}/apply"))
        assert page.visited == [f"{LEVER_URL}/apply"]


class TestGenericAdapter:
    def test_dismisses_a_cookie_banner_before_filling(self):
        page = FakePage(
            [
                Step(
                    controls=contact_controls(),
                    clickable=("#onetrust-accept-btn-handler",),
                )
            ]
        )
        result = GenericAdapter().run(page, _ctx("https://careers.acme.com/apply/1"))
        assert "#onetrust-accept-btn-handler" in page.clicked
        assert result.status == "filled"


# --------------------------------------------------------------------------- #
# Workday — the multi-step shape                                               #
# --------------------------------------------------------------------------- #


class TestWorkdayAdapter:
    @staticmethod
    def wizard(*, submit_visible_on_last: bool = True) -> FakePage:
        return FakePage(
            [
                Step(  # My Information
                    controls=[
                        control(automation_id="legalNameSection_firstName", label="First Name"),
                        control(automation_id="legalNameSection_lastName", label="Last Name"),
                        control(automation_id="email", label="Email Address"),
                    ],
                    clickable=(NEXT,),
                    advances_on=(NEXT,),
                ),
                Step(  # Application questions
                    controls=[
                        control(
                            automation_id="q1",
                            label="Are you legally authorized to work in the US?",
                            tag="select",
                            options=("Yes", "No"),
                            required=True,
                        ),
                    ],
                    clickable=(NEXT,),
                    advances_on=(NEXT,),
                ),
                Step(  # Review
                    controls=[],
                    clickable=(SUBMIT,),
                    visible=(SUBMIT,) if submit_visible_on_last else (),
                    text="Review your application",
                ),
            ]
        )

    def test_walks_every_step_and_fills_each_one(self):
        page = self.wizard()
        result = WorkdayAdapter().run(page, _ctx(WORKDAY_URL))

        assert result.status == "filled"
        assert page.filled['[data-automation-id="legalNameSection_firstName"]'] == "Jordan"
        assert page.selected['[data-automation-id="q1"]'] == "Yes"
        assert page.clicked.count(NEXT) == 2

    def test_submits_on_the_final_step_when_asked(self):
        page = self.wizard()
        result = WorkdayAdapter().run(page, _ctx(WORKDAY_URL, submit=True))
        assert result.status == "submitted"
        assert SUBMIT in page.clicked

    def test_stops_at_a_sign_in_wall_without_creating_an_account(self):
        page = FakePage(
            [
                Step(
                    controls=[],
                    visible=('[data-automation-id="createAccountLink"]',),
                    text="Create Account to continue",
                )
            ]
        )
        result = WorkdayAdapter().run(page, _ctx(WORKDAY_URL))
        assert result.status == "needs_input"
        assert "candidate account" in (result.note or "")

    def test_an_unanswerable_question_stops_the_wizard_mid_flight(self):
        page = FakePage(
            [
                Step(
                    controls=[
                        control(
                            automation_id="q1",
                            label="What is your Acme employee number?",
                            required=True,
                        )
                    ],
                    clickable=(NEXT, SUBMIT),
                    advances_on=(NEXT,),
                ),
                Step(controls=[], visible=(SUBMIT,)),
            ]
        )
        result = WorkdayAdapter().run(page, _ctx(WORKDAY_URL, submit=True))
        assert result.status == "needs_input"
        assert SUBMIT not in page.clicked

    def test_a_wizard_that_never_ends_is_stopped_by_the_step_cap(self):
        # A page that always offers "Continue" and never a submit button would
        # otherwise loop against a real employer's form forever.
        forever = [
            Step(
                controls=[control(name=f"f{i}", label=f"Field {i}")],
                clickable=(NEXT,),
                advances_on=(NEXT,),
            )
            for i in range(WorkdayAdapter.MAX_STEPS + 5)
        ]
        page = FakePage(forever)
        result = WorkdayAdapter().run(page, _ctx(WORKDAY_URL))
        assert page.clicked.count(NEXT) <= WorkdayAdapter.MAX_STEPS
        assert result.status == "filled"


class TestAdapterRegistry:
    @pytest.mark.parametrize(
        ("platform", "expected"),
        [
            (ATSPlatform.GREENHOUSE, GreenhouseAdapter),
            (ATSPlatform.LEVER, LeverAdapter),
            (ATSPlatform.WORKDAY, WorkdayAdapter),
            (ATSPlatform.GENERIC, GenericAdapter),
        ],
    )
    def test_maps_platforms_to_their_adapter(self, platform, expected):
        assert isinstance(adapter_for(platform), expected)

    def test_linkedin_is_handled_by_the_linkedin_service_instead(self):
        assert adapter_for(ATSPlatform.LINKEDIN) is None


# --------------------------------------------------------------------------- #
# Browser plumbing                                                             #
# --------------------------------------------------------------------------- #


class TestGoto:
    def test_a_navigation_failure_is_retryable(self):
        page = FakePage([Step()])
        page.goto_failures = 1
        with pytest.raises(TransientBrowserError):
            goto(page, "https://example.test")


class TestWithRetries:
    def test_returns_the_first_success_without_sleeping(self):
        slept: list[float] = []
        result, report = with_retries(
            lambda _n: "done", attempts=3, sleep=slept.append
        )
        assert result == "done"
        assert report.attempts == 1 and not report.retried and slept == []

    def test_retries_a_transient_failure_and_reports_it(self):
        calls: list[int] = []
        slept: list[float] = []

        def flaky(attempt: int) -> str:
            calls.append(attempt)
            if attempt < 3:
                raise TransientBrowserError("net::ERR_TIMED_OUT")
            return "done"

        result, report = with_retries(flaky, attempts=3, sleep=slept.append)
        assert result == "done"
        assert calls == [1, 2, 3]
        assert report.attempts == 3 and report.retried
        assert len(report.errors) == 2
        # Backoff grows rather than hammering.
        assert slept == [2.0, 4.0]

    def test_gives_up_and_re_raises_the_last_failure(self):
        def always(_n):
            raise TransientBrowserError("still broken")

        with pytest.raises(TransientBrowserError, match="still broken"):
            with_retries(always, attempts=2, sleep=lambda _s: None)

    def test_anything_not_transient_is_not_retried(self):
        calls: list[int] = []

        def boom(attempt: int):
            calls.append(attempt)
            raise ValueError("a question we can't answer stays unanswerable")

        with pytest.raises(ValueError):
            with_retries(boom, attempts=3, sleep=lambda _s: None)
        assert calls == [1]


# The real thing, when the driver is installed but the browser it downloads is
# not — the state this deployment was actually in. The banner is addressed to
# whoever runs the server, which is exactly why it must not reach a candidate.
_MISSING_BINARY_MESSAGE = (
    "BrowserType.launch: Executable doesn't exist at "
    "/opt/TalentPing/.cache/ms-playwright/chromium_headless_shell-1228/"
    "chrome-headless-shell-linux64/chrome-headless-shell\n"
    "╔════════════════════════════════════════════════════════════╗\n"
    "║ Looks like Playwright was just installed or updated.       ║\n"
    "║ Please run the following command to download new browsers: ║\n"
    "║     playwright install                                     ║\n"
    "╚════════════════════════════════════════════════════════════╝"
)


class _FakeChromium:
    def __init__(self, *, executable_path="/nowhere/chrome", launch_error=None):
        self.executable_path = executable_path
        self._launch_error = launch_error

    def launch(self, **_kwargs):
        if self._launch_error is not None:
            raise self._launch_error
        raise AssertionError("these tests never reach a real browser")


class _FakePlaywright:
    """Stands in for ``sync_playwright()``, which is a context manager."""

    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fake_playwright(monkeypatch, chromium) -> None:
    # ``raising=False``: where Playwright is genuinely absent — CI, and any dev
    # machine without the browser extra — the module never binds the name at all.
    monkeypatch.setattr(browser_runner, "_PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(
        browser_runner,
        "sync_playwright",
        lambda: _FakePlaywright(chromium),
        raising=False,
    )
    browser_runner.binary_installed.cache_clear()


class TestBrowserBinaryDetection:
    """A driver that imports is not a browser that runs.

    The two were conflated, so a server with Playwright installed and no
    Chromium reported itself ready, accepted the run, and failed at launch.
    """

    def setup_method(self):
        browser_runner.binary_installed.cache_clear()

    def teardown_method(self):
        browser_runner.binary_installed.cache_clear()

    def test_is_unavailable_when_the_binary_is_missing(self, monkeypatch, tmp_path):
        _fake_playwright(
            monkeypatch, _FakeChromium(executable_path=str(tmp_path / "absent"))
        )
        assert browser_runner.binary_installed() is False
        assert browser_runner.is_available() is False

    def test_is_available_when_both_the_driver_and_the_binary_are_present(
        self, monkeypatch, tmp_path
    ):
        chrome = tmp_path / "chrome"
        chrome.write_text("#!/bin/sh\n")
        _fake_playwright(monkeypatch, _FakeChromium(executable_path=str(chrome)))
        assert browser_runner.binary_installed() is True
        assert browser_runner.is_available() is True

    def test_the_package_alone_is_not_enough(self, monkeypatch):
        """The old check — and the whole bug — was this one returning True."""
        monkeypatch.setattr(browser_runner, "_PLAYWRIGHT_AVAILABLE", True)
        monkeypatch.setattr(browser_runner, "binary_installed", lambda: False)
        assert browser_runner.is_available() is False

    def test_an_unreadable_driver_counts_as_unavailable(self, monkeypatch):
        monkeypatch.setattr(browser_runner, "_PLAYWRIGHT_AVAILABLE", True)

        def explode():
            raise RuntimeError("driver process would not start")

        monkeypatch.setattr(
            browser_runner, "sync_playwright", explode, raising=False
        )
        browser_runner.binary_installed.cache_clear()
        assert browser_runner.binary_installed() is False

    def test_the_answer_is_cached_rather_than_re_read_per_call(
        self, monkeypatch, tmp_path
    ):
        calls: list[int] = []

        class Counting(_FakeChromium):
            @property
            def executable_path(self):  # type: ignore[override]
                calls.append(1)
                return str(tmp_path / "absent")

            @executable_path.setter
            def executable_path(self, _value):
                return None

        _fake_playwright(monkeypatch, Counting())
        browser_runner.binary_installed()
        browser_runner.binary_installed()
        assert len(calls) == 1


class TestBrowserPageLaunchFailures:
    def setup_method(self):
        browser_runner.binary_installed.cache_clear()

    def teardown_method(self):
        browser_runner.binary_installed.cache_clear()

    def test_a_missing_binary_becomes_browser_unavailable(self, monkeypatch):
        _fake_playwright(
            monkeypatch,
            _FakeChromium(launch_error=RuntimeError(_MISSING_BINARY_MESSAGE)),
        )
        with pytest.raises(BrowserUnavailable) as caught:
            with browser_runner.browser_page():
                pass  # pragma: no cover - the launch never returns a page

        message = str(caught.value)
        assert "playwright install chromium" in message
        # The operator-facing banner is not the candidate's problem.
        assert "╔" not in message and "Looks like Playwright" not in message

    def test_it_is_not_a_transient_error_so_it_is_never_retried(self, monkeypatch):
        """A binary will not appear between attempt one and attempt three."""
        _fake_playwright(
            monkeypatch,
            _FakeChromium(launch_error=RuntimeError(_MISSING_BINARY_MESSAGE)),
        )
        calls: list[int] = []

        def launch(attempt: int):
            calls.append(attempt)
            with browser_runner.browser_page():
                pass  # pragma: no cover

        with pytest.raises(BrowserUnavailable):
            with_retries(launch, attempts=3, sleep=lambda _s: None)
        assert calls == [1]

    def test_any_other_launch_failure_is_left_alone(self, monkeypatch):
        """Only the missing-binary case is reclassified; the rest stay themselves."""
        _fake_playwright(
            monkeypatch,
            _FakeChromium(launch_error=RuntimeError("Target page crashed")),
        )
        with pytest.raises(RuntimeError, match="Target page crashed"):
            with browser_runner.browser_page():
                pass  # pragma: no cover


class TestStepLog:
    def test_records_a_screenshot_per_step(self, tmp_path):
        page = FakePage([Step()])
        log = StepLog("run-1", enabled=True, base_dir=tmp_path)
        log.step(page, "opened")
        log.step(page, "application form", note="5 controls")

        assert [entry["step"] for entry in log.entries] == [1, 2]
        assert log.entries[1]["note"] == "5 controls"
        assert len(page.screenshots) == 2
        assert log.entries[0]["screenshot"].endswith(".png")

    def test_a_failed_screenshot_never_loses_the_step(self, tmp_path):
        page = FakePage([Step()])

        def broken(**_kwargs):
            raise RuntimeError("target closed")

        page.screenshot = broken
        log = StepLog("run-2", enabled=True, base_dir=tmp_path)
        entry = log.step(page, "submitted")

        assert entry["step"] == 1
        assert "screenshot" not in entry
        assert "screenshot_error" in entry

    def test_screenshots_can_be_switched_off_entirely(self, tmp_path):
        page = FakePage([Step()])
        log = StepLog("run-3", enabled=False, base_dir=tmp_path)
        log.step(page, "opened")
        assert page.screenshots == []
        assert log.entries[0]["label"] == "opened"


class TestForm:
    def test_reads_options_and_required_off_the_page(self):
        page = FakePage(
            [
                Step(
                    controls=[
                        control(
                            name="country",
                            label="Country",
                            tag="select",
                            options=("US", "UK"),
                            required=True,
                        )
                    ]
                )
            ]
        )
        field_ = Form(page).fields()[0]
        assert field_.options == ["US", "UK"]
        assert field_.required
        assert field_.selector == '[name="country"]'

    def test_workday_controls_are_addressed_by_automation_id(self):
        page = FakePage([Step(controls=[control(automation_id="email", label="Email")])])
        assert Form(page).fields()[0].selector == '[data-automation-id="email"]'

    def test_click_returns_the_selector_that_worked(self):
        page = FakePage([Step(clickable=("#second",))])
        assert Form(page).click(["#first", "#second"]) == "#second"

    def test_click_returns_none_when_nothing_matches(self):
        assert Form(FakePage([Step()])).click(["#nope"]) is None


# --------------------------------------------------------------------------- #
# The service                                                                  #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def posting(db_session, current_user) -> JobPosting:
    row = JobPosting(
        user_id=current_user.id,
        title="Backend Engineer",
        company="Acme",
        url=GREENHOUSE_URL,
        fingerprint=job_fingerprint("Backend Engineer", "Acme", GREENHOUSE_URL),
        status=JobStatus.NEW,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


class TestCreateApplication:
    def test_detects_the_platform_from_the_posting_url(
        self, db_session, current_user, posting
    ):
        application = form_apply_service.create_application(
            db_session, current_user, posting=posting
        )
        assert application.platform is ATSPlatform.GREENHOUSE
        assert application.status is FormApplyStatus.QUEUED
        assert application.submit_requested is False
        assert application.company == "Acme"

    def test_a_posting_with_no_url_is_refused(self, db_session, current_user):
        bare = JobPosting(
            user_id=current_user.id,
            title="Backend Engineer",
            company="Acme",
            fingerprint=job_fingerprint("Backend Engineer", "Acme", "x"),
        )
        db_session.add(bare)
        db_session.commit()
        with pytest.raises(FormApplyRefused, match="no application URL"):
            form_apply_service.create_application(db_session, current_user, posting=bare)

    def test_a_link_we_cannot_open_is_refused(self, db_session, current_user):
        with pytest.raises(FormApplyRefused, match="application link"):
            form_apply_service.create_application(
                db_session, current_user, url="mailto:jobs@acme.com"
            )

    def test_a_posting_already_submitted_is_never_submitted_twice(
        self, db_session, current_user, posting
    ):
        done = form_apply_service.create_application(
            db_session, current_user, posting=posting, submit=True
        )
        done.status = FormApplyStatus.SUBMITTED
        db_session.commit()

        with pytest.raises(FormApplyRefused, match="already applied"):
            form_apply_service.create_application(
                db_session, current_user, posting=posting, submit=True
            )

    def test_filling_it_again_is_always_allowed(
        self, db_session, current_user, posting
    ):
        done = form_apply_service.create_application(
            db_session, current_user, posting=posting, submit=True
        )
        done.status = FormApplyStatus.SUBMITTED
        db_session.commit()
        # Filling costs the candidate nothing and doesn't reach the employer.
        again = form_apply_service.create_application(
            db_session, current_user, posting=posting, submit=False
        )
        assert again.id != done.id

    def test_the_daily_submission_limit_is_enforced(
        self, db_session, current_user, posting, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "form_apply_daily_limit", 1)
        first = form_apply_service.create_application(
            db_session, current_user, url="https://jobs.lever.co/acme/1", submit=True
        )
        from datetime import UTC, datetime

        first.status = FormApplyStatus.SUBMITTED
        first.finished_at = datetime.now(UTC)
        db_session.commit()

        with pytest.raises(FormApplyRefused, match="Daily form-application limit"):
            form_apply_service.create_application(
                db_session, current_user, posting=posting, submit=True
            )


class TestRunApplication:
    def test_reports_unsupported_when_no_browser_is_installed(
        self, db_session, current_user, posting, monkeypatch
    ):
        monkeypatch.setattr(browser_runner, "is_available", lambda: False)
        application = form_apply_service.create_application(
            db_session, current_user, posting=posting
        )
        result = form_apply_service.run_application(db_session, application)

        assert result.status is FormApplyStatus.UNSUPPORTED
        assert "playwright install" in (result.note or "")
        db_session.refresh(posting)
        assert posting.form_apply_status == "unsupported"

    def test_drives_a_real_adapter_over_a_fake_browser(
        self, db_session, current_user, posting, resume, monkeypatch
    ):
        page = FakePage(
            [
                Step(
                    controls=contact_controls(),
                    clickable=(SUBMIT,),
                    advances_on=(SUBMIT,),
                ),
                Step(text="Thank you for applying to Acme."),
            ]
        )
        _fake_browser(monkeypatch, page)
        _profile(db_session, current_user)

        application = form_apply_service.create_application(
            db_session, current_user, posting=posting, submit=True
        )
        result = form_apply_service.run_application(db_session, application)

        assert result.status is FormApplyStatus.SUBMITTED
        assert result.attempts == 1
        assert "first_name" in result.filled_fields
        assert result.finished_at is not None and result.duration_ms is not None

        db_session.refresh(posting)
        assert posting.status is JobStatus.APPLIED
        assert posting.form_apply_status == "submitted"
        assert posting.applied_at is not None

    def test_a_transient_failure_is_retried_within_the_run(
        self, db_session, current_user, posting, resume, monkeypatch
    ):
        page = FakePage([Step(controls=contact_controls())])
        page.goto_failures = 1  # first navigation dies, second works
        _fake_browser(monkeypatch, page)
        _profile(db_session, current_user)
        monkeypatch.setattr(browser_runner.time, "sleep", lambda _s: None)

        application = form_apply_service.create_application(
            db_session, current_user, posting=posting
        )
        result = form_apply_service.run_application(db_session, application)

        assert result.status is FormApplyStatus.FILLED
        assert "attempt 2" in (result.note or "")

    def test_an_unanswerable_question_leaves_the_run_needing_input(
        self, db_session, current_user, posting, resume, monkeypatch
    ):
        controls = [
            *contact_controls(),
            control(name="badge", label="What is your Acme badge number?", required=True),
        ]
        page = FakePage([Step(controls=controls, clickable=(SUBMIT,))])
        _fake_browser(monkeypatch, page)
        _profile(db_session, current_user, llm_answers_enabled=False)

        application = form_apply_service.create_application(
            db_session, current_user, posting=posting, submit=True
        )
        result = form_apply_service.run_application(db_session, application)

        assert result.status is FormApplyStatus.NEEDS_INPUT
        assert result.unanswered == ["What is your Acme badge number?"]
        assert SUBMIT not in page.clicked
        db_session.refresh(posting)
        assert posting.status is not JobStatus.APPLIED

    def test_an_unexpected_failure_is_recorded_rather_than_raised(
        self, db_session, current_user, posting, monkeypatch
    ):
        monkeypatch.setattr(browser_runner, "is_available", lambda: True)

        def explode(**_kwargs):
            raise RuntimeError("chromium crashed")

        monkeypatch.setattr(browser_runner, "browser_page", explode)
        application = form_apply_service.create_application(
            db_session, current_user, posting=posting
        )
        result = form_apply_service.run_application(db_session, application)

        assert result.status is FormApplyStatus.FAILED
        assert "chromium crashed" in (result.error or "")


def _fake_browser(monkeypatch, page: FakePage) -> None:
    """Point the service at a scripted page instead of a real Chromium."""
    import contextlib

    @contextlib.contextmanager
    def fake_page(**_kwargs):
        yield page

    monkeypatch.setattr(browser_runner, "is_available", lambda: True)
    monkeypatch.setattr(browser_runner, "browser_page", fake_page)


def _profile(db_session, user, **overrides) -> FormApplyProfile:
    profile = form_apply_service.get_or_create_profile(db_session, user)
    profile.work_authorized = True
    profile.requires_sponsorship = False
    profile.llm_answers_enabled = False
    for name, value in overrides.items():
        setattr(profile, name, value)
    db_session.commit()
    return profile


class TestMaterializeResume:
    def test_writes_the_extracted_text_when_there_is_no_original(self, resume):
        path = form_apply_service.materialize_resume(resume)
        assert path is not None and path.endswith(".txt")
        from pathlib import Path

        assert "Jordan Candidate" in Path(path).read_text()

    def test_no_resume_means_no_upload(self):
        assert form_apply_service.materialize_resume(None) is None


# --------------------------------------------------------------------------- #
# The endpoints                                                                #
# --------------------------------------------------------------------------- #


class TestFormApplyEndpoints:
    def test_status_reports_what_this_server_can_drive(self, auth_client):
        body = auth_client.get("/api/v1/form-apply/status").json()
        assert "browser_installed" in body
        platforms = {p["platform"] for p in body["platforms"]}
        assert {"greenhouse", "lever", "workday", "linkedin"} <= platforms
        assert body["budget"]["limit"] > 0

    def test_the_profile_is_created_empty_and_saved(self, auth_client):
        created = auth_client.get("/api/v1/form-apply/profile").json()
        assert created["work_authorized"] is None
        assert created["llm_answers_enabled"] is True

        saved = auth_client.put(
            "/api/v1/form-apply/profile",
            json={
                "work_authorized": True,
                "requires_sponsorship": False,
                "desired_salary": "$180,000",
                "custom_answers": {"how did you hear": "A friend"},
            },
        ).json()
        assert saved["work_authorized"] is True
        assert saved["custom_answers"] == {"how did you hear": "A friend"}

    def test_applying_to_a_posting_records_a_run(
        self, auth_client, db_session, posting, resume
    ):
        resp = auth_client.post(f"/api/v1/form-apply/jobs/{posting.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["application"]["platform"] == "greenhouse"
        assert body["application"]["platform_label"] == "Greenhouse"
        # No browser in the test environment: the contract is a clean status.
        assert body["application"]["status"] == "UNSUPPORTED"

        listed = auth_client.get("/api/v1/form-apply").json()
        assert [row["id"] for row in listed] == [body["application"]["id"]]

    def test_applying_to_a_pasted_url_works_too(self, auth_client, resume):
        resp = auth_client.post(
            "/api/v1/form-apply/url",
            json={"url": "https://jobs.lever.co/acme/1a2b3c4d", "submit": False},
        )
        assert resp.status_code == 200
        assert resp.json()["application"]["platform"] == "lever"

    def test_someone_elses_posting_is_not_reachable(self, auth_client, db_session):
        from app.models.user import User

        other = User(
            email="other@example.com", full_name="Other", hashed_password="x"
        )
        db_session.add(other)
        db_session.commit()
        theirs = JobPosting(
            user_id=other.id,
            title="Backend Engineer",
            company="Acme",
            url=GREENHOUSE_URL,
            fingerprint=job_fingerprint("Backend Engineer", "Acme", "other"),
        )
        db_session.add(theirs)
        db_session.commit()

        assert auth_client.post(f"/api/v1/form-apply/jobs/{theirs.id}").status_code == 404

    def test_a_duplicate_submission_is_refused_with_a_conflict(
        self, auth_client, db_session, current_user, posting
    ):
        done = form_apply_service.create_application(
            db_session, current_user, posting=posting, submit=True
        )
        done.status = FormApplyStatus.SUBMITTED
        db_session.commit()

        resp = auth_client.post(
            f"/api/v1/form-apply/jobs/{posting.id}", json={"submit": True}
        )
        assert resp.status_code == 409
        assert "already applied" in resp.json()["detail"]

    def test_the_detail_view_carries_the_audit_trail(
        self, auth_client, db_session, current_user, posting
    ):
        application = form_apply_service.create_application(
            db_session, current_user, posting=posting
        )
        application.status = FormApplyStatus.FILLED
        application.answers = [
            {"question": "Sponsorship?", "answer": "No", "source": "bank"}
        ]
        application.steps = [{"step": 1, "label": "opened", "screenshot": "a.png"}]
        db_session.commit()

        body = auth_client.get(f"/api/v1/form-apply/{application.id}").json()
        assert body["answers"][0]["source"] == "bank"
        assert body["screenshot_count"] == 1

    def test_only_a_recorded_screenshot_can_be_requested(
        self, auth_client, db_session, current_user, posting
    ):
        application = form_apply_service.create_application(
            db_session, current_user, posting=posting
        )
        application.steps = [{"step": 1, "label": "opened", "screenshot": "step-1.png"}]
        db_session.commit()

        # A name the run never recorded — including a traversal attempt.
        for name in ("nope.png", "..%2F..%2Fetc%2Fpasswd"):
            resp = auth_client.get(
                f"/api/v1/form-apply/{application.id}/screenshots/{name}"
            )
            assert resp.status_code == 404

        # A recorded name whose file has been cleaned up reads as gone, not 404.
        assert (
            auth_client.get(
                f"/api/v1/form-apply/{application.id}/screenshots/step-1.png"
            ).status_code
            == 410
        )

    def test_a_finished_run_cannot_be_retried(
        self, auth_client, db_session, current_user, posting
    ):
        application = form_apply_service.create_application(
            db_session, current_user, posting=posting
        )
        application.status = FormApplyStatus.NEEDS_INPUT
        db_session.commit()

        resp = auth_client.post(f"/api/v1/form-apply/{application.id}/retry")
        assert resp.status_code == 409

    def test_a_failed_run_can_be_retried(
        self, auth_client, db_session, current_user, posting
    ):
        application = form_apply_service.create_application(
            db_session, current_user, posting=posting
        )
        application.status = FormApplyStatus.FAILED
        application.attempts = 3
        db_session.commit()

        resp = auth_client.post(f"/api/v1/form-apply/{application.id}/retry")
        assert resp.status_code == 200
        assert resp.json()["application"]["max_attempts"] >= 4

    def test_everything_here_needs_authentication(self, client):
        for path in ("/api/v1/form-apply", "/api/v1/form-apply/status", "/api/v1/form-apply/profile"):
            assert client.get(path).status_code == 401


class TestFormApplicationModel:
    def test_terminal_and_retryable_statuses(self):
        assert not FormApplyStatus.QUEUED.is_terminal
        assert not FormApplyStatus.RUNNING.is_terminal
        assert FormApplyStatus.SUBMITTED.is_terminal
        assert FormApplyStatus.FAILED.is_retryable
        assert FormApplyStatus.RATE_LIMITED.is_retryable
        # A run waiting on the candidate is not worth re-running.
        assert not FormApplyStatus.NEEDS_INPUT.is_retryable
        assert not FormApplyStatus.SUBMITTED.is_retryable

    def test_screenshot_count_ignores_steps_that_failed_to_capture(self):
        application = FormApplication(
            steps=[
                {"step": 1, "screenshot": "a.png"},
                {"step": 2, "screenshot_error": "target closed"},
            ]
        )
        assert application.screenshot_count == 1


def test_selector_helper_matches_the_model(  # a fake that drifts tests nothing
):
    payload = control(name="email")
    from app.services.career_apply_service import FormField

    assert selector_for(payload) == FormField(name="email").selector
