"""Autopilot preferences — the one thing a job seeker configures, set once.

The whole product promise is "connect Gmail, upload a resume, set preferences,
walk away". This row *is* those preferences: what roles/industries/locations to
chase, what salary floor matters, how selective to be (``min_fit_score``), and
how many applications a day the user is comfortable sending from their personal
mailbox.

Exactly one row per user. When ``is_active`` is on, the auto-apply beat task
(:mod:`app.tasks.auto_apply_tasks`) runs the full pipeline for this user on a
schedule: discover jobs → score fit → tailor → find a recruiter → send → follow
up. Everything downstream reads its knobs off this row.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.campaign import Campaign
    from app.models.resume import Resume
    from app.models.user import User


class AutopilotPreference(Base, TimestampMixin):
    __tablename__ = "autopilot_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    # `unique=True` with no `index=True`. Adding `index=True` would not have
    # added a second index here — SQLAlchemy renders the pair as one *unique
    # index* — but that is a different object from the named unique *constraint*
    # the migration built, so the model and production described this column
    # differently for as long as both existed. The migration then created
    # `ix_autopilot_preferences_user_id` alongside its constraint, and *that* is
    # what put two btrees on one column. `c5a9e63b17d4` drops the extra one; the
    # two descriptions are now checked against each other in
    # `tests/test_schema_drift.py`.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # The resume the pipeline scores and tailors against. Falls back to the
    # user's default resume when unset.
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), index=True
    )
    # The campaign every auto-applied outreach is filed under — created lazily on
    # the first run so the tracker/dashboard group autopilot work together.
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )

    # Master switch. Off by default: nothing sends until the user opts in.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ---- Targeting (all derived-from-resume when left empty) ----
    target_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    target_industries: Mapped[list[str]] = mapped_column(JSON, default=list)
    locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    salary_min: Mapped[int | None] = mapped_column(Integer)

    # ---- Selectivity ----
    # Only jobs scoring at or above this are applied to. The quality-over-volume
    # lever: research §1.2 shows tailored beats spray-and-pray 10x.
    min_fit_score: Mapped[int] = mapped_column(Integer, default=70, nullable=False)
    # User-facing ceiling on new applications per day. The warm-up ramp
    # (reputation_service) can hold the effective number lower early on.
    daily_application_limit: Mapped[int] = mapped_column(
        Integer, default=10, nullable=False
    )

    # ---- Behaviour ----
    # Send outreach without a per-email review step. This governs the initial
    # outreach only; replies to inbound recruiters have their own switch and cap
    # (``RecruiterReplyPreference.auto_reply_enabled``) and ignore this column.
    #
    # This is the user's *intent*; it is not on its own the answer to "may this
    # email skip review?". Three more columns below can hold auto-send back with
    # the intent still on, and they are only composed correctly in one place:
    # :mod:`app.services.send_policy`. Read the policy, not this column.
    auto_send: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # How many outreach emails the user wants to approve by hand before auto-send
    # takes over — the "watch it work, then let it run" ramp. 0 means auto-send
    # engages on the very first email. Only consulted while ``auto_send`` is on.
    auto_send_trial_approvals: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    # Approvals booked against that trial. Monotonic: it counts how much the user
    # has seen, so lowering the trial length can graduate someone immediately but
    # raising it never un-graduates them retroactively into a surprise.
    auto_send_approved_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    # Auto-send off until this moment, with the rest of autopilot still running.
    # The lever for "stop emailing strangers for a bit" that does not require
    # tearing down the whole pipeline (and losing the discovery work) to pull.
    # Emails found during a pause are parked as DRAFT, not dropped.
    auto_send_paused_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # User-facing ceiling on *unreviewed* sends per rolling 24h. Null defers
    # entirely to the reputation ramp. This is deliberately a second, lower
    # ceiling rather than a replacement: the ramp protects the mailbox, this
    # protects the user from an agent having a bad day at scale.
    auto_send_daily_limit: Mapped[int | None] = mapped_column(Integer)
    # ---- Inbox replies (see services/thread_reply_policy) --------------------
    # Whether a reply the agent drafts *on a thread we started* may go back out
    # without the user reading it. Separate from ``auto_send`` above, which
    # governs first contact with a stranger, and from
    # ``RecruiterReplyPreference.auto_reply_enabled``, which governs mail that
    # arrived cold. This one is about a conversation the user is already in.
    #
    # Defaults on, to match ``auto_send``: the promise is "walk away", and a
    # recruiter who asked a question on Friday should not wait until Monday for
    # an answer the agent already wrote. Everything risky is held regardless of
    # this switch — see the intent list below.
    inbox_auto_reply: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # The classifier confidence a reply needs before it may send unread, as a
    # percentage. Some intents demand more than this (never less) — the floors
    # are in ``thread_reply_policy``, because a bar the user can drag below what
    # is safe is not a safety property.
    inbox_auto_reply_min_confidence: Mapped[int] = mapped_column(
        Integer, default=85, nullable=False
    )
    # Which classified intents are eligible at all, by name. A list rather than a
    # column per intent so adding an intent is not a migration, and empty means
    # "nothing auto-sends" — which is the honest reading of an empty allowlist
    # and gives the user a second way to turn the feature off.
    #
    # The default deliberately omits OFFER and NOT_INTERESTED. Both are moments
    # where the words are the candidate's to choose: an offer is the one email
    # in the process worth reading twice, and a rejection answered by a machine
    # is how a bridge gets burned politely.
    inbox_auto_reply_intents: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["INTERESTED", "QUESTION", "SCHEDULING"]
    )

    # Attempt to auto-fill the job's own application form (Playwright) in addition
    # to emailing a recruiter. Off by default — it is best-effort.
    form_autofill_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Write a job-specific cover letter for every auto-applied posting. On by
    # default: a letter grounded in the resume is the cheapest lift in response
    # rate the pipeline has, and it costs the user nothing to review.
    cover_letter_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # inline | attachment — whether the letter is folded into the email body or
    # sent as a separate file. Inline by default: a cold email with an attachment
    # from an unknown sender is materially likelier to be filtered.
    cover_letter_delivery: Mapped[str] = mapped_column(
        String(16), default="inline", nullable=False
    )

    # ---- Outreach personalization -------------------------------------------
    # How the email should read. These reach both the model prompt *and* the
    # deterministic template fallback, so a user who sets a tone still gets it
    # when no LLM is reachable — a preference that silently does nothing on the
    # fallback path is worse than not offering it.
    #
    # peer | warm | direct | formal — the register, not the content.
    outreach_tone: Mapped[str] = mapped_column(
        String(16), default="peer", nullable=False
    )
    # brief | standard — maps to a word ceiling in the composer.
    outreach_length: Mapped[str] = mapped_column(
        String(16), default="standard", nullable=False
    )
    # call | reply | referral — what the single closing ask actually asks for.
    outreach_cta: Mapped[str] = mapped_column(
        String(16), default="call", nullable=False
    )
    # Replaces "Best," when set. Null keeps the composer's default.
    outreach_sign_off: Mapped[str | None] = mapped_column(String(60))
    # Things the user wants mentioned when they are true of the candidate —
    # "open to relocation", "shipped the billing rewrite". Offered to the model
    # as optional material, never as facts to assert: see the composer.
    outreach_highlights: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Free-text steering appended to the prompt. Bounded in the schema.
    outreach_custom_instructions: Mapped[str | None] = mapped_column(Text)

    # ---- Follow-up sequence (mirrors Campaign's, applied to every auto-apply) ----
    follow_up_count: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # Days from the initial send to the *first* nudge — the preferences form's
    # "First one after". 3, not 4, so that a user who never opens the control
    # gets the day-3/day-7 cadence the product documents: this default and
    # ``settings.follow_up_step_days`` describe the same sequence and used to
    # disagree, which was invisible only for as long as nothing read this column.
    follow_up_interval_days: Mapped[int] = mapped_column(
        Integer, default=3, nullable=False
    )
    follow_up_stop_on_reply: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # Explicit day offsets from the initial send, e.g. [3, 7, 14]. Null falls
    # back to the sequence `default_offsets` generates from the two knobs above.
    #
    # The mirror of `Campaign.follow_up_step_days`, and the reason that column
    # exists: the interval knob cannot express "day 3 then day 7" — no integer
    # interval yields that pair under the shape the generator extends. Held here
    # as well as on the campaign because `auto_apply_service` copies this row's
    # cadence onto the autopilot campaign every cycle, so a value that lived only
    # on the campaign would be overwritten by the row that did not have one.
    follow_up_step_days: Mapped[list[int] | None] = mapped_column(JSON)

    # ---- Live state ----
    # Set the first time the user saves preferences. The row is created lazily
    # on any read of /autopilot, so existence alone can't tell the onboarding
    # wizard whether the preferences step is done — this timestamp can.
    configured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When a cycle took the lease, or NULL when none is running. A cycle is
    # minutes of crawling and model calls, so it cannot hold a row lock for its
    # duration; this timestamp is the lease it holds instead. See
    # ``auto_apply_service.claim_cycle`` for who can run at once and why it
    # matters that only one can.
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    applications_created: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="autopilot")
    resume: Mapped[Resume | None] = relationship()
    campaign: Mapped[Campaign | None] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<AutopilotPreference user={self.user_id} active={self.is_active}>"
