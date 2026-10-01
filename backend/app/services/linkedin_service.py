"""LinkedIn: credentials, the Easy Apply budget, and the Easy Apply flow.

LinkedIn publishes no application API, so Easy Apply is a browser flow driven as
the candidate. That makes this the most safety-sensitive module in the product,
and the constraints are deliberate rather than incidental:

* **The password is the candidate's own.** It is Fernet-encrypted at rest
  (:mod:`app.services.crypto`), decrypted only inside the worker that is about
  to type it into LinkedIn's own login form, and never returned by the API or
  written to a log. A successful login's cookies are stored instead, so the
  steady state is session reuse.
* **A challenge stops the run.** 2FA, a CAPTCHA or an "unusual activity" screen
  sets the account to ``challenge_required`` and returns. Nothing here attempts
  to solve or evade one — the candidate signs in themselves and the automation
  picks their session back up.
* **25 applications a day, paced.** ``settings.linkedin_easy_apply_daily_limit``
  is a rolling-24h ceiling and there is a minimum gap between applications.
  LinkedIn restricts accounts that apply at machine speed, and the account it
  restricts belongs to the candidate, not to us.
* **Automation is off by default.** Both the LinkedIn job source and Easy Apply
  are opt-in settings. LinkedIn's User Agreement prohibits automated access;
  turning these on is the deployment's decision to make, knowingly.

Job discovery from LinkedIn uses their public *guest* job listing endpoint — the
one an unauthenticated browser gets — and only the fields a listing shows.
:func:`parse_guest_jobs` is pure so it can be tested against a saved fixture.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.form_apply import ATSPlatform
from app.models.linkedin_account import LinkedInAccount
from app.models.user import User
from app.services import crypto
from app.services.ats_adapters import (
    AdapterResult,
    ApplyContext,
    FillOutcome,
    dismiss_cookies,
    fill_step,
    finish_run,
    fold_result,
    goto,
)
from app.services.browser_runner import Form, TransientBrowserError

logger = logging.getLogger(__name__)

GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
JOB_VIEW_URL = "https://www.linkedin.com/jobs/view/{job_id}/"
LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"

# Signs that LinkedIn wants the human, not the automation. Any of these ends the
# run — see the module docstring.
_CHALLENGE_MARKERS = (
    "checkpoint",
    "security verification",
    "verification code",
    "unusual activity",
    "let's do a quick security check",
    "captcha",
    "puzzle",
)
_BAD_CREDENTIAL_MARKERS = (
    "wrong email or password",
    "couldn't find a linkedin account",
    "that's not the right password",
    "please enter a valid email",
)


# --------------------------------------------------------------------------- #
# Credentials                                                                  #
# --------------------------------------------------------------------------- #


def get_account(db: Session, user: User) -> LinkedInAccount | None:
    return db.scalar(
        select(LinkedInAccount).where(LinkedInAccount.user_id == user.id)
    )


def store_credentials(
    db: Session, user: User, *, email: str, password: str
) -> LinkedInAccount:
    """Save (or replace) the candidate's LinkedIn credentials, encrypted.

    Replacing the password always clears the stored session: the old cookies
    belong to a login we can no longer reproduce, and keeping them around means
    a stale session failing in a way that looks like bad credentials.
    """
    account = get_account(db, user)
    encrypted = crypto.encrypt(password)
    if account is None:
        account = LinkedInAccount(
            user_id=user.id, email=email, password_encrypted=encrypted
        )
        db.add(account)
    else:
        account.email = email
        account.password_encrypted = encrypted
        account.session_state_encrypted = None
        account.session_saved_at = None
    account.status = "connected"
    account.last_error = None
    db.commit()
    db.refresh(account)
    return account


def clear_credentials(db: Session, user: User) -> bool:
    """Forget everything we hold about this user's LinkedIn account."""
    account = get_account(db, user)
    if account is None:
        return False
    db.delete(account)
    db.commit()
    return True


def password_for(account: LinkedInAccount) -> str:
    """Decrypt the stored password. Called only by the worker, at login time."""
    return crypto.decrypt(account.password_encrypted)


def session_state(account: LinkedInAccount) -> dict | None:
    """The stored Playwright storage state, if the last login left one."""
    if not account.session_state_encrypted:
        return None
    try:
        return json.loads(crypto.decrypt(account.session_state_encrypted))
    except (crypto.TokenCryptoError, ValueError) as exc:
        logger.info("stored LinkedIn session unusable: %s", exc)
        return None


