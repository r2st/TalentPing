"""Per-ATS application adapters — the top five platforms, and the fallback.

These five cover most of what a candidate actually meets, and they fail in
completely different ways, which is why each gets its own adapter rather than
one clever universal filler:

* **Greenhouse** is the easy case: one page, one form, often iframed into the
  company's own careers site — so the controls live in a frame, not the top
  document.
* **Lever** splits the posting and the form across two URLs. Opening the posting
  and hunting for the apply button is where a generic filler gets lost; the
  ``/apply`` page is predictable, so we go straight there.
* **Ashby** does the same at ``/application``, but the posting page renders the
  form inline behind an "Apply for this Job" button, so the run also has to be
  willing to press it.
* **iCIMS** puts the form in an iframe of its own name and — on most tenants —
  behind a candidate account. Recognising that wall and stopping is the whole
  job: iCIMS is the platform where a blind filler burns an attempt typing into
  a login page.
* **Workday** is a multi-step wizard behind a sign-in wall, with no useful
  ``name`` or ``id`` attributes — everything hangs off ``data-automation-id``.
  It is the one that needs a state machine rather than a form fill.

Every adapter is built from the same three moves — fill what the resume knows,
answer what it doesn't (:mod:`app.services.form_answers`), then either stop or
submit — and talks to the page only through :class:`~app.services.browser_runner.Form`.
That is what makes them testable: the tests drive a fake page implementing
``Form``'s small vocabulary and run a whole Workday wizard with no browser.

**Submission is opt-in per run.** With ``submit=False`` the adapter fills
everything and stops at the button, which is the default everywhere. And a form
that asks something we cannot answer honestly stops as ``needs_input`` rather
than submitting a blank or a guess.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from app.models.form_apply import ATSPlatform
from app.models.resume import Resume
from app.services import ats_platform
from app.services.browser_runner import (
    ApplyBlocked,
    Deadline,
    Form,
    PostingGone,
    RateLimited,
    StepLog,
    TransientBrowserError,
    scope_for,
)
from app.services.career_apply_service import (
    NON_INPUT_TYPES,
    ApplicantProfile,
    FormField,
    plan_fills,
)
from app.services.form_answers import (
    Answer,
    AnswerBank,
    ScreeningQuestion,
    answer_questions,
    blocking_questions,
)
from app.services.openrouter_client import chat_completion

logger = logging.getLogger(__name__)

# Controls that are never filled: they are not questions, they are chrome.
#
# Defined next to `plan_fills` rather than here, because `plan_fills` has to
# apply it too — a chrome control that matches a rule spends the profile
# attribute the real field needed. This module's copy was the only one, which
# is what left the plain career-page path unguarded.
_NON_INPUT_TYPES = NON_INPUT_TYPES

_SUBMIT_SELECTORS = (
    '[data-automation-id="bottom-navigation-next-button"]',
    'button[type="submit"]',
    'input[type="submit"]',
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
)

_NEXT_SELECTORS = (
    '[data-automation-id="bottom-navigation-next-button"]',
    "button:has-text('Save and Continue')",
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button:has-text('Review')",
)

_COOKIE_SELECTORS = (
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "#onetrust-accept-btn-handler",
    "[data-testid='cookie-accept']",
)

# Text that means the page wants a human before it wants a form filled.
#
# The Cloudflare phrasings are here because the interstitial is served with a
# 403 *or* a 200 depending on the challenge, so the status check alone does not
# see all of it — and because "verify you are human" was the only bot-wall
# marker in this list and Cloudflare does not say that. It says "Verifying you
# are human", present participle, which shares no substring with the entry that
# was supposed to catch it. So the run read an interstitial as an ordinary page,
# found no controls, and reported `no_form`.
_WALL_MARKERS = (
    "create account",
    "sign in to apply",
    "please sign in",
    "verify you are human",
    "verifying you are human",
    "checking your browser",
    "checking if the site connection is secure",
    "enable javascript and cookies to continue",
    "captcha",
    "are you a robot",
    "unusual activity",
    "access denied",
)

# Text that means the site is throttling us, on a page that answered 200.
#
# The 429 is caught by the status check; this is the other half — several ATS
# and most CDNs in front of them serve a perfectly ordinary 200 page saying to
# come back later. Read as a page it has no form on it, so it landed as
# `no_form`: terminal, unretryable, and the opposite of what a throttle is
# asking for.
_THROTTLE_MARKERS = (
    "too many requests",
    "rate limit exceeded",
    "you have made too many",
    "slow down",
    "try again in a few minutes",
    "temporarily blocked",
)

# Text that means a submit click did *not* result in an application.
#
# `finish_run` used to treat the absence of a confirmation as merely
# unconfirmed — "Submitted — no confirmation text on the page, see the
# screenshot" — and report `submitted` anyway. That is the right reading of
# silence, and the wrong reading of a page that came back saying the
# application was rejected: the row goes SUBMITTED, the posting flips to
# APPLIED, the analytics event fires, the day's budget is spent, and
# `create_application` refuses ever to try that posting again. The candidate is
# recorded as having applied to a job they have not applied to, and the product
# will not let them.
#
# So silence stays `submitted`, and only a *positive* statement of failure
# downgrades the run. That is why the markers are whole phrases and not the
# words that make them up: "required", "invalid" and "error" appear on the
# chrome of half the forms on the internet — a validation hint next to an empty
# optional field would fail every honest submission on the site.
_SUBMIT_ERROR_MARKERS = (
    "there was a problem",
    "there were problems",
    "there was an error",
    "an error occurred",
    "an unexpected error",
    "something went wrong",
    "please correct the errors",
    "please correct the following",
    "please fix the errors",
    "could not be submitted",
    "was not submitted",
    "unable to submit your application",
    "we were unable to process",
    "your application could not",
    "please try again later",
)

_CONFIRMATION_MARKERS = (
    "thank you for applying",
    "application submitted",
    "your application has been",
    "we have received your application",
    "thanks for applying",
    "application received",
)


@dataclass
class ApplyContext:
    """Everything an adapter needs about the candidate and this application."""

    url: str
    applicant: ApplicantProfile
    bank: AnswerBank
    resume: Resume | None = None
    resume_path: str | None = None
    submit: bool = False
    job_title: str | None = None
    company: str | None = None
    log: StepLog | None = None
    # The completion function the answering engine uses. A plain field rather
    # than a hard import so tests answer screening questions without an LLM.
    completion: Any = chat_completion
    # The run's wall-clock ceiling, checked between wizard steps. ``None``
    # means no ceiling, which is what every existing test gets.
    deadline: Deadline | None = None

    def out_of_time(self) -> bool:
        return self.deadline is not None and self.deadline.expired

    def record(self, page, label: str, *, note: str | None = None) -> None:
        if self.log is not None:
            self.log.step(page, label, note=note)


@dataclass
class AdapterResult:
    """What one adapter run did — the shape persisted on ``FormApplication``."""

    # filled | submitted | needs_input | no_form | failed
    status: str
    filled_fields: list[str] = field(default_factory=list)
    answers: list[dict] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)
    resume_uploaded: bool = False
    note: str | None = None
    # What the employer's page said back, verbatim, when it acknowledged the
    # submission. The receipt's strongest line; None when it said nothing.
    confirmation: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("filled", "submitted")


@dataclass
class FillOutcome:
    filled: list[str] = field(default_factory=list)
    answers: list[Answer] = field(default_factory=list)
    resume_uploaded: bool = False
    blocking: list[str] = field(default_factory=list)
    control_count: int = 0


# --------------------------------------------------------------------------- #
# Shared moves                                                                 #
# --------------------------------------------------------------------------- #


#: HTTP statuses the ATS will probably answer differently in a minute. Retried
#: inside the run, then escalated to the task-level retry if they persist.
#:
#: The 52x block is Cloudflare's: 520 "unknown error", 521 "origin down", 522
#: "connection timed out", 523 "origin unreachable", 524 "a timeout occurred".
#: They are the employer's own server having a moment behind a CDN, which is
#: exactly the case the retry machinery exists for.
RETRYABLE_STATUSES = frozenset(
    {408, 425, 500, 502, 503, 504, 507, 509, 520, 521, 522, 523, 524}
)

#: The posting is not there. Terminal — a removed job does not come back.
GONE_STATUSES = frozenset({404, 410})

#: The site refused us. A person can still open the link; this process cannot.
#: 451 is in here rather than with the gone statuses because the posting exists
#: and is being withheld, which is a different thing to tell the candidate.
BLOCKED_STATUSES = frozenset({401, 403, 407, 451})

#: "Slow down." Escalated past the in-run retry — see
#: :class:`~app.services.browser_runner.RateLimited`.
RATE_LIMIT_STATUSES = frozenset({429})

#: Content types that are a web page. Anything else is a link that does not
#: lead to a form, however cheerfully it answered 200.
_PAGE_CONTENT_TYPES = ("text/html", "application/xhtml", "text/plain", "text/xml")

#: Chromium's wording for a URL that redirects to itself (directly or round a
#: cycle). Retrying it is the one thing guaranteed not to help: the loop is a
#: property of the site's configuration, not of this attempt.
_REDIRECT_LOOP_MARKERS = ("err_too_many_redirects", "too_many_redirects")


def _header(response, name: str) -> str:
    """One response header, lowercased, or "" — never raising.

    Playwright exposes headers as a method on some versions and a mapping on
    others, and a fake page has neither. A header we cannot read is a header
    that says nothing, which is the same as an absent one.
    """
    try:
        getter = getattr(response, "headers", None)
        if getter is None:
            return ""
        headers = getter() if callable(getter) else getter
        value = (headers or {}).get(name) or (headers or {}).get(name.title())
    except Exception:  # noqa: BLE001 - an unreadable header is an absent one
        # Inside the ``try`` rather than before it: on a detached response
        # ``headers`` is a property that raises, and ``getattr``'s default
        # does not catch that — it only covers the attribute being absent.
        return ""
    return str(value or "").lower()


def check_response(response, url: str) -> None:
    """Turn the HTTP status of a navigation into the right kind of failure.

    Nothing used to read this. ``page.goto`` returns the response and the
    return value was dropped on the floor, so *every* HTTP failure arrived at
    the adapters as an ordinary page that simply had no form on it — and the
    adapters duly reported ``no_form``, "No form controls on the page", which
    is terminal, unretryable and in four of the five cases untrue:

    * **503 / 502 / 504** — the ATS was down for ninety seconds. This is the
      textbook retryable failure, the retry machinery was already built for it,
      and the classification bypassed it: a blip retired the application
      permanently.
    * **429** — we were being rate limited, and the answer was to keep going at
      the same rate against the next posting.
    * **404 / 410** — the job was taken down. Indistinguishable, to the
      candidate reading the run, from a page we failed to parse.
    * **403** — the site blocked the automated browser. Also indistinguishable,
      and also not something a retry fixes.

    Only the last case, a 200 with nothing fillable on it, was ever really
    ``no_form``.

    A ``None`` response is not a failure: Playwright returns one for a
    same-document navigation, and the fake page in the tests returns one for
    every navigation, so "no response object" has to mean "carry on".
    """
    status = getattr(response, "status", None)
    if not isinstance(status, int) or status < 400:
        _check_content_type(response, url)
        return

    if status in RATE_LIMIT_STATUSES:
        raise RateLimited(
            f"{url} answered {status} — the site is rate-limiting us. "
            "The run will be tried again shortly."
        )
    if status in GONE_STATUSES:
        raise PostingGone(
            f"{url} answered {status} — this posting has been taken down."
        )
    if status in BLOCKED_STATUSES:
        raise ApplyBlocked(
            f"{url} answered {status} — the site blocked the automated browser. "
            "Open the link yourself to apply."
        )
    if status in RETRYABLE_STATUSES:
        raise TransientBrowserError(f"{url} answered {status}")
    # An unlisted 4xx/5xx: 5xx is the server's, so it may pass; 4xx is ours,
    # and repeating it will get the same answer.
    if status >= 500:
        raise TransientBrowserError(f"{url} answered {status}")
    raise ApplyBlocked(f"{url} answered {status} and did not serve the form.")


def _check_content_type(response, url: str) -> None:
    """Refuse a 200 that is not a web page.

    An application link that answers with JSON, a PDF or a zip is a link to
    something that is not a form, and Chromium will render it as text or hand
    it to a download. Either way the run reads zero controls and reports
    ``no_form`` — true, but it does not say *why*, and "the link returns
    application/json" is the one fact that would let somebody fix it.
    """
    content_type = _header(response, "content-type")
    if not content_type:
        return
    if any(kind in content_type for kind in _PAGE_CONTENT_TYPES):
        return
    raise ApplyBlocked(
        f"{url} returned {content_type.split(';')[0]}, not a web page — "
        "this link doesn't lead to an application form."
    )


def goto(page, url: str, *, timeout_ms: int | None = None) -> None:
    """Navigate, and classify what came back.

    Navigation *errors* are transient by default — a timeout, a reset
    connection, a truncated body — with one exception carved out: a redirect
    loop is a property of the site, so retrying it burns the run's attempts and
    its backoff to arrive at the identical failure three times.
    """
    try:
        kwargs: dict[str, Any] = {"wait_until": "domcontentloaded"}
        if timeout_ms:
            kwargs["timeout"] = timeout_ms
        response = page.goto(url, **kwargs)
    except Exception as exc:  # noqa: BLE001 - classified below
        message = str(exc).lower()
        if any(marker in message for marker in _REDIRECT_LOOP_MARKERS):
            raise ApplyBlocked(
                f"{url} redirects in a loop and never reaches a form. "
                "Open the link yourself to check where it goes."
            ) from exc
        raise TransientBrowserError(f"could not open {url}: {exc}") from exc

    check_response(response, url)


def dismiss_cookies(form: Form) -> None:
    """Click a cookie banner out of the way if one is covering the form.

    Only ever the banner's own accept control, and only because an overlay makes
    every subsequent click land on the overlay instead.
    """
    form.click(list(_COOKIE_SELECTORS))


def hit_a_wall(form: Form, *, extra: tuple[str, ...] = ()) -> str | None:
    """The reason this page needs a human, or None if it doesn't.

    *extra* carries the markers that only mean "wall" on one platform — iCIMS
    greets returning candidates by name, and that greeting is the sign-in page.
    """
    text = form.text()
    if not text:
        return None
    for marker in (*_WALL_MARKERS, *extra):
        if marker in text:
            return marker
    return None


def throttled(form: Form) -> str | None:
    """The phrase by which this page is telling us to slow down, or None."""
    text = form.text()
    if not text:
        return None
    return next((m for m in _THROTTLE_MARKERS if m in text), None)


def guard_page(form: Form, url: str) -> None:
    """Raise if the page we landed on is a throttle rather than an application.

    Called after the cookie banner is out of the way and before anything is
    typed. The status check in :func:`goto` catches the 429; this catches the
    same message delivered with a 200, which is what most CDNs in front of an
    ATS actually do.
    """
    marker = throttled(form)
    if marker is not None:
        raise RateLimited(
            f"{url} is throttling us (\"{marker}\"). The run will be tried "
            "again shortly."
        )


def submission_error(form: Form) -> str | None:
    """What the page said went wrong after submit, or None if it didn't.

    Returns the employer's own sentence around the marker, the same way
    :func:`confirmed` does, because "the page came back with an error" is not
    something a candidate can act on and "Please correct the errors below:
    Phone number is not valid" is.
    """
    raw = form.raw_text()
    text = raw.lower()
    for marker in _SUBMIT_ERROR_MARKERS:
        found = text.find(marker)
        if found < 0:
            continue
        start = max(0, found - 40)
        return " ".join(raw[start : found + 200].split()).strip()
    return None


def fillable(fields: list[FormField]) -> list[FormField]:
    """The controls worth touching — real inputs with some handle on them."""
    return [
        f
        for f in fields
        if f.field_type not in _NON_INPUT_TYPES and f.selector is not None
    ]


def questions_from(fields: list[FormField], skip: set[int]) -> list[ScreeningQuestion]:
    """Turn the controls nothing else claimed into screening questions.

    A control with no label and no name isn't a question, it's a widget — asking
    a model about ``input#\\_r_1a`` produces confident nonsense, so it is left
    alone.
    """
    out: list[ScreeningQuestion] = []
    for control in fields:
        if control.index in skip or control.field_type == "file":
            continue
        text = (control.label or control.placeholder or control.name or "").strip()
        if len(text) < 3:
            continue
        out.append(
            ScreeningQuestion(
                text=text,
                field_type=(
                    "select" if control.tag == "select" else control.field_type or "text"
                ),
                options=tuple(control.options or ()),
                required=control.required,
                key=str(control.index),
            )
        )
    return out


def upload_resume(form: Form, fields: list[FormField], ctx: ApplyContext) -> bool:
    """Attach the resume to the first file input that looks like it wants one."""
    if not ctx.resume_path:
        return False
    file_fields = [f for f in fields if f.field_type == "file"]
    if not file_fields:
        return False
    # Prefer a field that says "resume"/"cv" over, say, a cover-letter upload.
    preferred = next(
        (f for f in file_fields if any(h in f.haystack for h in ("resume", "cv"))),
        file_fields[0],
    )
    return form.upload(preferred, ctx.resume_path)


def fill_step(form: Form, ctx: ApplyContext) -> FillOutcome:
    """Fill one page (or one wizard step) as far as it can honestly be filled.

    Profile fields first (deterministic), then the resume upload, then whatever
    is left goes through the answering engine. Returns what happened, including
    the required questions that came back unanswered — the caller decides
    whether that is fatal.
    """
    outcome = FillOutcome()
    controls = fillable(form.fields())
    outcome.control_count = len(controls)
    if not controls:
        return outcome

    by_index = {control.index: control for control in controls}

    # 1. What the resume already knows: name, email, phone, links.
    claimed: set[int] = set()
    for plan in plan_fills(controls, ctx.applicant):
        if form.fill(plan.field, plan.value):
            outcome.filled.append(plan.attr)
            claimed.add(plan.field.index)

    # 2. The resume file itself.
    outcome.resume_uploaded = upload_resume(form, controls, ctx)

    # 3. Everything else is a question.
    questions = questions_from(controls, claimed)
    if not questions:
        return outcome

    answers = answer_questions(
        questions,
        bank=ctx.bank,
        resume=ctx.resume,
        job_title=ctx.job_title,
        company=ctx.company,
        completion=ctx.completion,
    )
    # A value the control refused is not an answer on the form.
    #
    # `blocking_questions` reads `Answer.answered`, which says only that a
    # value was *computed*. Whether it reached the page is a different fact,
    # and it used to be recorded in a note that nothing read — so a required
    # question whose write failed left `blocking` empty, the caller saw a
    # clean fill, and the application was submitted with that field blank
    # while `FormApplication.answers` recorded the value as filled in. A
    # wrong audit trail on a submitted application is worse than either
    # failure alone: the run cannot be re-tried, because nothing says it went
    # out incomplete.
    #
    # Writes fail for ordinary reasons, not exotic ones. `Form.fill` swallows
    # every exception Playwright raises, and Playwright refuses to type into
    # an `<input type="number">` a value that is not a number — which is what
    # a bank answer like "$180,000" is. A disabled or detached control, and a
    # `choose` whose option list moved under us, land in the same place.
    unwritten: list[str] = []
    for answer in answers:
        control = by_index.get(int(answer.question.key or -1))
        if control is None or not answer.answered:
            continue
        written = (
            form.choose(control, answer.value)
            if (control.tag == "select" or control.options)
            else form.fill(control, answer.value)
        )
        if written:
            continue
        answer.note = (answer.note or "") + " (could not write to the control)"
        if answer.question.required:
            unwritten.append(answer.question.text)

    outcome.answers = answers
    outcome.blocking = blocking_questions(answers)
    outcome.blocking.extend(q for q in unwritten if q not in outcome.blocking)
    return outcome


def confirmed(form: Form) -> str | None:
    """What the page said when it acknowledged the application, or None.

    The employer's own words are the strongest evidence a run can carry — a
    screenshot shows a page, this shows the sentence on it — so the surrounding
    text is kept, not just the marker that matched. Truthy/falsy either way, so
    callers reading it as "did it confirm" still read correctly.
    """
    # Matched against the lowercased text so a page shouting "APPLICATION
    # RECEIVED" still matches, but sliced out of the original so what gets
    # stored is the sentence the employer actually wrote. The two strings are
    # the same length, so an offset found in one indexes the other.
    raw = form.raw_text()
    text = raw.lower()
    for marker in _CONFIRMATION_MARKERS:
        found = text.find(marker)
        if found < 0:
            continue
        # A window around the match: enough to be quotable, bounded so a page
        # that repeats its whole body into one <div> can't fill the column.
        start = max(0, found - 40)
        return " ".join(raw[start : found + 200].split()).strip()
    return None


def unanswered(outcomes: list[FillOutcome]) -> list[str]:
    """The questions still needing the candidate, each named once.

    One *question* reaches `fill_step` as several controls and, on a wizard,
    across several steps. A radio group is the everyday case: each `<input>`
    is its own control carrying the fieldset's label, so "Are you legally
    authorized to work?" arrives three times and comes back unanswered three
    times.

    `fold_result` had always deduped; `finish_run` built its own flat list and
    had not. The two ran on the same outcomes, one after the other, and
    disagreed — the row stored that one question while the sentence shown to
    the user counted three and then printed it three times, which reads as a
    broken run rather than as one box to go and tick.

    Order is preserved — first mention wins — because it is the order of the
    form, which is the order the candidate will walk it in.
    """
    out: list[str] = []
    for outcome in outcomes:
        out.extend(q for q in outcome.blocking if q not in out)
    return out


def fold_result(
    status: str,
    outcomes: list[FillOutcome],
    *,
    note: str | None = None,
    confirmation: str | None = None,
) -> AdapterResult:
    """Fold one or more step outcomes into the result the service stores."""
    filled: list[str] = []
    answers: list[dict] = []
    uploaded = False
    for outcome in outcomes:
        filled.extend(a for a in outcome.filled if a not in filled)
        answers.extend(a.as_dict() for a in outcome.answers)
        uploaded = uploaded or outcome.resume_uploaded
    return AdapterResult(
        status=status,
        filled_fields=filled,
        answers=answers,
        unanswered=unanswered(outcomes),
        resume_uploaded=uploaded,
        note=note,
        confirmation=confirmation,
    )


def finish_run(
    form: Form,
    page,
    ctx: ApplyContext,
    outcomes: list[FillOutcome],
    *,
    submit_selectors: tuple[str, ...] = _SUBMIT_SELECTORS,
) -> AdapterResult:
    """The shared ending: stop at the button, or press it."""
    blocking = unanswered(outcomes)
    if blocking:
        ctx.record(page, "needs input", note=f"{len(blocking)} unanswered")
        return fold_result(
            "needs_input",
            outcomes,
            note=(
                f"{len(blocking)} required question(s) need your answer: "
                + "; ".join(blocking[:3])
            ),
        )

    if not ctx.submit:
        ctx.record(page, "filled")
        return fold_result("filled", outcomes, note="Filled, not submitted")

    clicked = form.click(list(submit_selectors))
    if clicked is None:
        ctx.record(page, "no submit button")
        return fold_result(
            "filled", outcomes, note="Filled, but no submit button was found"
        )

    form.settle()
    acknowledged = confirmed(form)

    # Three readings of the page after the click, and only the first two used to
    # exist. An acknowledgement is a submission; silence is a submission we
    # cannot prove. The third — the page saying, in as many words, that the
    # application did not go — was being read as the second, and reported as
    # `submitted`.
    #
    # Precedence matters and is deliberate: an acknowledgement wins. A
    # confirmation page can carry the word "problem" in a support link at the
    # foot of it, and a page that has told the candidate their application was
    # received has told them the truth whatever else is on it.
    if acknowledged is None:
        wall = hit_a_wall(form)
        if wall is not None:
            # A captcha *on the submit* means the click did not submit anything.
            # Reported as needs_input rather than failed, because no number of
            # retries gets a bot past it.
            ctx.record(page, "blocked at submit", note=wall)
            return fold_result(
                "needs_input",
                outcomes,
                note=(
                    f"The form was filled, but submitting it hit a human check "
                    f"({wall}). Open the link and press submit yourself — "
                    "everything is already filled in."
                ),
            )
        problem = submission_error(form)
        if problem is not None:
            ctx.record(page, "submit rejected", note=problem[:120])
            return fold_result(
                "failed",
                outcomes,
                note=f"The form was submitted but the page rejected it: {problem}",
            )

    # Screenshotted *after* settling on the confirmation page, so the picture on
    # the receipt is the employer's acknowledgement rather than the form.
    ctx.record(page, "submitted", note="confirmation seen" if acknowledged else None)
    return fold_result(
        "submitted",
        outcomes,
        note=(
            "Submitted — the page confirmed it"
            if acknowledged
            else "Submitted — no confirmation text on the page, see the screenshot"
        ),
        confirmation=acknowledged,
    )


# --------------------------------------------------------------------------- #
# Adapters                                                                     #
# --------------------------------------------------------------------------- #


class GreenhouseAdapter:
    """One page, one form — but usually inside the company's own careers site.

    Also the base for every other single-page ATS, which differ only in four
    declared ways: where the form lives (``frame_hint``), whether the posting
    URL has to be corrected to reach it (``ats_platform.apply_url``), whether a
    button has to be pressed to reveal it (``open_selectors``), and what that
    platform's login page says (``wall_markers``). Anything needing more than
    that is not a single-page ATS — see :class:`WorkdayAdapter`.
    """

    platform = ATSPlatform.GREENHOUSE
    frame_hint = "greenhouse"
    # Controls that reveal a form the posting page keeps behind a button.
    open_selectors: tuple[str, ...] = ()
    # Wall phrasings specific to this platform, on top of the shared ones.
    wall_markers: tuple[str, ...] = ()
    submit_selectors: tuple[str, ...] = _SUBMIT_SELECTORS

    def run(self, page, ctx: ApplyContext) -> AdapterResult:
        # The posting page is not always the form page — Lever's is at /apply,
        # Ashby's at /application.
        target = ats_platform.apply_url(ctx.url, self.platform)
        if target != ctx.url:
            ctx = _with_url(ctx, target)

        goto(page, ctx.url)
        form = Form(scope_for(page, self.frame_hint))
        dismiss_cookies(form)
        guard_page(form, ctx.url)
        if self.open_form(form):
            # The click may have swapped the iframe (iCIMS) or navigated, so
            # the scope we read the controls from has to be found again.
            form = Form(scope_for(page, self.frame_hint))
        ctx.record(page, "opened")

        wall = hit_a_wall(form, extra=self.wall_markers)
        if wall:
            return AdapterResult("needs_input", note=f"The page asked for a human ({wall})")

        outcome = fill_step(form, ctx)
        if outcome.control_count == 0:
            ctx.record(page, "no form")
            return AdapterResult("no_form", note="No form controls on the page")

        ctx.record(page, "application form")
        return finish_run(
            form, page, ctx, [outcome], submit_selectors=self.submit_selectors
        )

    def open_form(self, form: Form) -> bool:
        """Press the platform's "Apply" control. True when one was there.

        Never fatal: on the platforms that go straight to a form URL there is
        no such button, and the form is already on the page.
        """
        if not self.open_selectors:
            return False
        if form.click(list(self.open_selectors)) is None:
            return False
        form.settle()
        return True


class LeverAdapter(GreenhouseAdapter):
    """Same single-page shape as Greenhouse, at a URL we have to correct first."""

    platform = ATSPlatform.LEVER
    frame_hint = "lever.co"


class AshbyAdapter(GreenhouseAdapter):
    """Ashby: ``/application`` if the URL allows it, the button if it doesn't.

    Ashby postings shared in the wild come in both shapes — the posting page
    and the application page — and the posting page renders the form inline
    once "Apply for this Job" is pressed rather than navigating. Correcting the
    URL handles the first, pressing the button handles the second, and doing
    both costs one click that finds nothing.
    """

    platform = ATSPlatform.ASHBY
    frame_hint = "ashbyhq"
    open_selectors = (
        "button:has-text('Apply for this Job')",
        "a:has-text('Apply for this Job')",
        "[data-testid='apply-button']",
    )


class IcimsAdapter(GreenhouseAdapter):
    """iCIMS: an iframe on the company's page, usually behind an account.

    Two things make iCIMS its own adapter. The form is inside
    ``icims_content_iframe`` on the employer's own domain, so the controls are
    never in the top document; and most tenants route "Apply" through a
    candidate portal that wants an account before it wants an application. We
    never create one — the run stops as ``needs_input`` and says so, which is
    honest and, unlike typing into a login page, costs the candidate nothing.
    """

    platform = ATSPlatform.ICIMS
    frame_hint = "icims"
    open_selectors = (
        "a:has-text('Apply for this job online')",
        "button:has-text('Apply for this job online')",
        "#quickApplyBtn",
        "a:has-text('Apply Now')",
        "button:has-text('Apply Now')",
    )
    wall_markers = (
        "returning candidate",
        "create an account",
        "log back in",
    )


class GenericAdapter(GreenhouseAdapter):
    """Best effort on a careers page we don't recognise."""

    platform = ATSPlatform.GENERIC
    frame_hint = "apply"


