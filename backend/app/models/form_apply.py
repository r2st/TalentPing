"""Form-based applications — the browser agent's side of the pipeline.

Email outreach produces an :class:`~app.models.application.Application`; a form
application produces one of these. They are deliberately separate tables: an
outreach is a conversation with a person and lives or dies on replies, whereas a
form submission is a *run* — it has attempts, steps, screenshots and an exit
status, and the interesting question afterwards is "what did the browser
actually do", not "did they write back".

Three things live here:

* :class:`FormApplication` — one row per attempt at one posting's form. Every
  step is screenshotted and every answered question recorded, because when an
  automated submission goes wrong the only useful evidence is what the page
  looked like at the time.
* :class:`FormApplyProfile` — the answers a resume cannot supply. Work
  authorization, sponsorship, notice period and salary expectations are
  *legally significant* and the candidate's to state, so they are stored once by
  the user and replayed verbatim. Scout answers the rest from the resume, and
  never invents an answer to these.
* :class:`ATSPlatform` / :class:`FormApplyStatus` — the vocabulary both the
  service layer and the UI speak.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.core.enums import FilterEnum
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.job import JobPosting
    from app.models.user import User


class ATSPlatform(FilterEnum):
    """Which applicant tracking system is behind an application URL.

    ``GENERIC`` is a real form we will try the general-purpose filler on;
    ``UNKNOWN`` means there is nothing usable to open at all.
    """

    LINKEDIN = "linkedin"
    WORKDAY = "workday"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    ICIMS = "icims"
    GENERIC = "generic"
    UNKNOWN = "unknown"


class FormApplyStatus(FilterEnum):
    """Where one form-apply run got to.

    The distinction that matters operationally is ``NEEDS_INPUT`` versus
    ``FAILED``: the first means the page asked something only the candidate can
    answer (a sign-in wall, a 2FA challenge, a question with no grounded
    answer) and retrying changes nothing; the second is a transient browser or
    network problem and *is* worth another attempt.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    FILLED = "FILLED"            # filled but deliberately not submitted
    SUBMITTED = "SUBMITTED"
    NEEDS_INPUT = "NEEDS_INPUT"  # a human has to finish it
    NO_FORM = "NO_FORM"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"  # no browser installed on this server
    RATE_LIMITED = "RATE_LIMITED"
    THROTTLED = "THROTTLED"      # the *employer's* site told us to slow down

    @property
    def is_terminal(self) -> bool:
        return self not in (FormApplyStatus.QUEUED, FormApplyStatus.RUNNING)

    @property
    def is_retryable(self) -> bool:
        """Statuses a second run could plausibly get further with.

        What the *user* may retry by hand. Three different reasons:
        ``FAILED`` and ``THROTTLED`` are the site's problem and a later run may
        well succeed, and ``RATE_LIMITED`` is ours — a budget that will roll
        over, a flag an operator can turn on — so the button has to be there
        even though nothing automatic should press it.

        :func:`app.tasks.form_apply_tasks._should_retry` is deliberately
        narrower than this; see its docstring for why RATE_LIMITED is not on
        the automatic list.
        """
        return self in (
            FormApplyStatus.FAILED,
            FormApplyStatus.RATE_LIMITED,
            FormApplyStatus.THROTTLED,
        )

    @property
    def retries_automatically(self) -> bool:
        """Statuses the Celery task re-queues on its own.

        Split out from :attr:`is_retryable` because the two questions have
        genuinely different answers for ``RATE_LIMITED``. That status covers a
        *local* refusal — the daily submission budget, a disabled feature
        flag, a missing LinkedIn connection, a platform with no adapter — and
        none of those clear in the one-to-fifteen minutes the task backs off
        for. Spending the row's remaining attempts on them turns a recoverable
        refusal into a hard failure, which is why it is not here.

        ``THROTTLED`` exists precisely so the *remote* case does not have to
        share that fate: an ATS answering 429 is asking us to come back later,
        and coming back later is a thing this can do.
        """
        return self in (FormApplyStatus.FAILED, FormApplyStatus.THROTTLED)


# Statuses that count against the daily submission budget. A run that never
# reached the submit button didn't cost the user anything with the ATS.
BUDGETED_STATUSES = (FormApplyStatus.SUBMITTED,)