def save_session(db: Session, account: LinkedInAccount, state: dict | None) -> None:
    """Persist a fresh session so the password isn't needed next time."""
    account.session_state_encrypted = (
        crypto.encrypt(json.dumps(state)) if state else None
    )
    account.session_saved_at = datetime.now(UTC) if state else None
    db.commit()


def set_status(
    db: Session, account: LinkedInAccount, status: str, *, error: str | None = None
) -> None:
    account.status = status
    account.last_error = (error or None) and error[:500]
    db.commit()


# --------------------------------------------------------------------------- #
# The daily budget                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BudgetDecision:
    """Whether another Easy Apply may go out right now."""

    allowed: bool
    used: int
    limit: int
    reason: str | None = None
    retry_after_seconds: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
        }


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips naive datetimes; this arithmetic needs tz-aware ones."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def apply_budget(
    account: LinkedInAccount | None, *, now: datetime | None = None
) -> BudgetDecision:
    """How much Easy Apply headroom this account has left.

    The window is rolling rather than calendar-day: 25 applications at 23:50
    followed by 25 more at 00:05 is exactly the burst the ceiling exists to
    prevent.
    """
    limit = settings.linkedin_easy_apply_daily_limit
    if account is None:
        return BudgetDecision(False, 0, limit, reason="No LinkedIn account connected")
    if not account.is_usable:
        return BudgetDecision(
            False, account.daily_apply_count, limit, reason=_status_reason(account)
        )

    now = now or datetime.now(UTC)
    used = account.daily_apply_count or 0
    reset_at = _aware(account.daily_count_reset_at)
    if reset_at is None or now - reset_at >= timedelta(hours=24):
        used = 0  # the window has rolled over

    if used >= limit:
        elapsed = (now - reset_at).total_seconds() if reset_at else 0
        return BudgetDecision(
            False,
            used,
            limit,
            reason=f"LinkedIn's daily cap of {limit} Easy Applies is used up",
            retry_after_seconds=max(0, int(86400 - elapsed)),
        )

    gap = settings.linkedin_min_seconds_between_applies
    last = _aware(account.last_apply_at)
    if last is not None and (now - last).total_seconds() < gap:
        wait = int(gap - (now - last).total_seconds())
        return BudgetDecision(
            False,
            used,
            limit,
            reason="Pacing — applying again too soon looks automated",
            retry_after_seconds=wait,
        )

    return BudgetDecision(True, used, limit)


def record_apply(
    db: Session, account: LinkedInAccount, *, now: datetime | None = None
) -> None:
    """Count one submitted Easy Apply against the rolling window."""
    now = now or datetime.now(UTC)
    reset_at = _aware(account.daily_count_reset_at)
    if reset_at is None or now - reset_at >= timedelta(hours=24):
        account.daily_apply_count = 0
        account.daily_count_reset_at = now
    account.daily_apply_count = (account.daily_apply_count or 0) + 1
    account.easy_apply_total = (account.easy_apply_total or 0) + 1
    account.last_apply_at = now
    db.commit()


def _status_reason(account: LinkedInAccount) -> str:
    return {
        "challenge_required": (
            "LinkedIn asked for a security check. Sign in on linkedin.com "
            "yourself, then reconnect here."
        ),
        "invalid_credentials": "LinkedIn rejected the saved password.",
        "disabled": "LinkedIn automation is switched off for this account.",
    }.get(account.status, "LinkedIn account is not usable right now")


def status_summary(account: LinkedInAccount | None) -> dict:
    """The account's state as the UI shows it — never including the password."""
    budget = apply_budget(account)
    if account is None:
        return {
            "connected": False,
            "email": None,
            "status": "not_connected",
            "enabled": settings.linkedin_easy_apply_enabled,
            "budget": budget.as_dict(),
        }
    return {
        "connected": True,
        "email": account.email,
        "status": account.status,
        "enabled": settings.linkedin_easy_apply_enabled,
        "has_session": bool(account.session_state_encrypted),
        "session_saved_at": (
            account.session_saved_at.isoformat() if account.session_saved_at else None
        ),
        "easy_apply_total": account.easy_apply_total,
        "last_apply_at": (
            account.last_apply_at.isoformat() if account.last_apply_at else None
        ),
        "last_error": account.last_error,
        "budget": budget.as_dict(),
    }