class WorkdayAdapter:
    """Workday's multi-step wizard.

    The loop is the adapter: fill the step, screenshot it, press Continue, and
    do it again until the page offers a submit button or stops advancing.
    ``MAX_STEPS`` is a stop rather than a target — a wizard that never stops
    advancing is a wizard we have misread, and going round forever with a real
    employer's form is the worst available outcome.
    """

    platform = ATSPlatform.WORKDAY
    MAX_STEPS = 8

    def run(self, page, ctx: ApplyContext) -> AdapterResult:
        goto(page, ctx.url)
        form = Form(page)
        dismiss_cookies(form)
        guard_page(form, ctx.url)
        ctx.record(page, "opened")

        # Workday tenants front the form with "Apply Manually" / "Autofill with
        # Resume". Manual is the one whose fields we can read.
        form.click(
            [
                '[data-automation-id="applyManually"]',
                "button:has-text('Apply Manually')",
                "a:has-text('Apply Manually')",
                '[data-automation-id="adventureButton"]',
            ]
        )
        form.settle()

        wall = self._sign_in_wall(form)
        if wall is not None:
            ctx.record(page, "sign-in wall")
            return AdapterResult("needs_input", note=wall)

        outcomes: list[FillOutcome] = []
        for step in range(1, self.MAX_STEPS + 1):
            # Checked before the step rather than after, so the run never
            # starts filling a page it has no time to finish and press
            # Continue on. ``MAX_STEPS`` bounds how many steps a wizard may
            # have; this bounds how long they may take, which is the other
            # half and the one that was missing — eight steps of a dozen
            # controls at five seconds apiece has no useful ceiling.
            if ctx.out_of_time():
                ctx.record(page, f"step {step} skipped", note="out of time")
                return fold_result(
                    "needs_input",
                    outcomes,
                    note=(
                        ctx.deadline.note()
                        + " Everything filled so far is in the screenshots; "
                        "finish it on the employer's site."
                    ),
                )

            outcome = fill_step(form, ctx)
            outcomes.append(outcome)
            ctx.record(page, f"step {step}", note=f"{outcome.control_count} controls")

            if outcome.blocking:
                return finish_run(form, page, ctx, outcomes)

            # The last step is the one offering a submit button.
            if self._at_submit(form):
                return finish_run(form, page, ctx, outcomes)

            if form.click(list(_NEXT_SELECTORS)) is None:
                break
            form.settle()

            wall = self._sign_in_wall(form)
            if wall is not None:
                ctx.record(page, "sign-in wall")
                return fold_result("needs_input", outcomes, note=wall)

        return finish_run(form, page, ctx, outcomes)

    @staticmethod
    def _sign_in_wall(form: Form) -> str | None:
        """Workday tenants require a candidate account. We never create one."""
        for selector in (
            '[data-automation-id="signInLink"]',
            '[data-automation-id="createAccountLink"]',
            '[data-automation-id="createAccountCheckbox"]',
        ):
            if form.visible(selector):
                return (
                    "This Workday site needs a candidate account. Create one on "
                    "the employer's site, then re-run — your details will be "
                    "filled in for you."
                )
        wall = hit_a_wall(form)
        return f"The page asked for a human ({wall})" if wall else None

    @staticmethod
    def _at_submit(form: Form) -> bool:
        return any(form.visible(selector) for selector in _SUBMIT_SELECTORS[:3])


