"""Browser plumbing for the form-apply agent: sessions, screenshots, retries.

The adapters in :mod:`app.services.ats_adapters` describe *what* to do on a
Workday or Greenhouse page. This module is everything underneath that: opening a
headless Chromium, reading a page's controls into plain dataclasses, writing
values back, screenshotting every step, and retrying the transient failures that
are simply what driving somebody else's website is like.

Two design choices make the rest of the feature testable:

* **Everything the adapters touch goes through :class:`Form`**, which speaks a
  deliberately small vocabulary — read the fields, fill one, choose an option,
  upload a file, click the first selector that exists. A test supplies a fake
  page implementing that vocabulary and drives a whole Workday flow with no
  browser anywhere near it.
* **Playwright is imported lazily and optionally.** Same contract as
  :mod:`app.services.career_apply_service`: with no browser installed the
  feature reports ``unsupported`` rather than raising, so a deploy without the
  ``browser`` extra keeps working.

Screenshots are the audit trail. When an automated submission goes wrong, the
only evidence worth having is what the page looked like at each step, so every
step is captured by default and the paths are recorded on the run. Each capture
is stamped with the page's URL and the SHA-256 of the bytes written, which is
what lets :mod:`app.services.submission_proof` hand the candidate a receipt
rather than a picture somebody could have swapped.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from app.core.config import settings
from app.services.career_apply_service import FIELD_JS, FormField

logger = logging.getLogger(__name__)

try:  # Optional — installed via the `browser` extra.
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where playwright is absent
    _PLAYWRIGHT_AVAILABLE = False

# A real desktop UA. The bot UA used for careers-page scraping is honest about
# what it is, but ATS vendors serve a degraded no-JS page to anything that looks
# automated, and a degraded page has no form to fill.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_UNSAFE_PATH = re.compile(r"[^a-z0-9._-]+")


class BrowserUnavailable(RuntimeError):
    """Playwright (or its browser binary) is not installed on this server."""


class TransientBrowserError(RuntimeError):
    """A failure worth retrying — a timeout, a navigation error, a flaky click."""


class ApplyBlocked(RuntimeError):
    """The site refused the automated browser, and only a person can get past it.

    A 403, a Cloudflare interstitial, a captcha, an application link that
    redirects in a loop. Distinct from :class:`TransientBrowserError` because
    retrying is not merely useless — it is the thing that got us blocked. The
    run stops as ``needs_input`` and tells the candidate to open the link
    themselves, which is what they would have to do anyway.
    """


class PostingGone(RuntimeError):
    """The posting is not there any more — the site answered 404 or 410.

    Terminal on purpose. A removed job does not come back, and the candidate
    reading "no form controls on the page" (which is what this used to look
    like) has no way to tell that from a page we simply failed to parse.
    """


class RateLimited(RuntimeError):
    """The ATS asked us to slow down — a 429, or its own throttle page.

    *Not* a :class:`TransientBrowserError`, and the difference is the whole
    point. The in-run retry backs off two seconds and then four, which against
    a vendor rate limit is three requests where one was already too many. This
    escalates instead: the run ends ``THROTTLED`` and the task-level retry
    comes back in fifteen minutes.

    ``THROTTLED`` rather than ``RATE_LIMITED`` because the latter already means
    something else here — *our* refusal to run, from the daily budget or a
    disabled flag — which is not a thing waiting fifteen minutes fixes.
    """


# Playwright's own wording when the driver is installed but the browser it
# downloads separately is not. Matched on the message because the launch failure
# is a plain ``playwright.sync_api.Error`` — the same class it raises for a
# perfectly ordinary bad argument, so the type alone cannot tell them apart.
_MISSING_BINARY_MARKERS = (
    "executable doesn't exist",
    "playwright install",
    "please run the following command",
)

INSTALL_HINT = (
    "Browser automation is not installed on this server. Install the `browser` "
    "extra and run `playwright install chromium` as the user the worker runs "
    "as, or point PLAYWRIGHT_BROWSERS_PATH at a directory that user can read."
)


def _looks_like_missing_binary(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _MISSING_BINARY_MARKERS)


@lru_cache(maxsize=1)
def binary_installed() -> bool:
    """True when Chromium is actually on disk *for the current user*.

    ``_PLAYWRIGHT_AVAILABLE`` only says the Python package imports. The browser
    is a separate download that lands under ``$HOME``, so a service running as
    its own user can import Playwright perfectly and still have no browser —
    which is precisely how this deployment was configured, and why every run
    failed at launch with a raw Playwright error instead of an honest
    "not installed".

    The answer is cached: it is read on every Jobs page load, and starting a
    driver process each time to re-answer a question that changes only on deploy
    is not worth it. Call ``binary_installed.cache_clear()`` after installing.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return False
    try:
        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception as exc:  # noqa: BLE001 - an unreadable driver is unavailable
        logger.info("could not locate the Chromium binary: %s", exc)
        return False