# --------------------------------------------------------------------------- #
# Job discovery (public guest endpoint)                                        #
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")


def _text(node) -> str | None:
    if node is None:
        return None
    value = _WS_RE.sub(" ", node.get_text(" ", strip=True))
    return value.strip() or None


def parse_guest_jobs(html: str) -> list[dict]:
    """Parse LinkedIn's guest job-card HTML into plain dicts.

    Pure, so it is tested against a saved fixture rather than the live site.
    Returns only what a listing card shows: title, company, location, link and
    the posting date. No member profiles, no data behind the login.
    """
    if not html or not html.strip():
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a hard dependency
        logger.warning("beautifulsoup4 is not installed; LinkedIn parsing skipped")
        return []

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for card in soup.select("li, div.base-card"):
        link = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
        title = _text(card.select_one(".base-search-card__title, h3"))
        if link is None or not title:
            continue
        href = str(link.get("href") or "").split("?")[0]
        if "/jobs/view/" not in href:
            continue
        time_node = card.select_one("time")
        out.append(
            {
                "title": title,
                "company": _text(
                    card.select_one(".base-search-card__subtitle, h4")
                ),
                "location": _text(card.select_one(".job-search-card__location")),
                "url": href,
                "posted_at": (time_node.get("datetime") if time_node else None),
                "job_id": job_id_from_url(href),
            }
        )
    return out


_JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/]*?-)?(\d{6,})")


def job_id_from_url(url: str | None) -> str | None:
    """LinkedIn's numeric job id, which their URLs bury after the slug."""
    if not url:
        return None
    match = _JOB_ID_RE.search(url)
    return match.group(1) if match else None


def canonical_job_url(url: str) -> str:
    """The plain ``/jobs/view/<id>/`` URL, free of tracking parameters."""
    job_id = job_id_from_url(url)
    return JOB_VIEW_URL.format(job_id=job_id) if job_id else url


def search_params(
    *, keywords: str, location: str | None, remote_only: bool, start: int = 0
) -> dict:
    """Query parameters for the guest search endpoint."""
    params: dict[str, str | int] = {"keywords": keywords, "start": start}
    if location:
        params["location"] = location
    if remote_only:
        params["f_WT"] = 2  # LinkedIn's "remote" workplace-type filter
    return params


# --------------------------------------------------------------------------- #
# Easy Apply                                                                   #
# --------------------------------------------------------------------------- #


class LinkedInChallenge(RuntimeError):
    """LinkedIn asked for a security check. A human has to answer it."""


class LinkedInCredentialsRejected(RuntimeError):
    """LinkedIn refused the stored password."""


def _looks_signed_out(form: Form) -> bool:
    return form.visible("#username") or form.visible("input[name='session_key']")


def _challenge_reason(page, form: Form) -> str | None:
    url = (getattr(page, "url", "") or "").lower()
    text = form.text()
    for marker in _CHALLENGE_MARKERS:
        if marker in url or marker in text:
            return marker
    return None


def ensure_login(page, account: LinkedInAccount, password: str) -> None:
    """Make sure the page is signed in as *account*, typing the password if needed.

    Raises :class:`LinkedInChallenge` on a security check and
    :class:`LinkedInCredentialsRejected` on a bad password. Neither is retried:
    both need the candidate, and hammering either is exactly what gets an account
    restricted.
    """
    form = Form(page)
    goto(page, FEED_URL)
    dismiss_cookies(form)
    if not _looks_signed_out(form):
        return  # the stored session is still good

    goto(page, LOGIN_URL)
    form = Form(page)
    dismiss_cookies(form)

    try:
        page.fill("#username", account.email, timeout=10000)
        page.fill("#password", password, timeout=10000)
        page.click("button[type='submit']", timeout=10000)
    except Exception as exc:  # noqa: BLE001 - a login page we can't drive
        raise TransientBrowserError(f"LinkedIn login form unusable: {exc}") from exc

    form.settle(3000)

    challenge = _challenge_reason(page, form)
    if challenge:
        raise LinkedInChallenge(
            "LinkedIn asked for a security check "
            f"({challenge}). Sign in at linkedin.com in your own browser, then "
            "reconnect the account here."
        )
    text = form.text()
    if any(marker in text for marker in _BAD_CREDENTIAL_MARKERS):
        raise LinkedInCredentialsRejected("LinkedIn rejected the saved password.")
    if _looks_signed_out(form):
        raise TransientBrowserError("LinkedIn login did not complete")


