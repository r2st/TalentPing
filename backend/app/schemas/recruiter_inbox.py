"""Recruiter Inbox schemas — inbound mail we detected, and what we did about it.

The ordinary inbox is thread-shaped: conversations the product started. This one
is message-shaped, because first contact has no conversation yet — there is one
email from a stranger, a verdict about it, and at most one reply.

Every row carries the *decision* as well as the data: which band fired, on what
number, and why in the words the UI shows. A user asking "why did you answer that
one and not this one?" is answered from the row.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.core.enums import FilterEnum
from app.models.recruiter_email import (
    RecruiterEmailKind,
    RecruiterEmailStatus,
    ReplyRoute,
)
from app.schemas.smart_apply import SalaryInsightOut


class RecruiterInboxFilter(FilterEnum):
    """The list's headline filters, as the tabs present them."""

    ALL = "all"
    # Everything the product declined to answer on its own: flagged, or
    # classified as UNKNOWN. The only bucket that needs the user.
    NEEDS_YOU = "needs_you"
    DRAFTED = "drafted"
    REPLIED = "replied"
    # Job alerts, ATS noise and everything else recorded but not acted on.
    NOT_RECRUITER = "not_recruiter"


class OpportunityOut(BaseModel):
    """The opportunity a detected message describes, normalised for the card.

    Derived from ``extracted`` at read time by
    :mod:`app.services.opportunity`, and carried beside it rather than instead
    of it: ``extracted`` is what the classifier *said*, verbatim, and stays the
    audit record; this is what it means.
    """

    role_title: str | None = None
    company: str | None = None
    location: str | None = None
    # Tri-state. Null is "the message didn't say", which the routing gate treats
    # differently from "on-site" and so must the badge.
    remote: bool | None = None
    seniority: str | None = None

    # Pay as the recruiter phrased it, kept whole — the words around the figures
    # ("+ equity", "DOE", "depending on experience") are half the information.
    salary_text: str | None = None
    # The same string read as annual figures, when it holds any. Both null for
    # an hourly or monthly rate, which is not annualised on a guess.
    salary_min: int | None = None
    salary_max: int | None = None

    # The direct questions the recruiter asked, at most five. The reply agent is
    # briefed on these; showing them is how a user checks that it answered them.
    asks: list[str] = Field(default_factory=list)


class OpportunityIntelOut(BaseModel):
    """``GET /recruiter-inbox/{id}/intel`` — the market context for one message.

    Its own call, not a field on the detail, for the reason
    ``GET /jobs/{id}/intel`` is its own call: modelling a band reads and may
    write a globally shared cache row, which is affordable once for the message
    on screen and not affordable per row of a list.
    """

    email_id: int
    # Null when the classifier found no role title. A band modelled off nothing
    # would still be a number, printed in the same card as a real one — see
    # :func:`app.services.opportunity.market_context`.
    salary: SalaryInsightOut | None = None
    # The matched profile's salary floor and its name, so the card can say whose
    # floor it is. Null when that profile set none.
    floor: int | None = None
    floor_label: str | None = None
    # True when there is no floor to clear, which is not a judgement about pay.
    clears_floor: bool = True


class RecruiterEmailRow(BaseModel):
    """One detected message, summarised for the list."""

    id: int
    from_address: str
    from_name: str | None = None
    subject: str | None = None
    snippet: str | None = None
    received_at: datetime | None = None

    kind: RecruiterEmailKind
    classification_confidence: float = 0.0
    classified_by: str | None = None

    # Null for mail that was never an opportunity — a job alert has nothing to
    # route. Populated for everything actionable, including flags.
    route: ReplyRoute | None = None
    route_confidence: float | None = None
    status: RecruiterEmailStatus

    matched_profile_id: int | None = None
    matched_profile_name: str | None = None
    match_score: float | None = None
    match_reason: str | None = None
    flag_reason: str | None = None

    # What the classifier read off the message: role, company, location, comp.
    # The raw blob, in the classifier's own words.
    extracted: dict = Field(default_factory=dict)
    # The same facts normalised and with the pay string read as figures. Cheap
    # enough per row that the list can badge remote/comp without a second call.
    opportunity: OpportunityOut = Field(default_factory=OpportunityOut)

    # The generated reply. Approving or discarding it goes through the existing
    # /review endpoints — this is only the pointer, so a draft keeps exactly one
    # code path however the user reaches it.
    reply_email_id: int | None = None
    # Set once we engaged: the conversation is an ordinary thread from here.
    application_id: int | None = None
    read_at: datetime | None = None
    created_at: datetime

    # ---- Follow-ups ----
    # A recruiter we already answered who has written again. Deliberately not
    # folded into `status`: this row is still correctly REPLIED, and what changed
    # is that it now wants a human. The UI shows the banner off this flag and
    # puts the row in "Needs you" regardless of status.
    escalated: bool = False
    escalation_reason: str | None = None
    follow_up_count: int = 0
    last_follow_up_at: datetime | None = None

    # ---- What history and the role selection did ----
    # Signed, in the classifier's own 0..1 units. Non-zero means the user's own
    # earlier verdicts on this sender moved the reading — shown so the number is
    # explainable rather than mysterious.
    confidence_adjustment: float = 0.0
    # Why the reply is carrying the resume it is carrying, when the selector
    # chose one against this specific role.
    resume_choice_reason: str | None = None


