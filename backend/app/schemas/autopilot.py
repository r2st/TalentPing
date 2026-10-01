"""Autopilot schemas — the one preference object the whole agent runs on."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, computed_field, field_validator

from app.core.config import settings
from app.models.email import ReplyIntent
from app.services import follow_up_service, thread_reply_policy


class AutopilotUpdate(BaseModel):
    """Everything the user configures once, then walks away from.

    Every field is optional so the UI can PATCH a single knob; unset fields keep
    their stored value.
    """

    resume_id: int | None = None
    is_active: bool | None = None

    target_roles: list[str] | None = Field(default=None, max_length=10)
    target_industries: list[str] | None = Field(default=None, max_length=10)
    locations: list[str] | None = Field(default=None, max_length=10)
    remote_only: bool | None = None
    salary_min: int | None = Field(default=None, ge=0)

    min_fit_score: int | None = Field(default=None, ge=0, le=100)
    daily_application_limit: int | None = Field(default=None, ge=1, le=50)

    auto_send: bool | None = None
    # Approve this many by hand first, then auto-send takes over. 0 = straight to
    # auto-send. Capped low: a "trial" of 50 is review mode with extra steps.
    auto_send_trial_approvals: int | None = Field(default=None, ge=0, le=25)
    # Ceiling on unreviewed sends per rolling 24h. Null clears it, deferring to
    # the mailbox warm-up ramp alone.
    auto_send_daily_limit: int | None = Field(default=None, ge=1, le=50)
    # ---- Inbox replies ----
    # Whether a reply drafted on a conversation the user is already in may go
    # back out without them reading it, and the bar it has to clear.
    inbox_auto_reply: bool | None = None
    # Floored at 50 rather than 0: a bar the agent clears half the time is not a
    # bar, and the intent floors in ``thread_reply_policy`` can raise this but
    # never lower it. 100 is a legal setting and means "never, in practice".
    inbox_auto_reply_min_confidence: int | None = Field(default=None, ge=50, le=100)
    # Which intents may answer themselves, by name. Validated against the enum at
    # the edge so an unknown intent is a 422 rather than a stored string the
    # policy silently drops. An empty list is legal and means "none".
    inbox_auto_reply_intents: list[str] | None = None

    form_autofill_enabled: bool | None = None
    cover_letter_enabled: bool | None = None
    cover_letter_delivery: str | None = Field(default=None, pattern="^(inline|attachment)$")

    follow_up_count: int | None = Field(default=None, ge=0, le=5)
    follow_up_interval_days: int | None = Field(default=None, ge=1, le=30)
    follow_up_stop_on_reply: bool | None = None
    # Explicit day offsets, e.g. [3, 7, 14]. Null keeps the generated sequence;
    # an empty list is how the UI hands the choice back to the generator, and is
    # normalised to null below rather than stored as "a sequence of no nudges" —
    # ``follow_up_count`` is what turns follow-ups off, and two ways to say it
    # is one too many.
    #
    # Bounded at 5 to match ``follow_up_count``: a list longer than the count is
    # truncated by ``sequence_offsets`` anyway, and a control that silently drops
    # what you typed is worse than one that refuses it. 365 because a nudge more
    # than a year out is not a follow-up.
    follow_up_step_days: list[int] | None = Field(default=None, max_length=5)

    # ---- Outreach personalization ----
    # Patterns rather than enums so an unrecognized value is a 422 at the edge
    # instead of a stored string the composer has to defend against later.
    outreach_tone: str | None = Field(default=None, pattern="^(peer|warm|direct|formal)$")
    outreach_length: str | None = Field(default=None, pattern="^(brief|standard)$")
    outreach_cta: str | None = Field(default=None, pattern="^(call|reply|referral)$")
    outreach_sign_off: str | None = Field(default=None, max_length=60)
    # Short phrases the user wants worked in — capped in both directions so a
    # settings box can't quietly become the whole prompt.
    outreach_highlights: list[str] | None = Field(default=None, max_length=5)
    outreach_custom_instructions: str | None = Field(default=None, max_length=500)


    @field_validator("follow_up_step_days")
    @classmethod
    def _sane_sequence(cls, value: list[int] | None) -> list[int] | None:
        """Sorted, deduplicated, positive — the shape ``sequence_offsets`` reads.

        That function already sorts and dedupes what it is given, so an unsorted
        list would *work* and would then be echoed back to the form in the order
        the user typed it, beside a schedule preview in a different one. Storing
        the normalised form is what keeps the setting and its own preview from
        disagreeing.

        Day 0 and negative days are refused rather than dropped: a follow-up
        before the email it follows is a typo, and silently discarding one entry
        of a sequence leaves the user with a cadence they did not ask for and
        cannot see the difference in.
        """
        if value is None:
            return None
        for day in value:
            if day < 1 or day > 365:
                raise ValueError("follow-up days must be between 1 and 365")
        unique = sorted(set(value))
        # An empty list means "no explicit sequence" — see the field comment.
        return unique or None

    @field_validator("inbox_auto_reply_intents")
    @classmethod
    def _known_and_permitted(cls, value: list[str] | None) -> list[str] | None:
        """Reject intents that don't exist, and ones that may never auto-reply.

        The second half is the point. ``thread_reply_policy`` drops an
        unpermitted intent from its allowlist anyway, so accepting one here would
        store a setting that reads as "on" in the UI and does nothing — the worst
        kind of switch. UNSUBSCRIBE is the case that matters: it must never be
        answered, and a user must not be shown a control implying it could be.
        """
        if value is None:
            return None
        out: list[str] = []
        for raw in value:
            name = str(raw).strip().upper()
            try:
                intent = ReplyIntent(name)
            except ValueError:
                raise ValueError(f"unknown reply intent: {raw!r}") from None
            if intent in thread_reply_policy.NEVER_AUTO:
                raise ValueError(f"{name} replies can never be sent automatically")
            if name not in out:
                out.append(name)
        return out


class AutopilotOut(BaseModel):
    id: int
    user_id: int
    resume_id: int | None = None
    campaign_id: int | None = None
    is_active: bool

    target_roles: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_only: bool
    salary_min: int | None = None

    min_fit_score: int
    daily_application_limit: int
    auto_send: bool
    auto_send_trial_approvals: int
    auto_send_approved_count: int
    auto_send_paused_until: datetime | None = None
    auto_send_daily_limit: int | None = None
    inbox_auto_reply: bool
    inbox_auto_reply_min_confidence: int
    inbox_auto_reply_intents: list[str] = Field(default_factory=list)
    form_autofill_enabled: bool
    # Write a job-specific letter for every auto-applied posting, and whether it
    # travels in the email body or as a file.
    cover_letter_enabled: bool
    cover_letter_delivery: str

    follow_up_count: int
    follow_up_interval_days: int
    follow_up_stop_on_reply: bool
    follow_up_step_days: list[int] | None = None

    outreach_tone: str
    outreach_length: str
    outreach_cta: str
    outreach_sign_off: str | None = None
    outreach_highlights: list[str] = Field(default_factory=list)
    outreach_custom_instructions: str | None = None

    configured_at: datetime | None = None
    last_run_at: datetime | None = None
    # When the cycle currently running took its lease, or null when none is.
    # Raw rather than a computed "is_running" flag: a stale stamp left by a
    # killed worker is still readable here, and the expiry rule that decides
    # what it *means* lives in one place (``auto_apply_service.cycle_is_running``)
    # rather than being re-derived by every reader.
    running_since: datetime | None = None
    last_error: str | None = None
    applications_created: int
    created_at: datetime

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def follow_up_schedule(self) -> list[int]:
        """The days these settings will actually nudge on, counted from the send.

        Derived server-side because the client had been deriving it *wrongly*:
        the preferences form computed the sequence with the widening formula
        that ``follow_up_service`` retired, so it promised "day 4 and day 10"
        beside a scheduler that used day 3 and day 7. A number the user reads
        off a settings panel has to come from the thing that acts on it.
        """
        # The explicit sequence wins, exactly as it does in
        # ``follow_up_service.sequence_offsets`` — this property is the panel's
        # copy of that decision and has to make it the same way. Capped by the
        # count for the same reason the scheduler caps it: the count is what says
        # how many nudges go out, whichever list picks the days.
        if self.follow_up_step_days:
            return sorted(self.follow_up_step_days)[: self.follow_up_count]
        return follow_up_service.default_offsets(
            self.follow_up_interval_days, self.follow_up_count
        )

    @computed_field
    @property
    def follow_up_base_schedule(self) -> list[int]:
        """This deployment's configured sequence shape, before any user choice.

        The form previews an *unsaved* edit — "if I move this to 7 days, when do
        they go out?" — which it cannot ask the server without saving first. So
        it gets the shape and applies the same rule locally; without this it
        would be back to hard-coding day offsets in JavaScript, which is the
        defect above wearing different numbers.
        """
        return follow_up_service.parse_step_days(settings.follow_up_step_days)


class AutoSendPause(BaseModel):
    """How long to hold auto-send. Defaults to a day — long enough to be a real
    breather, short enough that forgetting to resume is not a silent opt-out."""

    hours: int = Field(default=24, ge=1, le=24 * 30)


class AutoSendStatus(BaseModel):
    """The live auto-send picture: the toggle, and what is actually happening.

    ``auto_send`` is what the user asked for; ``sending_now`` is what the policy
    permits this minute. They disagree during a pause, an unfinished trial, or a
    spent daily allowance — which is exactly when the UI needs to explain itself.
    """

    auto_send: bool
    sending_now: bool
    blocked_reason_code: str | None = None
    blocked_reason: str | None = None
    paused: bool = False
    paused_until: datetime | None = None
    trial_approvals: int = 0
    approvals_done: int = 0
    trial_remaining: int = 0
    daily_limit: int | None = None
    # Null when no ceiling is set — there is nothing to count against.
    auto_sent_today: int | None = None


class PlannedContactOut(BaseModel):
    """Who a planned email would go to, and how much the plan knows about them."""

    email: str | None = None
    name: str | None = None
    title: str | None = None
    confidence: float | None = None
    # known | cached | search — a contact on file, one from a shared crawl, or
    # none yet, in which case the live run has to go find one.
    source: str = "search"
    # Set when the contact exists but would be held back anyway (opted out, or
    # the address hard-bounced). The plan reports it rather than quietly
    # substituting a different recipient.
    warning: str | None = None


class PlannedSendOut(BaseModel):
    """One email autopilot intends to produce, with the reasoning behind it."""

    job_posting_id: int
    title: str | None = None
    company: str | None = None
    location: str | None = None
    remote: bool | None = None
    url: str | None = None
    posted_at: datetime | None = None

    fit_score: float | None = None
    llm_fit_score: float | None = None
    recommendation: str | None = None

    profile_id: int | None = None
    profile_label: str
    resume_id: int
    resume_label: str

    contact: PlannedContactOut
    from_address: str | None = None
    # inline | attachment | null — how the cover letter would travel, if any.
    cover_letter: str | None = None

    scheduled_at: datetime | None = None
    timezone: str
    # False when this message lands in the review queue instead of sending.
    sends_unreviewed: bool
    reasons: list[str] = Field(default_factory=list)


class SkippedPostingOut(BaseModel):
    """A posting the plan walked past, in the gate's own words."""

    job_posting_id: int
    title: str | None = None
    company: str | None = None
    reason: str