class EasyApplyAdapter:
    """Drives LinkedIn's Easy Apply modal, one step at a time.

    Structurally the same wizard as Workday's — fill, screenshot, advance —
    with two LinkedIn-specific manners: the "follow this company" box is
    unticked (applying is not following), and a job without an Easy Apply button
    is reported as such rather than half-applied to on the employer's own site.
    """

    platform = ATSPlatform.LINKEDIN
    MAX_STEPS = 8

    _EASY_APPLY = (
        "button.jobs-apply-button",
        "button:has-text('Easy Apply')",
        "[data-live-test-job-apply-button]",
    )
    _NEXT = (
        "button[aria-label='Continue to next step']",
        "button[aria-label='Review your application']",
        "button:has-text('Next')",
        "button:has-text('Review')",
    )
    _SUBMIT = (
        "button[aria-label='Submit application']",
        "button:has-text('Submit application')",
    )
    # The real control, which holds the state whether or not it is the thing
    # the page lets you click.
    _FOLLOW_INPUT = "input#follow-company-checkbox"
    _FOLLOW = (
        _FOLLOW_INPUT,
        "label[for='follow-company-checkbox']",
    )

    def __init__(self, account: LinkedInAccount, password: str) -> None:
        self.account = account
        self.password = password

    def run(self, page, ctx: ApplyContext) -> AdapterResult:
        ensure_login(page, self.account, self.password)
        ctx.record(page, "signed in")

        goto(page, canonical_job_url(ctx.url))
        form = Form(page)
        dismiss_cookies(form)
        ctx.record(page, "job page")

        if form.click(list(self._EASY_APPLY)) is None:
            return AdapterResult(
                "no_form",
                note=(
                    "This job has no Easy Apply — it applies on the company's own "
                    "site. Try Form apply against the employer's link instead."
                ),
            )
        form.settle(1500)
        self._untick_follow(form)

        outcomes: list[FillOutcome] = []
        for step in range(1, self.MAX_STEPS + 1):
            outcome = fill_step(form, ctx)
            outcomes.append(outcome)
            ctx.record(page, f"easy apply step {step}",
                       note=f"{outcome.control_count} controls")

            if outcome.blocking:
                return fold_result(
                    "needs_input",
                    outcomes,
                    note=(
                        f"{len(outcome.blocking)} question(s) need your answer: "
                        + "; ".join(outcome.blocking[:3])
                    ),
                )

            if any(form.visible(selector) for selector in self._SUBMIT):
                return self._submit(form, page, ctx, outcomes)

            if form.click(list(self._NEXT)) is None:
                break
            form.settle(1200)

        return finish_run(form, page, ctx, outcomes)

    def _untick_follow(self, form: Form) -> None:
        """Applying to a job is not the same as following the company.

        The box is a toggle, so its current state has to be read before it is
        clicked: LinkedIn ticks it by default, but a candidate who has already
        turned the default off would have had it clicked back *on* by a blind
        click — this method doing the exact thing it exists to prevent, on the
        candidate's real LinkedIn profile.

        The state is read off the input even when the click has to land on the
        label, which is the usual arrangement for a styled checkbox.
        """
        if form.checked(self._FOLLOW_INPUT) is False:
            return
        for selector in self._FOLLOW:
            if form.visible(selector):
                form.click([selector])
                return

    def _submit(self, form: Form, page, ctx: ApplyContext, outcomes) -> AdapterResult:
        if not ctx.submit:
            ctx.record(page, "ready to submit")
            return fold_result(
                "filled",
                outcomes,
                note="Filled and ready — submit was not requested",
            )
        if form.click(list(self._SUBMIT)) is None:
            return fold_result("filled", outcomes, note="No submit button on the modal")
        form.settle(2000)
        ctx.record(page, "submitted")
        return fold_result("submitted", outcomes, note="Easy Apply submitted")


__all__ = [
    "BudgetDecision",
    "EasyApplyAdapter",
    "LinkedInChallenge",
    "LinkedInCredentialsRejected",
    "apply_budget",
    "canonical_job_url",
    "clear_credentials",
    "ensure_login",
    "get_account",
    "job_id_from_url",
    "parse_guest_jobs",
    "password_for",
    "record_apply",
    "save_session",
    "search_params",
    "session_state",
    "set_status",
    "status_summary",
    "store_credentials",
]