class RecruiterEmailDetail(RecruiterEmailRow):
    """One message with its full body and the reply as it currently stands."""

    body_text: str | None = None
    reply_subject: str | None = None
    reply_body: str | None = None
    reply_status: str | None = None
    reply_note: str | None = None
    thread_id: int | None = None
    # Where the reply is actually addressed. Differs from `from_address` when the
    # sender's platform mails from an unmonitored address and names a human in
    # Reply-To, and the UI should show the address that will receive the answer.
    reply_to_address: str | None = None
    # The files queued to travel with the reply, and — when the resume isn't one
    # of them — why. A drafted reply that says nothing about its attachment is
    # indistinguishable from one that has none.
    reply_attachments: list[str] = Field(default_factory=list)
    reply_attachment_note: str | None = None

    # What the *recruiter* sent us, as opposed to what our reply will carry.
    # A job spec, a contract, a take-home brief: the message is often the
    # covering note and the attachment is the thing worth reading.
    attachments: list[str] = Field(default_factory=list)
    # The message id these open against, via
    # ``GET /inbox/emails/{id}/attachments/{n}``. Null until the conversation
    # exists — a detected message that has not been threaded yet has nothing to
    # serve bytes against, and the names are shown without being clickable
    # rather than offering a button that can only fail.
    attachment_email_id: int | None = None


class RecruiterInboxCounts(BaseModel):
    """Headline numbers, computed over everything *before* filters are applied.

    Same contract the ordinary inbox keeps: the chips show what they would
    reveal, not what is left after the current filter.
    """

    detected: int = 0
    unread: int = 0
    # Counts escalated rows too, so it matches what the "Needs you" tab shows.
    needs_you: int = 0
    drafted: int = 0
    replied: int = 0
    not_recruiter: int = 0
    escalated: int = 0
    by_kind: dict[str, int] = Field(default_factory=dict)


class RecruiterInboxOut(BaseModel):
    counts: RecruiterInboxCounts
    emails: list[RecruiterEmailRow] = Field(default_factory=list)
    # How many rows match the *current filters*, ignoring the page — which is
    # not `counts.detected`, since the counts deliberately ignore filters. This
    # is what lets a page that came back full say so; a truncated list that
    # looks exactly like a complete one is the failure paging introduces.
    total: int = 0
    # Whether the feature is switched on for this deployment and this user, so
    # the page can explain itself rather than showing an unexplained empty list.
    enabled: bool = False
    auto_reply_enabled: bool = False
    server_enabled: bool = False
    last_scan_at: datetime | None = None


class RecruiterScanOut(BaseModel):
    """Result of scanning now."""

    detected: int = 0
    # How many message ids Gmail returned for the search — the denominator every
    # other number here is a share of.
    listed: int = 0
    examined: int = 0
    # True when the work went to a worker rather than running in the request.
    dispatched: bool = False
    # How many connected mailboxes this scan covered. Every counter above is a
    # sum across them, so without this "12 examined" is ambiguous between one
    # busy mailbox and three quiet ones — and a user who has just added a second
    # address is precisely the one asking whether it was read.
    mailboxes_scanned: int = 0
    # Left for the next scan because the per-run cap was reached. Surfaced so the
    # UI can say so rather than implying the whole mailbox was covered.
    deferred: int = 0
    skipped_known: int = 0
    skipped_own_thread: int = 0
    skipped_from_self: int = 0
    skipped_bounce: int = 0
    skipped_opt_out: int = 0
    # Out-of-office notices, refused on their headers. Surfaced for the same
    # reason as the rest: a scan that examined twenty messages and detected none
    # has to be able to say why.
    skipped_auto_reply: int = 0
    # The Gmail search that produced all of the above. "Why is that email
    # missing?" is answered by the query more often than by any of the filters,
    # and the user can only ask about a query they can see.
    query: str = ""
    error: str | None = None


