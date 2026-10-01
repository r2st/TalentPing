"""Career-page form auto-apply — the browser agent (roadmap improvement 7).

For jobs that have a real "Apply" form (not just a recruiter to email), this
fills the common fields from the candidate's resume and, optionally, submits.
Roadmap §2.5: start simple — name, email, phone, LinkedIn, resume upload — and
degrade gracefully on everything else. The market's biggest gap is reliable ATS
form-filling; this is the honest first step, not a claim to have solved Workday.

Design for testability and safety:

* The **matching logic is pure** — :func:`plan_fills` takes a list of
  :class:`FormField` descriptors and the candidate profile and returns what to
  type where. No browser is involved, so it is unit-tested directly.
* The **browser driving** (:func:`autofill_application`) is a thin Playwright
  wrapper around that plan. Playwright is an *optional* dependency: if it (or its
  browser binary) isn't installed, the call returns ``unsupported`` rather than
  raising, exactly like :mod:`app.services.gmail_service` does for Google.
* **Submit is opt-in.** By default the form is only *filled* — the candidate (or
  an explicit ``submit=True``) makes the irreversible click.

Network- and browser-bound: only ever call it from a Celery worker.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.models.resume import Resume

logger = logging.getLogger(__name__)

try:  # Optional — installed via the `browser` extra. Absent in the default deploy.
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where playwright is absent
    _PLAYWRIGHT_AVAILABLE = False


@dataclass
class ApplicantProfile:
    """The candidate facts a job form might ask for, from their resume."""

    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    website: str | None = None
    resume_path: str | None = None

    @classmethod
    def from_resume(cls, resume: Resume, *, resume_path: str | None = None) -> ApplicantProfile:
        name = (resume.full_name or "").strip()
        first, _, last = name.partition(" ")
        links = resume.links or []
        linkedin = next((u for u in links if "linkedin.com" in u.lower()), None)
        website = next((u for u in links if "linkedin.com" not in u.lower()), None)
        return cls(
            full_name=name or None,
            first_name=first or None,
            last_name=last or None,
            email=resume.email,
            phone=resume.phone,
            location=resume.location,
            linkedin_url=linkedin,
            website=website,
            resume_path=resume_path,
        )


# Anything that is not a letter or a digit separates two words. Identifiers
# spell that gap as ``_``, ``-`` or ``.``; prose spells it as a space; both mean
# the same thing to a rule written as "first name".
_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")


def _normalize_hint(text: str) -> str:
    """A hint in the same shape as :attr:`FormField.haystack`."""
    return _SEPARATOR_RE.sub(" ", text.lower()).strip()


@dataclass
class FormField:
    """A single form control, as read off the page (or built in a test)."""

    name: str = ""
    field_id: str = ""
    field_type: str = "text"  # text|email|tel|url|file|textarea|...
    label: str = ""
    placeholder: str = ""
    tag: str = "input"  # input|textarea|select
    # ``<select>`` / radio-group choices, so a question with a fixed answer set
    # can be answered with one of *its* words rather than free text.
    options: list[str] = field(default_factory=list)
    required: bool = False
    # Workday hangs everything off data-automation-id rather than name/id, and
    # it is by far the most stable handle on one of their pages.
    automation_id: str = ""
    # Position among the controls that were read, so a nameless control still
    # has something to address it by.
    index: int = 0

    @property
    def haystack(self) -> str:
        """All the human-readable hints for this field, lowercased and unpunctuated.

        Separators are folded to spaces before anything is matched against this,
        because two of the five sources are *identifiers* rather than prose, and
        an identifier does not spell a word gap with a space.

        The hints below are phrases — "first name", "date of birth", "social
        security" — and a control named ``first_name``, ``first-name`` or
        ``firstName`` contained none of them. Only ``firstName`` survived, by
        accident: lowercasing closes the gap for camelCase and for nothing else.
        So on a form whose controls carry ids but no readable label — the case
        the label hunt in :data:`FIELD_JS` already admits it cannot always win —
        ``first_name`` fell past every specific rule to the bare "name" rule at
        the bottom, and was filled with the candidate's **full** name. Worse,
        :func:`plan_fills` spends each attribute once, so ``last_name`` then
        matched the same exhausted rule and was left empty.

        That is a real application, submitted to a real employer, reading
        "Jane Doe" in First Name and nothing in Last Name — and the run reports
        two fields filled, which is what it looks like when it works.

        Folding is applied to the hint tables too, at import — see
        :func:`_normalize_hint`. Both sides normalise or "e-mail" would stop
        matching ``e-mail``.
        """
        joined = " ".join(
            [self.name, self.field_id, self.label, self.placeholder, self.automation_id]
        )
        return _SEPARATOR_RE.sub(" ", joined.lower()).strip()

    @property
    def selector(self) -> str | None:
        """A CSS selector addressing this control, or None if it has no handle.

        Attribute form rather than ``#id``: ATS ids routinely contain colons and
        dashes that make a bare ``#id`` selector invalid CSS.
        """
        if self.automation_id:
            return f'[data-automation-id="{self.automation_id}"]'
        if self.field_id:
            return f'[id="{self.field_id}"]'
        if self.name:
            return f'[name="{self.name}"]'
        return None


# Ordered most-specific first: "first name" must win before the bare "name" rule.
# Each entry maps a set of hint substrings to the profile attribute to type.
_FIELD_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("first name", "firstname", "given name", "fname"), "first_name"),
    (("last name", "lastname", "surname", "family name", "lname"), "last_name"),
    (("full name", "your name", "candidate name"), "full_name"),
    (("email", "e-mail"), "email"),
    (("phone", "mobile", "telephone", "contact number"), "phone"),
    (("linkedin",), "linkedin_url"),
    (("portfolio", "website", "personal site", "github"), "website"),
    (("location", "city", "where are you", "current location"), "location"),
    # Bare "name" is last so it only catches a lone name field.
    (("name",), "full_name"),
)

# The same rules in the shape :attr:`FormField.haystack` is in. Derived rather
# than hand-normalised so the table above stays readable and cannot drift.
_FIELD_RULES = tuple(
    (tuple(_normalize_hint(hint) for hint in hints), attr)
    for hints, attr in _FIELD_RULES
)

# Fields we never auto-fill from a resume — they need a human's judgement or are
# sensitive. Matching any of these hints skips the field entirely.
#
# Stems rather than whole words for the one spelt two ways. This list carried
# "authorization" alone, so it guarded the US spelling of a question and no
# other: "Work authorisation status" and "Are you authorised to work in the
# UK?" are what every UK, Irish and Australian ATS prints, and neither matched.
# "Where are you authorised to work?" is the one that bites, because it does
# not merely fall through — it contains "where are you", which is the
# `location` rule, so the candidate's home city was typed into a
# work-authorization field on a real employer's form as though it answered it.
_SKIP_HINTS = (
    "password", "salary", "compensation", "ssn", "social security", "sponsor",
    "visa", "authoriz", "authoris", "right to work", "eligible to work",
    "work permit", "cover letter", "why do you", "gender", "race",
    "ethnicity", "disability", "veteran", "date of birth",
)
_SKIP_HINTS = tuple(_normalize_hint(hint) for hint in _SKIP_HINTS)

# Whose name a name field is asking for.
#
# The rules above match on substrings and the last of them is a bare "name", so
# "Current employer name", "School name" and "Emergency contact name" were all
# filled with the candidate's own name. :func:`plan_fills` doubles the damage
# by spending each profile attribute once: whichever of these appeared first on
# the page consumed ``full_name``, so the candidate's *real* name field further
# down was left blank. One field wrong, one field empty, and nothing in the run
# says either happened.
#
# Applied to every name rule rather than only the bare one — "Reference first
# name" and "Emergency contact last name" are the same question about the same
# other person.
_OTHER_PARTY = (
    "company", "employer", "organisation", "organization", "business",
    "school", "university", "college", "institution",
    "reference", "referral", "referrer", "referred by", "recruiter",
    "emergency", "next of kin", "spouse", "supervisor", "manager",
    # Not a person at all — a login, or the file just uploaded.
    "username", "user name", "filename", "file name",
)
_OTHER_PARTY = tuple(_normalize_hint(hint) for hint in _OTHER_PARTY)
_NAME_ATTRS = frozenset({"first_name", "last_name", "full_name"})

#: Controls that are page chrome rather than questions — never filled, never
#: asked about. Lives here rather than in :mod:`app.services.ats_adapters`
#: because both callers of :func:`plan_fills` need it and only one of them had
#: it; see :func:`plan_fills`.
NON_INPUT_TYPES = frozenset(
    {"hidden", "submit", "button", "image", "reset", "search"}
)

#: Controls :func:`plan_fills` must not type into.
#:
#: A superset of :data:`NON_INPUT_TYPES`, and deliberately a *different* set: a
#: checkbox or a radio is a real question — :mod:`app.services.form_answers`
#: answers those from the bank — it is simply not one you type a name into.
#: Keeping them out of ``NON_INPUT_TYPES`` is what lets ``questions_from`` still
#: see them.
_UNTYPEABLE_TYPES = NON_INPUT_TYPES | {"checkbox", "radio", "file"}

@dataclass
class Fill:
    field: FormField
    value: str
    attr: str  # which profile attribute it came from


@dataclass
class FormApplyResult:
    # unsupported | no_form | filled | submitted | failed
    status: str
    filled_fields: list[str] = field(default_factory=list)
    resume_uploaded: bool = False
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "filled_fields": self.filled_fields,
            "resume_uploaded": self.resume_uploaded,
            "note": self.note,
        }


def _value_for(field_: FormField, profile: ApplicantProfile) -> tuple[str, str] | None:
    """The (value, source-attr) to type into *field_*, or None to leave it alone."""
    hay = field_.haystack
    if any(skip in hay for skip in _SKIP_HINTS):
        return None
    for hints, attr in _FIELD_RULES:
        if not any(hint in hay for hint in hints):
            continue
        # A name field belonging to somebody else is not one we have an answer
        # for, and claiming it costs the candidate their own. See
        # :data:`_OTHER_PARTY`.
        if attr in _NAME_ATTRS and any(other in hay for other in _OTHER_PARTY):
            return None
        value = getattr(profile, attr, None)
        if value:
            return str(value), attr
        return None
    return None


def plan_fills(fields: list[FormField], profile: ApplicantProfile) -> list[Fill]:
    """Decide what to type where — the pure core of the form filler.

    Skips controls that are not typed into at all, sensitive fields, and
    anything with no matching profile value. Each profile attribute is used at
    most once, so a page with both "name" and "full name" doesn't get the name
    typed twice.

    That last rule is why :data:`_UNTYPEABLE_TYPES` has to be checked *here*
    rather than by each caller. The skip is not merely "don't bother" — a
    control that matches a rule **spends** the profile attribute, and the real
    field further down the page is then never planned at all.

    :func:`app.services.ats_adapters.fillable` filtered these out before
    calling, so the ATS wizard was safe; :func:`autofill_application`, which is
    the path a plain career page takes, passed the page's controls in raw. On
    such a page:

    * ``<input type="search" placeholder="Search jobs by name">`` matched the
      bare "name" rule, and a search box is visible and editable, so Playwright
      typed the candidate's name into the site search — successfully. The run
      reported ``filled_fields: ["full_name"]`` while the form's own Full Name
      box stayed empty.
    * ``<input type="checkbox" name="email_job_alerts">`` matched the "email"
      rule and spent ``email``. Playwright refuses to fill a checkbox, so that
      one failed quietly and the real address field was never even attempted.
    * A hidden tracking input carrying ``first_name`` in its id did the same to
      the name.

    An application submitted with no name and no address in it, from a run that
    says it filled the form. Both the honeypot pattern (a hidden or
    off-screen field named for a real one, there to catch exactly this) and an
    ordinary "email me similar jobs" tickbox produce it.
    """
    used: set[str] = set()
    plan: list[Fill] = []
    for field_ in fields:
        if field_.field_type in _UNTYPEABLE_TYPES or field_.tag == "select":
            continue
        match = _value_for(field_, profile)
        if match is None:
            continue
        value, attr = match
        if attr in used:
            continue
        used.add(attr)
        plan.append(Fill(field=field_, value=value, attr=attr))
    return plan


def is_available() -> bool:
    """True when Playwright (and thus live form-filling) is installed."""
    return _PLAYWRIGHT_AVAILABLE


# --------------------------------------------------------------------------- #
# Browser driving (thin wrapper around the pure plan above)                    #
# --------------------------------------------------------------------------- #

# Read every control on a page into the shape of :class:`FormField`. Shared with
# :mod:`app.services.browser_runner`, which drives the ATS adapters — one
# extraction, so the general filler and the platform adapters always see the
# same page.
#
# The label hunt is three-deep on purpose. A `for=`-linked label is the clean
# case; ATS builders instead wrap the control in a label, or (Workday, Lever)
# label it via aria-label / aria-labelledby with no <label> element at all, and a
# field with no label is a field we cannot match on.
FIELD_JS = r"""els => els.map((el, index) => {
    // One reader for every branch below, because they were four spellings of
    // the same line and two of them were wrong in the same way.
    //
    // `innerText` is the right first choice: it is the text as rendered, with
    // the indentation a legend is typeset with already collapsed. It is also
    // empty for anything the page has `display: none`'d — and a label a form
    // hides is still the question the control is asking. Every ATS on the
    // market ships at least one: a `<legend>` kept for screen readers, a
    // `.label` shown only on the review step. `textContent` is what that
    // element still says, and it is the accessible name either way.
    //
    // Whitespace is collapsed here rather than left to the caller. `innerText`
    // does it and `textContent` does not, so without this the same question
    // arrives as "Work Authorization" from one branch and as
    // "\n        Work Authorization\n      " from the next, and every hint
    // table that reads a label is written for the first spelling.
    const textOf = node => (
        ((node && (node.innerText || node.textContent)) || '')
            .replace(/\s+/g, ' ')
            .trim()
    );
    const id = el.id || '';
    let label = '';
    if (id) {
        const l = document.querySelector(`label[for="${CSS.escape(id)}"]`);
        if (l) label = textOf(l);
    }
    if (!label && el.closest('label')) label = textOf(el.closest('label'));
    if (!label) label = el.getAttribute('aria-label') || '';
    if (!label) {
        // `aria-labelledby` is a space-separated *list* of ids — that is the
        // whole attribute, not an edge case of it — and the list was handed to
        // `getElementById` whole. "q42-legend q42-hint" is not an element id,
        // so the lookup returned null and the control came back with no label
        // at all.
        //
        // It is the spelling Workday and iCIMS use, and they use it for the
        // questions that matter: a legend carrying the question and a second
        // node carrying the qualifier, which is exactly how "Are you legally
        // authorized to work in the US" and "without sponsorship" arrive as
        // two ids. Losing the label loses the question — `questions_from`
        // drops a control whose text is under three characters, so a required
        // screening question became a control nothing ever filled, and the run
        // submitted without it or stopped without saying which question it was
        // stopping on.
        //
        // Joined in the order the attribute lists them, which is the order the
        // accessible name is built in and the order the sentence reads in.
        const by = (el.getAttribute('aria-labelledby') || '').trim();
        if (by) {
            label = by.split(/\s+/)
                .map(ref => document.getElementById(ref))
                .map(textOf)
                .filter(Boolean)
                .join(' ');
        }
    }
    if (!label) {
        const group = el.closest('[role="group"], fieldset, .field, [data-automation-id]');
        if (group) {
            const legend = group.querySelector('legend, label, .label');
            if (legend) label = textOf(legend);
        }
    }
    const options = el.tagName.toLowerCase() === 'select'
        ? Array.from(el.options).map(o => (o.label || o.text || o.value || '').trim())
        : [];
    return {
        name: el.name || '',
        field_id: id,
        field_type: (el.type || '').toLowerCase(),
        label: (label || '').trim().slice(0, 300),
        placeholder: el.placeholder || '',
        tag: el.tagName.toLowerCase(),
        options: options.filter(Boolean),
        required: el.required || el.getAttribute('aria-required') === 'true',
        automation_id: el.getAttribute('data-automation-id') || '',
        index: index,
    };
})"""


def _read_fields(page) -> list[FormField]:  # pragma: no cover - needs a real browser
    """Extract form controls from a live Playwright page."""
    raw = page.eval_on_selector_all("input, textarea, select", FIELD_JS)
    return [FormField(**item) for item in raw]


def autofill_application(
    url: str,
    profile: ApplicantProfile,
    *,
    submit: bool = False,
    timeout_ms: int = 20000,
) -> FormApplyResult:
    """Open *url*, fill the common application fields, optionally submit.

    Returns ``unsupported`` (never raises) when Playwright isn't installed, so
    the pipeline can call it unconditionally and simply record the outcome.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return FormApplyResult(
            status="unsupported",
            note="Browser automation is not installed on this server",
        )

    try:  # pragma: no cover - requires a browser; covered by the pure-plan tests
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                fields = _read_fields(page)
                if not fields:
                    return FormApplyResult(status="no_form", note="No form fields found")

                plan = plan_fills(fields, profile)
                filled: list[str] = []
                for fill in plan:
                    selector = _selector_for(fill.field)
                    if selector is None:
                        continue
                    try:
                        page.fill(selector, fill.value, timeout=3000)
                        filled.append(fill.attr)
                    except Exception:  # noqa: BLE001 - one stubborn field must not abort
                        logger.debug("could not fill %s", selector)

                uploaded = _try_upload_resume(page, fields, profile)

                if submit:
                    _try_submit(page)
                    return FormApplyResult(
                        status="submitted", filled_fields=filled, resume_uploaded=uploaded
                    )
                return FormApplyResult(
                    status="filled", filled_fields=filled, resume_uploaded=uploaded
                )
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - never let a page kill the pipeline
        logger.warning(
            "autofill_application(%s) failed: %s", url, exc, exc_info=True
        )
        return FormApplyResult(status="failed", note=str(exc)[:300])


def _selector_for(field_: FormField) -> str | None:  # pragma: no cover - trivial
    return field_.selector


def _try_upload_resume(page, fields, profile) -> bool:  # pragma: no cover - needs browser
    if not profile.resume_path:
        return False
    file_field = next((f for f in fields if f.field_type == "file"), None)
    if file_field is None:
        return False
    selector = _selector_for(file_field)
    if selector is None:
        return False
    try:
        page.set_input_files(selector, profile.resume_path, timeout=5000)
        return True
    except Exception:  # noqa: BLE001
        return False


def _try_submit(page) -> None:  # pragma: no cover - needs a browser
    for selector in (
        'button[type="submit"]',
        'input[type="submit"]',
        "button:has-text('Submit')",
        "button:has-text('Apply')",
    ):
        try:
            page.click(selector, timeout=3000)
            return
        except Exception:  # noqa: BLE001
            continue


__all__ = [
    "NON_INPUT_TYPES",
    "ApplicantProfile",
    "Fill",
    "FormApplyResult",
    "FormField",
    "autofill_application",
    "is_available",
    "plan_fills",
]