def _with_url(ctx: ApplyContext, url: str) -> ApplyContext:
    """A copy of *ctx* pointed at a different URL (Lever's ``/apply`` page)."""
    return replace(ctx, url=url)


_ADAPTERS: dict[ATSPlatform, type] = {
    ATSPlatform.GREENHOUSE: GreenhouseAdapter,
    ATSPlatform.LEVER: LeverAdapter,
    ATSPlatform.ASHBY: AshbyAdapter,
    ATSPlatform.ICIMS: IcimsAdapter,
    ATSPlatform.WORKDAY: WorkdayAdapter,
    ATSPlatform.GENERIC: GenericAdapter,
}


def adapter_for(platform: ATSPlatform):
    """The adapter instance for *platform*, or None when there isn't one.

    LinkedIn is deliberately absent: Easy Apply needs a signed-in session, so it
    lives with the credential handling in :mod:`app.services.linkedin_service`.
    """
    cls = _ADAPTERS.get(platform)
    return cls() if cls is not None else None


__all__ = [
    "BLOCKED_STATUSES",
    "GONE_STATUSES",
    "RATE_LIMIT_STATUSES",
    "RETRYABLE_STATUSES",
    "AdapterResult",
    "ApplyContext",
    "AshbyAdapter",
    "GenericAdapter",
    "GreenhouseAdapter",
    "IcimsAdapter",
    "LeverAdapter",
    "WorkdayAdapter",
    "adapter_for",
    "check_response",
    "confirmed",
    "fill_step",
    "fillable",
    "goto",
    "guard_page",
    "hit_a_wall",
    "questions_from",
    "submission_error",
    "throttled",
    "unanswered",
    "upload_resume",
]