class FormApplication(Base, TimestampMixin):
    """One run of the browser agent against one application form."""

    __tablename__ = "form_applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # SET NULL rather than CASCADE: pruning the job feed must never erase the
    # record that we submitted something on the candidate's behalf.
    job_posting_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), index=True
    )
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), index=True
    )

    platform: Mapped[ATSPlatform] = mapped_column(
        SAEnum(ATSPlatform, native_enum=False, length=16),
        default=ATSPlatform.UNKNOWN,
        nullable=False,
        index=True,
    )
    url: Mapped[str | None] = mapped_column(Text)
    # Company/title copied off the posting so the audit row still reads sensibly
    # after the posting itself is pruned.
    job_title: Mapped[str | None] = mapped_column(String(500))
    company: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[FormApplyStatus] = mapped_column(
        SAEnum(FormApplyStatus, native_enum=False, length=16),
        default=FormApplyStatus.QUEUED,
        nullable=False,
        index=True,
    )
    # False means "fill only" — the candidate reviews and presses submit
    # themselves. The irreversible click is opt-in, per run.
    submit_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    # ---- What the run did (the audit trail) ----
    # Profile attributes that were typed in: ["first_name", "email", ...].
    filled_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    # [{"question": …, "answer": …, "source": "bank|resume|llm|declined"}]
    answers: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # [{"step": 1, "label": "contact info", "screenshot": "…png", "at": iso}]
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    resume_uploaded: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Questions the run could not answer from the resume or the answer bank.
    # These are what a NEEDS_INPUT run is waiting on.
    unanswered: Mapped[list[str]] = mapped_column(JSON, default=list)

    note: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # The employer's own acknowledgement, verbatim, as it appeared on the page
    # after the submit click. Null on a run that never submitted — and also on
    # one that submitted into a page that said nothing back, which is why the
    # receipt reports it as unconfirmed rather than as failure.
    confirmation: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    user: Mapped[User] = relationship()
    job_posting: Mapped[JobPosting | None] = relationship()

    @property
    def screenshot_count(self) -> int:
        return sum(1 for step in (self.steps or []) if step.get("screenshot"))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<FormApplication id={self.id} {self.platform.value} "
            f"status={self.status.value}>"
        )


class FormApplyProfile(Base, TimestampMixin):
    """The answers a resume can't supply, stated once by the candidate.

    Every field here is something an ATS asks and a language model must never
    guess: whether you can work in a country, whether you need sponsorship, what
    you expect to be paid. Scout answers screening questions from the resume,
    and consults this row first — anything it can't ground in one of the two is
    left for the candidate rather than invented.

    EEO/demographic questions are never answered from here. The adapters select
    "decline to self-identify" where the form offers it and skip it otherwise.
    """

    __tablename__ = "form_apply_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    # ---- Legally significant, always the user's own words ----
    work_authorized: Mapped[bool | None] = mapped_column(Boolean)
    requires_sponsorship: Mapped[bool | None] = mapped_column(Boolean)
    willing_to_relocate: Mapped[bool | None] = mapped_column(Boolean)
    # Free text so "immediately", "2 weeks" and "1 March" all work.
    earliest_start: Mapped[str | None] = mapped_column(String(120))
    notice_period_days: Mapped[int | None] = mapped_column(Integer)
    desired_salary: Mapped[str | None] = mapped_column(String(120))

    # ---- Contact details the resume may not carry ----
    phone: Mapped[str | None] = mapped_column(String(64))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    website_url: Mapped[str | None] = mapped_column(String(500))
    github_url: Mapped[str | None] = mapped_column(String(500))
    address_city: Mapped[str | None] = mapped_column(String(120))
    address_country: Mapped[str | None] = mapped_column(String(120))

    # Free-form overrides: {"question substring": "answer"}. Checked before the
    # LLM, so a candidate who keeps meeting the same odd question answers it once.
    custom_answers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    # Let Scout answer questions the bank doesn't cover, grounded in the resume.
    # Off means unmatched questions leave the run in NEEDS_INPUT instead.
    llm_answers_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    user: Mapped[User] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<FormApplyProfile user={self.user_id}>"