class AutopilotDryRunOut(BaseModel):
    """The next 24 hours of autopilot, before any of it happens.

    A forecast, not a commitment: the feed is refreshed at the start of every real
    run, so a better match found later can displace the bottom of ``sends``, and a
    contact still to be discovered may not be found at all.
    """

    generated_at: datetime
    window_hours: int
    active: bool
    # Why there is nothing to plan — autopilot off, no mailbox, no resume.
    blocked_reason: str | None = None

    auto_send: bool
    # What the policy permits this minute, which is not the toggle above.
    sends_unreviewed: bool
    send_policy_reason: str | None = None

    # The budget, decomposed: a single number cannot distinguish "you capped it
    # here" from "your mailbox is still warming up".
    budget: int
    daily_application_limit: int
    applied_today: int
    send_headroom: int
    warmup_day_limit: int
    # Set when a reputation pause — not the day's sending — is what zeroed the
    # headroom. The plan below is still real; it just cannot leave yet.
    send_hold_reason: str | None = None

    sends: list[PlannedSendOut] = Field(default_factory=list)
    skipped: list[SkippedPostingOut] = Field(default_factory=list)
    # Matches that cleared every gate but sit past today's budget.
    backlog: int = 0
    notes: list[str] = Field(default_factory=list)
    summary: str


class AutopilotRunResult(BaseModel):
    """What one autopilot cycle did — returned by the manual 'run now' endpoint."""

    status: str
    scanned: int = 0
    applied: int = 0
    skipped_low_fit: int = 0
    # Cleared the fit threshold but wasn't the candidate's line of work.
    skipped_irrelevant: int = 0
    # Right line of work, somewhere the candidate said they won't take it.
    skipped_location: int = 0
    skipped_no_contact: int = 0
    skipped_duplicate: int = 0
    # Raised partway through and were stepped over. Distinct from every skip
    # above, which are decisions; this one is a failure, and reporting it as a
    # skip would hide a broken crawler behind a number that reads as selectivity.
    failed: int = 0
    budget: int = 0
    # Applications made per profile name — which of their searches did the work.
    applied_by_profile: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    # True when the run was handed to a worker; False when it ran inline.
    queued: bool = False