class RecruiterPreferenceIn(BaseModel):
    """PATCH body for the two switches."""

    enabled: bool | None = None
    auto_reply_enabled: bool | None = None


class RecruiterPreferenceOut(BaseModel):
    enabled: bool = False
    auto_reply_enabled: bool = False
    # False when the deployment has not enabled the feature at all, in which case
    # the user's own switches are inert and the UI should say so.
    server_enabled: bool = False
    # False when the deployment forbids unreviewed replies regardless of the
    # user's preference — the UI disables that toggle rather than lying about it.
    server_auto_enabled: bool = False
    last_scan_at: datetime | None = None
    detected_count: int = 0
    replied_count: int = 0


class RematchIn(BaseModel):
    """Force a specific profile, or omit it to re-run the automatic match."""

    profile_id: int | None = None


# --------------------------------------------------------------------------- #
# Stats                                                                        #
# --------------------------------------------------------------------------- #


class RecruiterStatsTotals(BaseModel):
    """Headline numbers over the window.

    Scan counters (``scans``/``listed``/``examined``/``deferred``) come from the
    scan-run log; the rest come from the messages themselves. They are different
    denominators over different time axes and are deliberately not presented as
    one funnel — see the endpoint's docstring.
    """

    scans: int = 0
    # Message ids Gmail returned across every scan. The honest answer to "how
    # many emails did you look at?", and much larger than `examined`, because
    # listing is cheap and fetching is what the filters exist to avoid.
    listed: int = 0
    examined: int = 0
    detected: int = 0
    classified_recruiter: int = 0
    replied: int = 0
    # Replies that went out *without the user reading them* — the AUTO band
    # only. A draft the user approved is a reply they sent, and counting it here
    # would overstate exactly the number they most want to be honest.
    auto_sent: int = 0
    drafts_pending: int = 0
    flagged: int = 0
    escalated: int = 0
    dismissed: int = 0
    failed: int = 0
    deferred: int = 0


class SkipReason(BaseModel):
    """One reason messages were passed over, and how often."""

    reason: str
    # Plain English. The raw counter names are for the log, not for a person.
    label: str
    count: int = 0


class StatsTrendPoint(BaseModel):
    """One bucket of the time series."""

    period: date
    label: str
    scanned: int = 0
    detected: int = 0
    recruiter: int = 0
    replied: int = 0
    auto_sent: int = 0
    flagged: int = 0


class StatsPushState(BaseModel):
    """Whether push is registered, and whether it is actually delivering.

    Two separate claims on purpose. ``healthy`` says a watch exists and has not
    expired; ``covering`` says it has been heard from recently enough that the
    5-minute beat sweep stands down for this mailbox. A subscription can be the
    first without being the second — that is the exact failure mode the fallback
    exists for, and the UI should be able to say which one is wrong.
    """

    configured: bool = False
    # Both are claims about *every* connected mailbox — see the router. A user
    # with a pushing primary and a silent second mailbox is not "healthy", and
    # saying so hid the only mailbox that needed attention.
    healthy: bool = False
    covering: bool = False
    notifications: int = 0
    last_notified_at: datetime | None = None
    # The denominator for the two booleans above, so "3 of 4 mailboxes" can be
    # said out loud instead of collapsing to a bare false.
    mailboxes_total: int = 0
    mailboxes_pushing: int = 0
    # How the scans in this window were actually triggered. The honest answer to
    # "is push doing the work?".
    scans_from_push: int = 0
    scans_from_beat: int = 0
    scans_manual: int = 0


class RecruiterStatsOut(BaseModel):
    days: int
    bucket: str
    totals: RecruiterStatsTotals
    # Ranked, largest first.
    skipped: list[SkipReason] = Field(default_factory=list)
    # Oldest first, one point per bucket that has anything in it.
    trend: list[StatsTrendPoint] = Field(default_factory=list)
    push: StatsPushState