def is_available() -> bool:
    """True when live form-filling is possible on this deployment.

    Both halves have to hold: the driver *and* the browser it drives.
    """
    return _PLAYWRIGHT_AVAILABLE and binary_installed()


def artifact_dir() -> Path:
    """Where screenshots are written, created on demand."""
    path = Path(settings.form_apply_artifact_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_key(*parts: object) -> str:
    """A filesystem-safe key for one run's artifacts."""
    joined = "-".join(str(p) for p in parts if p not in (None, ""))
    return _UNSAFE_PATH.sub("-", joined.lower()).strip("-")[:80] or "run"


# --------------------------------------------------------------------------- #
# Deadline                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class Deadline:
    """A wall-clock ceiling on one run, checked between steps.

    Every timeout in this feature bounds one *operation*: a navigation gets
    ``form_apply_timeout_ms``, a control gets :attr:`Form.timeout_ms`. None of
    them bounds the run, and the run is what multiplies — a Workday wizard is
    up to eight steps, each filling a dozen controls at five seconds apiece,
    each asking the answering engine about whatever is left. Nothing in that
    chain is a hang; the sum of it is.

    So the adapters ask this between steps and stop with whatever they have
    rather than starting work they cannot finish. Stopping *between* steps
    rather than mid-step is deliberate: a step is the unit that either got
    filled or did not, and abandoning one halfway would leave a form half
    typed with nothing recorded about it.

    The clock is injectable so the tests are instant and exact. It is
    :func:`time.monotonic` rather than the wall clock because a run must not be
    lengthened or cut short by NTP stepping the machine's clock mid-flight.
    """

    seconds: float
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    @classmethod
    def from_settings(cls, clock=time.monotonic) -> Deadline:
        return cls(seconds=float(settings.form_apply_deadline_seconds), clock=clock)

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started_at

    @property
    def remaining(self) -> float:
        return self.seconds - self.elapsed

    @property
    def expired(self) -> bool:
        """True once the run is out of time.

        A non-positive budget means "no deadline" rather than "already over" —
        an operator switching the ceiling off with 0 must not get a feature
        that refuses every run.
        """
        if self.seconds <= 0:
            return False
        return self.remaining <= 0

    def note(self) -> str:
        return (
            f"Stopped after {int(self.elapsed)}s — this run hit the "
            f"{int(self.seconds)}s ceiling before the form was finished."
        )


# --------------------------------------------------------------------------- #
# Retry                                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class RetryReport:
    """What a retried call cost: how many attempts, and what went wrong."""

    attempts: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def retried(self) -> bool:
        return self.attempts > 1


def with_retries(
    call,
    *,
    attempts: int | None = None,
    backoff_seconds: float = 2.0,
    sleep=time.sleep,
    retry_on: tuple[type[BaseException], ...] = (TransientBrowserError,),
    deadline: Deadline | None = None,
):
    """Run *call* until it succeeds, up to *attempts* times.

    Only ``retry_on`` failures are retried: a page that timed out is worth
    another go, whereas a form asking a question we cannot answer will ask it
    again just as unanswerably. Returns ``(result, report)``; re-raises the last
    error when every attempt failed.

    *deadline*, when given, stops the loop rather than starting an attempt the
    run has no time left for. Without it a retry schedule is a *lower* bound on
    how long a run takes and nothing bounds it above: three attempts against a
    site that times out at 45 seconds is a little over two minutes of
    navigation plus six seconds of backoff, and the attempt that starts one
    second before the ceiling still runs its full timeout past it. The loop
    checks *before* sleeping as well as before calling, so a run does not spend
    its last four seconds waiting to do something it will not be allowed to do.

    Pure enough to test — inject ``sleep``, inject the deadline's clock, and
    pass any callable.
    """
    total = attempts or settings.form_apply_max_attempts
    report = RetryReport()
    last: BaseException | None = None

    for attempt in range(1, max(1, total) + 1):
        report.attempts = attempt
        try:
            return call(attempt), report
        except retry_on as exc:  # noqa: PERF203 - the retry loop is the point
            last = exc
            report.errors.append(str(exc)[:300])
            logger.info("form-apply attempt %s/%s failed: %s", attempt, total, exc)
            if deadline is not None and deadline.expired:
                logger.info(
                    "form-apply gave up after attempt %s: %s", attempt, deadline.note()
                )
                break
            if attempt < total:
                sleep(backoff_seconds * attempt)

    assert last is not None  # noqa: S101 - the loop cannot exit without one
    raise last


# --------------------------------------------------------------------------- #
# Sessions                                                                     #
# --------------------------------------------------------------------------- #


@contextmanager
def browser_page(
    *,
    storage_state: str | dict | None = None,
    headless: bool | None = None,
    user_agent: str | None = None,
    timeout_ms: int | None = None,
):  # pragma: no cover - requires a real browser
    """A headless Chromium page, closed on the way out.

    *storage_state* is Playwright's serialized cookies/localStorage — how a
    LinkedIn session is reused across runs so the password is typed rarely.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        raise BrowserUnavailable(
            "Browser automation is not installed on this server. Install the "
            "`browser` extra and run `playwright install chromium`."
        )

    is_headless = settings.form_apply_headless if headless is None else headless
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                headless=is_headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
        except Exception as exc:  # noqa: BLE001 - re-raised, classified, below
            # A missing browser is a server-capability problem, not a failed
            # application: it must not be retried (the binary will not appear
            # between attempts) and it must not reach the candidate as
            # Playwright's install banner, which is addressed to an operator.
            if _looks_like_missing_binary(exc):
                raise BrowserUnavailable(INSTALL_HINT) from exc
            raise
        context = browser.new_context(
            user_agent=user_agent or DEFAULT_USER_AGENT,
            viewport={"width": 1440, "height": 900},
            storage_state=storage_state,
        )
        context.set_default_timeout(timeout_ms or settings.form_apply_timeout_ms)
        page = context.new_page()
        try:
            yield page
        finally:
            try:
                context.close()
            finally:
                browser.close()


def scope_for(page, url_hint: str):
    """The frame that hosts an embedded board, or the page itself.

    Greenhouse and Lever are usually iframed into the company's own careers
    site; the controls live in the frame, not the top document.
    """
    frames = getattr(page, "frames", None) or []
    for frame in frames:
        try:
            if url_hint in (frame.url or "").lower():
                return frame
        except Exception:  # noqa: BLE001 - a detached frame is not our problem
            continue
    return page


# --------------------------------------------------------------------------- #
# Step log / screenshots                                                       #
# --------------------------------------------------------------------------- #


def digest(path: Path) -> tuple[str, int] | None:
    """``(sha256, size)`` of a file, or None when it can't be read.

    Recorded next to every screenshot so the picture is *evidence* rather than
    an illustration: a receipt that carries the hash of the image can be checked
    against the file on disk later, and a mismatch is visible instead of silent.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest(), len(data)


class StepLog:
    """Records each step of a run, with a screenshot where possible.

    Screenshot failures are never fatal: losing the picture of step 3 must not
    lose the application that step 3 was part of.

    Each entry carries what the picture alone cannot prove: the page it was
    taken of, when, and the hash of the bytes that were written.
    """

    def __init__(
        self,
        run_key: str,
        *,
        enabled: bool | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self.run_key = safe_key(run_key)
        self.enabled = settings.form_apply_screenshots if enabled is None else enabled
        self._base_dir = base_dir
        self.entries: list[dict] = []

    @property
    def base_dir(self) -> Path:
        return self._base_dir if self._base_dir is not None else artifact_dir()

    def step(self, page, label: str, *, note: str | None = None) -> dict:
        """Record one step. Returns the entry that was appended."""
        index = len(self.entries) + 1
        entry: dict = {
            "step": index,
            "label": label,
            "at": datetime.now(UTC).isoformat(),
        }
        if note:
            entry["note"] = note

        # The page's own URL, whether or not the picture works: a step that says
        # which page it happened on is worth something even with no screenshot.
        page_url = getattr(page, "url", None) if page is not None else None
        if isinstance(page_url, str) and page_url:
            entry["url"] = page_url[:2000]

        if self.enabled and page is not None:
            filename = f"{self.run_key}-{index:02d}-{safe_key(label)}.png"
            target = self.base_dir / filename
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(target), full_page=True)
                entry["screenshot"] = filename
            except Exception as exc:  # noqa: BLE001 - evidence is best-effort
                logger.debug("screenshot failed at %s: %s", label, exc)
                entry["screenshot_error"] = str(exc)[:120]
            else:
                stamped = digest(target)
                if stamped is not None:
                    entry["sha256"], entry["bytes"] = stamped

        self.entries.append(entry)
        return entry


# --------------------------------------------------------------------------- #
# The page surface the adapters use                                            #
# --------------------------------------------------------------------------- #


class Form:
    """A form on a page (or in a frame), in the vocabulary adapters need.

    Every method swallows per-control failures and reports them as ``False``.
    One stubborn field on a fifteen-field Workday step must not abandon the
    other fourteen — the run reports what it managed to fill and the candidate
    sees the screenshot.
    """

    def __init__(self, scope, *, timeout_ms: int = 5000) -> None:
        self.scope = scope
        self.timeout_ms = timeout_ms

    # ---- reading ----

    def fields(self, selector: str = "input, textarea, select") -> list[FormField]:
        """Every control on the page, as :class:`FormField` descriptors."""
        try:
            raw = self.scope.eval_on_selector_all(selector, FIELD_JS)
        except Exception as exc:  # noqa: BLE001 - a page we can't read has no form
            logger.debug("could not read fields: %s", exc)
            return []
        out: list[FormField] = []
        for index, item in enumerate(raw or []):
            if not isinstance(item, dict):
                continue
            out.append(
                FormField(
                    name=item.get("name") or "",
                    field_id=item.get("field_id") or "",
                    field_type=(item.get("field_type") or "text"),
                    label=item.get("label") or "",
                    placeholder=item.get("placeholder") or "",
                    tag=item.get("tag") or "input",
                    options=[str(o) for o in (item.get("options") or [])],
                    required=bool(item.get("required")),
                    automation_id=item.get("automation_id") or "",
                    index=index,
                )
            )
        return out

    def text(self) -> str:
        """The page's visible text, lowercased — for wall/error detection."""
        return self.raw_text().lower()

    def raw_text(self) -> str:
        """The page's visible text as the page wrote it, case intact.

        :meth:`text` lowercases so marker matching can be case-insensitive,
        which is right for detection and wrong for quotation: a confirmation
        stored as "thank you for applying to acme." is not the employer's
        sentence, and a receipt that claims to quote one has to actually do it.
        """
        for getter in ("inner_text", "content"):
            method = getattr(self.scope, getter, None)
            if method is None:
                continue
            try:
                value = method("body") if getter == "inner_text" else method()
            except TypeError:
                try:
                    value = method()
                except Exception:  # noqa: BLE001
                    continue
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, str):
                return value
        return ""

    def visible(self, selector: str) -> bool:
        try:
            return bool(self.scope.is_visible(selector))
        except Exception:  # noqa: BLE001 - absent counts as not visible
            return False

    def checked(self, selector: str) -> bool | None:
        """Whether a checkbox is ticked, or ``None`` when it can't be read.

        Three-valued on purpose. A caller that wants to *clear* a box has to
        distinguish "it is already clear" from "I could not tell", because a
        click is a toggle: acting on an unreadable state is how you switch on
        the thing you were trying to switch off.

        Reads the input itself rather than whatever the page makes clickable —
        styled checkboxes hide the real control behind a label, and the hidden
        control is the one holding the state.
        """
        try:
            return bool(self.scope.is_checked(selector))
        except Exception:  # noqa: BLE001 - absent, or not a checkbox
            return None

    # ---- writing ----

    def fill(self, field_: FormField, value: str) -> bool:
        selector = field_.selector
        if not selector or value is None:
            return False
        try:
            self.scope.fill(selector, str(value), timeout=self.timeout_ms)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("fill %s failed: %s", selector, exc)
            return False

    def choose(self, field_: FormField, value: str) -> bool:
        """Select an option — ``<select>``, radio group, or a listbox widget."""
        selector = field_.selector
        if not selector or value is None:
            return False
        if field_.tag == "select":
            try:
                self.scope.select_option(selector, label=str(value), timeout=self.timeout_ms)
                return True
            except Exception:  # noqa: BLE001 - fall through to value-matching
                try:
                    self.scope.select_option(
                        selector, value=str(value), timeout=self.timeout_ms
                    )
                    return True
                except Exception as exc:  # noqa: BLE001
                    logger.debug("select %s failed: %s", selector, exc)
                    return False
        # Radios and checkboxes: click the option whose label matches.
        return self.click([f'{selector}[value="{value}"]', selector]) is not None

    def upload(self, field_: FormField, path: str) -> bool:
        selector = field_.selector
        if not selector or not path:
            return False
        try:
            self.scope.set_input_files(selector, path, timeout=self.timeout_ms)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("upload to %s failed: %s", selector, exc)
            return False

    def click(self, selectors: list[str] | tuple[str, ...]) -> str | None:
        """Click the first of *selectors* that works; return which one."""
        for selector in selectors:
            try:
                self.scope.click(selector, timeout=self.timeout_ms)
                return selector
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
        return None

    def settle(self, ms: int = 1200) -> None:
        """Give a single-page app a moment to render the next step."""
        waiter = getattr(self.scope, "wait_for_timeout", None)
        if waiter is None:
            return
        with contextlib.suppress(Exception):
            waiter(ms)


__all__ = [
    "ApplyBlocked",
    "BrowserUnavailable",
    "DEFAULT_USER_AGENT",
    "Deadline",
    "Form",
    "INSTALL_HINT",
    "PostingGone",
    "RateLimited",
    "RetryReport",
    "StepLog",
    "TransientBrowserError",
    "artifact_dir",
    "binary_installed",
    "browser_page",
    "digest",
    "is_available",
    "safe_key",
    "scope_for",
    "with_retries",
]
