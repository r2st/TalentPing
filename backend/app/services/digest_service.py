"""The weekly digest — what the agent did, and what is waiting on the user.

Building it and sending it are kept apart on purpose. :func:`build` is a pure
read that any caller can render (the preview endpoint shows the user exactly
what Monday's mail will say, before it goes); :func:`send` is the only function
here that touches a mailbox.

**The digest is mail the user pays for.** It goes out through their own Gmail,
so it consumes the same warm-up allowance as outreach — which means it is
counted against the mailbox (``reputation_service.record_send``) and refused
while the mailbox is paused. It is deliberately *not* refused when the mailbox
is merely at its daily ceiling: one message a week to the account's own owner
does not endanger a reputation, and the week the agent is busiest is precisely
the week its digest matters most. The ceiling exists to protect strangers'
inboxes from the agent, not the user's inbox from their own product.

Everything in the digest is a count the user could already have found in the
app. The value is not the numbers — it is that they arrive without the user
having to open anything, which is what turns a draft that has waited four days
into a draft that gets approved on Monday morning.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.application import (
    CLOSED_STATUSES,
    ENGAGED_STATUSES,
    INTERVIEWING_STATUSES,
    Application,
    ApplicationStatus,
)
from app.models.digest import DigestPreference
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.follow_up import FollowUp, FollowUpStatus
from app.models.job import JobPosting, JobStatus
from app.models.recruiter import Recruiter
from app.models.user import User
from app.services import gmail_accounts, gmail_service, reputation_service

logger = logging.getLogger(__name__)

PERIOD_DAYS = 7

# The shortest gap between two digests, whatever the schedule says. This is the
# guard against the hourly beat sending one every hour of Monday, and it is all
# that guard has to be — the weekly spacing comes from the schedule itself.
MIN_GAP = timedelta(hours=24)

# How many drafts and replies are named individually before the digest stops
# listing and starts counting. Five is about where a list stops being a prompt
# to act and starts being a wall to skim past.
LIST_LIMIT = 5

# A week's change in reply rate below this is not worth a sentence — it is two
# recruiters happening to answer.
NOTABLE_RATE_CHANGE = 0.05

# An application that went out this long ago, has heard nothing, and has no
# follow-up on the books is *stalled* — the pipeline number that costs a
# candidate real interviews and that no screen in this product ever named.
# Fourteen days rather than seven: a recruiter who has not answered in a week is
# usually just a recruiter who had a week, and the digest that cries stall at
# seven days trains the user to ignore the word.
STALL_DAYS = 14

# How many of the week's new roles are named individually. Three rather than
# :data:`LIST_LIMIT`'s five: the drafts list is work the user owes somebody, and
# this one is an invitation. A long invitation is a list to skim past.
OPPORTUNITY_LIMIT = 3


@dataclass
class DraftLine:
    """One draft waiting for approval, as the digest names it."""

    email_id: int
    company: str | None
    subject: str | None
    kind: str  # reply | outreach
    waiting_days: int = 0


@dataclass
class OpportunityLine:
    """One role found this week, as the digest names it."""

    job_id: int
    title: str | None
    company: str | None
    location: str | None
    fit_score: float | None = None
    remote: bool = False

    @property
    def where(self) -> str:
        """The place, as one phrase — "Remote" wins over whatever city it lists."""
        if self.remote:
            return "Remote"
        return (self.location or "").strip() or "Location not stated"


@dataclass
class PipelineHealth:
    """The shape of the pipeline right now, not the week's activity.

    Everything else in the digest is a *flow* — what happened in seven days.
    This is the *stock*, and it is the half the user cannot reconstruct from
    the other half: "you sent eleven emails" says nothing about whether forty
    applications are quietly rotting.

    ``stalled`` is the number this exists for. An application that went out,
    heard nothing, and has no follow-up scheduled is not in any queue, does not
    appear in any count of work waiting, and will sit there until the candidate
    happens to scroll past it. See :data:`STALL_DAYS`.
    """

    awaiting_reply: int = 0
    in_conversation: int = 0
    interviewing: int = 0
    offers: int = 0
    closed: int = 0
    stalled: int = 0

    @property
    def active(self) -> int:
        """Everything still live — the number the dashboard leads with."""
        return self.awaiting_reply + self.in_conversation + self.interviewing

    @property
    def total(self) -> int:
        return self.active + self.closed

    @property
    def stall_rate(self) -> float:
        """Stalled as a share of what is still waiting. 0 when nothing is."""
        return _rate(self.stalled, self.awaiting_reply)


@dataclass
class Digest:
    """One week of activity, ready to render. Every field is set even at zero."""

    period_start: datetime
    period_end: datetime

    # ---- What the agent did ----
    sent: int = 0
    follow_ups_sent: int = 0
    jobs_found: int = 0

    # ---- What came back ----
    replies: int = 0
    interviews: int = 0

    # ---- What is waiting on the user ----
    drafts_waiting: int = 0
    drafts: list[DraftLine] = field(default_factory=list)
    # Inbound mail with nothing sent after it. The most time-sensitive item in
    # the digest: a recruiter is waiting on the other end of each one.
    unanswered_replies: int = 0

    # ---- What is queued next ----
    queued_emails: int = 0
    follow_ups_due: int = 0

    # ---- What is worth looking at ----
    # The week's best new roles, named. A count of "31 new roles found" is a
    # number; three titles with the companies attached is a reason to open the
    # app, which is the only thing this email is trying to buy.
    opportunities: list[OpportunityLine] = field(default_factory=list)

    # ---- The shape of the pipeline ----
    pipeline: PipelineHealth = field(default_factory=PipelineHealth)

    # ---- One number that moved ----
    response_rate: float = 0.0
    previous_response_rate: float = 0.0
    headline: str = ""

    @property
    def is_quiet(self) -> bool:
        """Nothing happened and nothing is waiting.

        A digest reporting seven zeroes trains the user to delete it unread,
        which costs the feature the one thing it exists for. The task skips
        these rather than sending them.
        """
        return not any(
            (
                self.sent,
                self.follow_ups_sent,
                self.replies,
                self.drafts_waiting,
                self.unanswered_replies,
                self.queued_emails,
                self.jobs_found,
                # A stalled application is the one thing here that can be true in
                # a week where literally nothing else happened — which is exactly
                # the week it most needs saying. Without this, the digest that
                # would have caught a dead pipeline is the digest that gets
                # skipped for being quiet.
                self.pipeline.stalled,
            )
        )


def get_or_create(db: Session, user: User) -> DigestPreference:
    """Every user has exactly one digest row; make it on first read.

    ``user_id`` is unique, and read-then-insert does not expect to lose. Every
    entry point here is one a user can reach twice at once — the digest screen,
    the settings step of onboarding, and :func:`send` itself — and two arriving
    together both load the relationship as ``None`` and both insert. The loser
    took the unique constraint: a 500 on a page that was merely opened, and out
    of :func:`send` an exception from a function whose whole contract is that it
    returns a status instead of raising one, which the beat sweep leans on to
    keep going through a bad row.

    So losing is a re-read rather than a failure. The insert gets its own
    savepoint for the reason ``_persist_detected`` does: without one the failed
    flush leaves the session unusable, and the caller's transaction — a request
    part-way through its work, or a sweep holding ninety-nine other users —
    dies with it.

    The error is only swallowed once the row it complained about has actually
    been found. A conflict this cannot then read is not a race that resolved
    itself, and pretending otherwise would return ``None`` to callers typed to
    receive a row.
    """
    pref = user.digest_preference
    if pref is not None:
        return pref

    savepoint = db.begin_nested()
    pref = DigestPreference(user_id=user.id)
    db.add(pref)
    try:
        db.flush()
    except IntegrityError:
        savepoint.rollback()
        existing = db.scalar(
            select(DigestPreference).where(DigestPreference.user_id == user.id)
        )
        if existing is None:
            raise
        logger.info(
            "digest row for user %s was created concurrently; adopting it", user.id
        )
        return existing
    savepoint.commit()
    db.commit()
    db.refresh(pref)
    return pref


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips naive datetimes; the arithmetic here needs tz-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def unsubscribe_url(pref: DigestPreference) -> str:
    """The one-click opt-out link, carrying the row's own secret.

    Built from ``TRACKING_BASE_URL`` — the API origin, the same one the pixel
    and click links use — because this link has the same requirement they do:
    it is clicked from a mail client, so it has to be an absolute URL that
    resolves from outside the app.

    It used to be built from ``COMPLIANCE_UNSUBSCRIBE_BASE_URL``, which is a
    different endpoint (the CAN-SPAM opt-out recruiters click, at
    ``/unsubscribe``) rather than a base to hang paths off. Appending
    ``/digest`` to it produced ``/api/v1/unsubscribe/digest``, and the route is
    ``/api/v1/digest/unsubscribe`` — the two segments the wrong way round, so
    every opt-out link this product has ever mailed answered 404.

    That is worse than an inconvenience. The URL also goes in the
    ``List-Unsubscribe`` header, and a one-click header that dead-ends is
    exactly what mailbox providers score a sender down for; the user's only
    remaining exit from the mail is the spam button, on their own mailbox, in
    the reputation this whole subsystem is built to protect.
    """
    base = settings.tracking_base_url.rstrip("/")
    return f"{base}/digest/unsubscribe?{urlencode({'token': pref.unsubscribe_token})}"


def _slot_start(now: datetime, weekday: int, hour: int) -> datetime:
    """The most recent scheduled slot at or before *now*.

    The start of the week the user is currently *in*, as they defined it. "Have
    we sent this week's digest?" is then one comparison against it.
    """
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(
        days=(now.weekday() - weekday) % 7
    )
    if slot > now:  # today is the day, but the hour hasn't come round yet
        slot -= timedelta(days=7)
    return slot


def is_due(pref: DigestPreference, *, now: datetime | None = None) -> bool:
    """Whether *pref*'s digest should go out at *now*.

    Two guards. :data:`MIN_GAP` stops the hourly beat sending a digest every
    hour of Monday; past that, one is due whenever the last send predates the
    current scheduled slot.

    **The gap is measured from the last attempt, not the last success.** A send
    that fails leaves ``last_sent_at`` alone on purpose — the week's digest has
    not gone out and should still go out — but the only other thing the beat
    consulted was that same column, so a mailbox that is disconnected, revoked
    or paused was retried on every tick: twenty-four full digest builds and
    twenty-four rejected Gmail calls a day, against an account that cannot
    receive mail, for as long as it stayed broken. ``last_attempt_at`` is what
    tells "we have not sent this week" apart from "we have not sent this week
    and just tried a minute ago". Late is still much better than silent, so a
    failed attempt only spaces the retries out to :data:`MIN_GAP` — it does not
    give up on the week.

    A digest that misses its hour — worker down, deploy in progress — still goes
    out later that week rather than being skipped until the next Monday. Late is
    a much smaller failure than silent.

    **And a late one does not move the schedule.** This used to be a pair of
    day-counts: a six-day floor since the last send, then a weekday/hour test
    with a seven-day override for a missed window. The floor could not tell "we
    already sent this week" from "the next slot is close", so a digest that went
    out on Wednesday because the worker was down on Monday found the following
    Monday only four days later and refused it — then fired on the Wednesday
    when the seven days were up, and pinned itself there. One outage moved a
    user's "Monday morning" to Wednesday afternoon permanently, and nothing in
    the UI explained why. Comparing against the slot instead of against a
    duration re-syncs on the next scheduled day.
    """
    now = now or datetime.now(UTC)
    if not pref.enabled:
        return False

    last = _aware(pref.last_sent_at)
    attempted = _aware(pref.last_attempt_at)
    recent = max([t for t in (last, attempted) if t is not None], default=None)
    if recent is not None and (now - recent) < MIN_GAP:
        return False
    if last is None:
        # A brand-new row does not wait up to a week for its first digest: the
        # first one is the one that teaches the user the feature exists.
        return True
    return last < _slot_start(now, pref.weekday, pref.hour)


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def _sent_between(db: Session, user_id: int, start: datetime, end: datetime) -> int:
    return (
        db.scalar(
            select(func.count(Email.id))
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .join(Application, EmailThread.application_id == Application.id)
            .where(
                Application.user_id == user_id,
                Email.direction == EmailDirection.SENT,
                Email.status == EmailStatus.SENT,
                Email.sent_at >= start,
                Email.sent_at < end,
            )
        )
        or 0
    )


def _cohort_response_rate(
    db: Session, user_id: int, start: datetime, end: datetime
) -> tuple[int, float]:
    """Applications created in the window, and how many of them came back.

    Cohorted on creation for the same reason the trend chart is: "of what went
    out that week, how much answered" is the only version of this number that
    survives being compared to the week before it.
    """
    statuses = list(
        db.scalars(
            select(Application.status).where(
                Application.user_id == user_id,
                Application.created_at >= start,
                Application.created_at < end,
            )
        )
    )
    engaged = sum(1 for s in statuses if s in ENGAGED_STATUSES)
    return len(statuses), _rate(engaged, len(statuses))


def _unanswered_reply_count(db: Session, user_id: int) -> int:
    """Threads whose newest message is inbound — someone is waiting on us.

    Computed by comparing the latest message id per direction rather than by
    timestamp: inbound mail carries the sender's ``Date`` header, which is
    routinely skewed and occasionally in the future, and a clock-skewed
    recruiter should not be able to make a thread look answered.
    """
    latest_inbound = (
        select(
            EmailThread.application_id.label("application_id"),
            func.max(Email.id).label("inbound_id"),
        )
        .join(Email, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.user_id == user_id,
            Email.direction == EmailDirection.RECEIVED,
        )
        .group_by(EmailThread.application_id)
        .subquery()
    )
    latest_outbound = (
        select(
            EmailThread.application_id.label("application_id"),
            func.max(Email.id).label("outbound_id"),
        )
        .join(Email, Email.thread_id == EmailThread.id)
        .where(
            Email.direction == EmailDirection.SENT,
            Email.status.in_([EmailStatus.SENT, EmailStatus.QUEUED]),
        )
        .group_by(EmailThread.application_id)
        .subquery()
    )
    return (
        db.scalar(
            select(func.count())
            .select_from(latest_inbound)
            .outerjoin(
                latest_outbound,
                latest_inbound.c.application_id == latest_outbound.c.application_id,
            )
            .where(
                (latest_outbound.c.outbound_id.is_(None))
                | (latest_outbound.c.outbound_id < latest_inbound.c.inbound_id)
            )
        )
        or 0
    )


def _drafts(db: Session, user_id: int, now: datetime) -> tuple[int, list[DraftLine]]:
    """Everything sitting in the review queue, and the first few by name."""
    rows = db.execute(
        select(Email, Recruiter)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .join(Recruiter, Application.recruiter_id == Recruiter.id)
        .where(
            Application.user_id == user_id,
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.DRAFT,
        )
        .order_by(Email.id)
    ).all()

    inbound_threads = set(
        db.scalars(
            select(Email.thread_id)
            .where(
                Email.thread_id.in_([e.thread_id for e, _ in rows] or [0]),
                Email.direction == EmailDirection.RECEIVED,
            )
            .distinct()
        )
    )

    lines = [
        DraftLine(
            email_id=email.id,
            company=recruiter.company,
            subject=email.subject,
            kind="reply" if email.thread_id in inbound_threads else "outreach",
            waiting_days=max(
                0, (now - (_aware(email.created_at) or now)).days
            ),
        )
        for email, recruiter in rows[:LIST_LIMIT]
    ]
    return len(rows), lines


_AWAITING_STATUSES = (
    ApplicationStatus.QUEUED,
    ApplicationStatus.OUTREACH_SENT,
    ApplicationStatus.FOLLOW_UP,
)
_CONVERSATION_STATUSES = (
    ApplicationStatus.REPLIED,
    ApplicationStatus.INTERESTED,
)


def _opportunities(
    db: Session, user_id: int, start: datetime
) -> list[OpportunityLine]:
    """The week's best new roles, best first.

    Ordered on the deterministic ``fit_score`` rather than on Scout's re-rank:
    the re-rank only ever runs on a shortlist, so ordering by it would sort the
    handful Scout happened to see above everything it never looked at. Rows with
    no score at all sort last rather than being dropped — a posting the scorer
    could not read is still a role the candidate might want.

    Cross-board duplicates and postings an autopilot gate already passed over
    are excluded: the digest should not invite the user to look at a role the
    agent has already decided against on their behalf, and it certainly should
    not name the same role twice under two boards' spellings.
    """
    rows = db.scalars(
        select(JobPosting)
        .where(
            JobPosting.user_id == user_id,
            JobPosting.created_at >= start,
            JobPosting.duplicate_of_id.is_(None),
            JobPosting.screened_out_at.is_(None),
            JobPosting.status != JobStatus.DISMISSED,
        )
        .order_by(
            JobPosting.fit_score.is_(None),
            JobPosting.fit_score.desc(),
            JobPosting.id.desc(),
        )
        .limit(OPPORTUNITY_LIMIT)
    ).all()
    return [
        OpportunityLine(
            job_id=row.id,
            title=row.title,
            company=row.company,
            location=row.location,
            fit_score=row.fit_score,
            remote=bool(row.remote),
        )
        for row in rows
    ]


def _pipeline_health(db: Session, user_id: int, now: datetime) -> PipelineHealth:
    """Where every application stands, and how many have gone quiet.

    One pass over ``(status, id)`` rather than six counting queries: the whole
    point of this block is a shape, and six round trips that each see a slightly
    different moment can produce a shape whose parts do not add up.

    The stall test deliberately reads the *last outbound message* rather than
    ``Application.updated_at``. ``updated_at`` moves for reasons that are not a
    send — a status edit, a re-score, an inbound classification — so a pipeline
    kept warm by background bookkeeping would report itself as fresh while no
    human had heard from us in a month.
    """
    health = PipelineHealth()
    rows = db.execute(
        select(Application.id, Application.status).where(Application.user_id == user_id)
    ).all()

    awaiting_ids: list[int] = []
    for app_id, status in rows:
        if status in CLOSED_STATUSES:
            health.closed += 1
        elif status == ApplicationStatus.OFFER:
            health.offers += 1
            health.interviewing += 1
        elif status in INTERVIEWING_STATUSES:
            health.interviewing += 1
        elif status in _CONVERSATION_STATUSES:
            health.in_conversation += 1
        elif status in _AWAITING_STATUSES:
            health.awaiting_reply += 1
            awaiting_ids.append(app_id)

    if not awaiting_ids:
        return health

    cutoff = now - timedelta(days=STALL_DAYS)
    # Applications with a follow-up still on the books are not stalled — the
    # agent is going to chase them, and telling the user to intervene would be
    # telling them to duplicate their own agent's work.
    scheduled = set(
        db.scalars(
            select(FollowUp.application_id).where(
                FollowUp.application_id.in_(awaiting_ids),
                FollowUp.status == FollowUpStatus.SCHEDULED,
            )
        )
    )
    last_sent = {
        app_id: sent_at
        for app_id, sent_at in db.execute(
            select(EmailThread.application_id, func.max(Email.sent_at))
            .join(Email, Email.thread_id == EmailThread.id)
            .where(
                EmailThread.application_id.in_(awaiting_ids),
                Email.direction == EmailDirection.SENT,
                Email.status == EmailStatus.SENT,
            )
            .group_by(EmailThread.application_id)
        ).all()
    }

    for app_id in awaiting_ids:
        if app_id in scheduled:
            continue
        sent_at = _aware(last_sent.get(app_id))
        # No outbound message at all means nothing has been said yet, so there
        # is nothing to be waiting on. A queued application is not a stalled one.
        if sent_at is not None and sent_at < cutoff:
            health.stalled += 1
    return health


def _headline(digest: Digest, previous_cohort: int) -> str:
    """The one number worth putting at the top, in a sentence.

    Ordered by what the user can act on this week, not by size. An unanswered
    recruiter beats a rate change; a rate change beats a raw volume count.
    """
    if digest.unanswered_replies:
        plural = "y" if digest.unanswered_replies == 1 else "ies"
        return (
            f"{digest.unanswered_replies} recruiter repl{plural} "
            f"{'is' if digest.unanswered_replies == 1 else 'are'} still unanswered."
        )
    if digest.drafts_waiting:
        return (
            f"{digest.drafts_waiting} draft"
            f"{'' if digest.drafts_waiting == 1 else 's'} waiting for your approval."
        )

    # A stall is chronic where the two above are acute, so it ranks below them —
    # but above every rate and volume line, because it is the only headline here
    # that names work the user has *forgotten* rather than work they have
    # postponed.
    if digest.pipeline.stalled:
        n = digest.pipeline.stalled
        return (
            f"{n} application{'' if n == 1 else 's'} "
            f"{'has' if n == 1 else 'have'} gone quiet — no reply in "
            f"{STALL_DAYS} days and no follow-up scheduled."
        )

    change = digest.response_rate - digest.previous_response_rate
    if previous_cohort and abs(change) >= NOTABLE_RATE_CHANGE:
        direction = "up" if change > 0 else "down"
        return (
            f"Reply rate is {direction} to {digest.response_rate:.0%} "
            f"from {digest.previous_response_rate:.0%} the week before."
        )
    if digest.sent:
        return (
            f"{digest.sent} email{'' if digest.sent == 1 else 's'} went out this "
            f"week and {digest.replies} came back."
        )
    if digest.jobs_found:
        return f"{digest.jobs_found} new roles found, none emailed yet."
    return "A quiet week — nothing went out and nothing is waiting."


def _fit_label(score: float | None) -> str:
    """The fit score as the digest states it, or nothing at all.

    Never renders a "0%" for a posting that was simply never scored: an unscored
    role is not a bad one, and a zero next to a title is the fastest way to make
    a user stop trusting the number.
    """
    return f"{round(score)}% match" if score is not None else ""


def build(db: Session, user: User, *, now: datetime | None = None) -> Digest:
    """Everything last week's digest needs to say. Reads only."""
    now = now or datetime.now(UTC)
    start = now - timedelta(days=PERIOD_DAYS)
    previous_start = start - timedelta(days=PERIOD_DAYS)

    digest = Digest(period_start=start, period_end=now)

    digest.sent = _sent_between(db, user.id, start, now)
    # Counted from the email, not from ``FollowUp.status``: that status reads
    # SENT as soon as the step is actioned, including when the message was
    # parked as a draft for review — which is a message the user still has to
    # approve, and it is listed as such further down this same digest. The
    # window is the delivery time for the same reason ``_sent_between`` uses it:
    # ``updated_at`` moves for reasons that are not a send.
    digest.follow_ups_sent = (
        db.scalar(
            select(func.count(FollowUp.id))
            .join(Application, FollowUp.application_id == Application.id)
            .join(Email, FollowUp.email_id == Email.id)
            .where(
                Application.user_id == user.id,
                Email.status == EmailStatus.SENT,
                Email.sent_at >= start,
                Email.sent_at < now,
            )
        )
        or 0
    )
    digest.jobs_found = (
        db.scalar(
            select(func.count(JobPosting.id)).where(
                JobPosting.user_id == user.id,
                JobPosting.created_at >= start,
                JobPosting.duplicate_of_id.is_(None),
            )
        )
        or 0
    )

    cohort, digest.response_rate = _cohort_response_rate(db, user.id, start, now)
    previous_cohort, digest.previous_response_rate = _cohort_response_rate(
        db, user.id, previous_start, start
    )

    replied_statuses = list(
        db.scalars(
            select(Application.status).where(
                Application.user_id == user.id,
                Application.updated_at >= start,
            )
        )
    )
    digest.replies = sum(1 for s in replied_statuses if s in ENGAGED_STATUSES)
    digest.interviews = sum(1 for s in replied_statuses if s in INTERVIEWING_STATUSES)

    digest.drafts_waiting, digest.drafts = _drafts(db, user.id, now)
    digest.unanswered_replies = _unanswered_reply_count(db, user.id)

    digest.queued_emails = (
        db.scalar(
            select(func.count(Email.id))
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .join(Application, EmailThread.application_id == Application.id)
            .where(
                Application.user_id == user.id,
                Email.direction == EmailDirection.SENT,
                Email.status == EmailStatus.QUEUED,
            )
        )
        or 0
    )
    digest.follow_ups_due = (
        db.scalar(
            select(func.count(FollowUp.id))
            .join(Application, FollowUp.application_id == Application.id)
            .where(
                Application.user_id == user.id,
                FollowUp.status == FollowUpStatus.SCHEDULED,
                FollowUp.scheduled_at < now + timedelta(days=PERIOD_DAYS),
            )
        )
        or 0
    )

    digest.opportunities = _opportunities(db, user.id, start)
    digest.pipeline = _pipeline_health(db, user.id, now)

    digest.headline = _headline(digest, previous_cohort)
    return digest


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def subject_line(digest: Digest) -> str:
    """What the user sees in their inbox list, before opening anything.

    The most actionable count goes in the subject rather than a generic "Your
    weekly digest": the whole failure mode this feature fixes is mail the user
    does not open.
    """
    if digest.unanswered_replies:
        return (
            f"{settings.app_name}: {digest.unanswered_replies} "
            f"repl{'y' if digest.unanswered_replies == 1 else 'ies'} waiting on you"
        )
    if digest.drafts_waiting:
        return (
            f"{settings.app_name}: {digest.drafts_waiting} "
            f"draft{'' if digest.drafts_waiting == 1 else 's'} to approve"
        )
    if digest.pipeline.stalled:
        return (
            f"{settings.app_name}: {digest.pipeline.stalled} application"
            f"{'' if digest.pipeline.stalled == 1 else 's'} have gone quiet"
        )
    return f"{settings.app_name}: your week — {digest.sent} sent, {digest.replies} replies"


def _draft_lines(digest: Digest) -> list[str]:
    lines = [
        f"  · {d.company or 'Unknown company'} — {d.subject or '(no subject)'}"
        f" [{d.kind}, waiting {d.waiting_days}d]"
        for d in digest.drafts
    ]
    remaining = digest.drafts_waiting - len(digest.drafts)
    if remaining > 0:
        lines.append(f"  · …and {remaining} more")
    return lines


def _opportunity_lines(digest: Digest) -> list[str]:
    lines = []
    for job in digest.opportunities:
        fit = _fit_label(job.fit_score)
        suffix = " · ".join(filter(None, (job.where, fit)))
        lines.append(
            f"  · {job.title or 'Untitled role'}"
            f" — {job.company or 'Unknown company'}"
            + (f" [{suffix}]" if suffix else "")
        )
    return lines


def render_text(digest: Digest, *, app_url: str, opt_out_url: str) -> str:
    """The plain-text body. The HTML part is generated from the same content."""
    parts = [digest.headline, ""]

    if digest.drafts_waiting:
        parts.append(f"Waiting for you ({digest.drafts_waiting}):")
        parts.extend(_draft_lines(digest))
        parts.append("")

    parts.append("Last 7 days")
    parts.append(f"  Emails sent          {digest.sent}")
    parts.append(f"  Follow-ups sent      {digest.follow_ups_sent}")
    parts.append(f"  Replies              {digest.replies}")
    parts.append(f"  Interviews           {digest.interviews}")
    parts.append(f"  New roles found      {digest.jobs_found}")
    parts.append(f"  Reply rate           {digest.response_rate:.0%}")
    parts.append("")

    health = digest.pipeline
    if health.total:
        parts.append("Your pipeline")
        parts.append(f"  Waiting on a reply   {health.awaiting_reply}")
        parts.append(f"  In conversation      {health.in_conversation}")
        parts.append(f"  Interviewing         {health.interviewing}")
        if health.offers:
            parts.append(f"  Offers               {health.offers}")
        parts.append(f"  Closed               {health.closed}")
        if health.stalled:
            parts.append(
                f"  Gone quiet           {health.stalled}"
                f"  (no reply in {STALL_DAYS}d, nothing scheduled)"
            )
        parts.append("")

    if digest.opportunities:
        parts.append(f"New roles worth a look ({digest.jobs_found} found)")
        parts.extend(_opportunity_lines(digest))
        parts.append("")

    if digest.queued_emails or digest.follow_ups_due:
        parts.append("Queued for the week ahead")
        parts.append(f"  Emails ready to send {digest.queued_emails}")
        parts.append(f"  Follow-ups due       {digest.follow_ups_due}")
        parts.append("")

    parts.append(f"Open {settings.app_name}: {app_url}")
    parts.append("")
    parts.append(f"Stop these weekly emails: {opt_out_url}")
    return "\n".join(parts)


def _esc(value: str | None) -> str:
    """HTML-escape a value, quotes included.

    The quotes matter even though nothing untrusted is currently interpolated
    into an attribute here. Every string this template renders came from
    somewhere outside the product — a recruiter's subject line, a board's job
    title, a company name scraped off a careers page — and the difference
    between "safe" and "unsafe" is one future edit that moves one of them from
    a text position into an ``href`` or a ``title``. Escaping both quote
    characters costs nothing and removes the class of bug entirely, rather than
    leaving it resting on which quote style the template happens to use.
    """
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# Mail clients are not browsers. The rules this template keeps to, and why each
# one is not a style preference:
#
# * **Every style is inline.** Gmail's web client strips ``<style>`` blocks in
#   forwarded and clipped messages; an inline attribute survives.
# * **Layout is tables, not flex or grid.** Outlook's Word rendering engine
#   supports neither, and a two-column row built with ``display:flex`` renders
#   as two stacked full-width blocks there.
# * **No images and no external requests.** The digest carries no tracking
#   pixel — it is mail from the user's own mailbox to that mailbox's owner, and
#   measuring whether someone opened their own weekly summary is both useless
#   and a thing we would then have to disclose. No image also means nothing to
#   "display images below" past, so the mail is complete on first render.
# * **Colour is never the only carrier.** Anything the stall block says in red
#   it also says in words, because a fifth of mail clients render this in a dark
#   theme that re-maps the palette, and some strip colour entirely.
#
# It stays visually plain on purpose. Dressing a weekly summary up as a
# newsletter is how a personal mailbox teaches Gmail it sends newsletters, and
# this account's deliverability is the product.
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MUTED = "#5b6472"
_INK = "#111827"
_RULE = "#e5e7eb"
_ALERT = "#b42318"


def _stat_rows(pairs: tuple[tuple[str, object], ...], *, accent: str | None = None) -> str:
    """A two-column label/value table body, as every block here renders one."""
    return "".join(
        f"<tr><td style=\"padding:3px 20px 3px 0;color:{_MUTED};"
        f"font-size:14px;line-height:20px\">{label}</td>"
        f"<td style=\"padding:3px 0;font-size:14px;line-height:20px;"
        f"font-weight:600;color:{accent or _INK}\" align=\"right\">{value}</td></tr>"
        for label, value in pairs
    )


def _section(title: str, body: str) -> str:
    return (
        f"<h3 style=\"margin:26px 0 8px;font-size:13px;letter-spacing:.06em;"
        f"text-transform:uppercase;color:{_MUTED};font-weight:600\">{title}</h3>"
        f"{body}"
    )


def _table(body: str) -> str:
    return (
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
        f"border=\"0\" style=\"border-collapse:collapse;width:100%;max-width:420px\">"
        f"{body}</table>"
    )


def _html_drafts(digest: Digest) -> str:
    if not digest.drafts_waiting:
        return ""
    items = "".join(
        f"<li style=\"margin:0 0 6px\">"
        f"<strong style=\"font-weight:600\">{_esc(d.company) or 'Unknown company'}</strong>"
        f" — {_esc(d.subject) or '(no subject)'} "
        f"<span style=\"color:{_MUTED}\">({_esc(d.kind)}, waiting {d.waiting_days}d)</span>"
        f"</li>"
        for d in digest.drafts
    )
    remaining = digest.drafts_waiting - len(digest.drafts)
    if remaining > 0:
        items += f"<li style=\"margin:0;color:{_MUTED}\">…and {remaining} more</li>"
    return _section(
        f"Waiting for you ({digest.drafts_waiting})",
        f"<ul style=\"margin:0;padding-left:18px;font-size:14px;line-height:21px\">"
        f"{items}</ul>",
    )


def _html_pipeline(digest: Digest) -> str:
    """The stock, not the flow — and the stall callout that earns its place."""
    health = digest.pipeline
    if not health.total:
        return ""

    pairs: list[tuple[str, object]] = [
        ("Waiting on a reply", health.awaiting_reply),
        ("In conversation", health.in_conversation),
        ("Interviewing", health.interviewing),
    ]
    if health.offers:
        pairs.append(("Offers", health.offers))
    pairs.append(("Closed", health.closed))

    body = _table(_stat_rows(tuple(pairs)))
    if health.stalled:
        # A left border rather than a filled panel: a background colour is the
        # first thing a dark-mode client inverts, and a rule survives it. The
        # word "quiet" carries the meaning; the colour only emphasises it.
        body += (
            f"<p style=\"margin:12px 0 0;padding:8px 0 8px 12px;"
            f"border-left:3px solid {_ALERT};font-size:14px;line-height:20px;"
            f"color:{_INK}\">"
            f"<strong style=\"font-weight:600;color:{_ALERT}\">"
            f"{health.stalled} gone quiet.</strong> "
            f"No reply in {STALL_DAYS} days and no follow-up scheduled — "
            f"these are waiting on nobody.</p>"
        )
    return _section("Your pipeline", body)


def _html_opportunities(digest: Digest) -> str:
    if not digest.opportunities:
        return ""
    items = "".join(
        f"<li style=\"margin:0 0 6px\">"
        f"<strong style=\"font-weight:600\">{_esc(job.title) or 'Untitled role'}</strong>"
        f" — {_esc(job.company) or 'Unknown company'}<br>"
        f"<span style=\"color:{_MUTED}\">"
        + " · ".join(filter(None, (_esc(job.where), _fit_label(job.fit_score))))
        + "</span></li>"
        for job in digest.opportunities
    )
    more = digest.jobs_found - len(digest.opportunities)
    if more > 0:
        items += (
            f"<li style=\"margin:0;color:{_MUTED}\">…and {more} more in the app</li>"
        )
    return _section(
        "New roles worth a look",
        f"<ul style=\"margin:0;padding-left:18px;font-size:14px;line-height:21px\">"
        f"{items}</ul>",
    )


def render_html(digest: Digest, *, app_url: str, opt_out_url: str) -> str:
    """The HTML part, built from exactly the content :func:`render_text` renders.

    The two parts of a multipart message must say the same thing: a client
    showing the text alternative — and a screen reader preferring it — is not
    entitled to a smaller digest. Every block here has a counterpart in
    :func:`render_text`, and ``tests/test_digest.py`` asserts the pairing rather
    than trusting it.
    """
    week = _section(
        "Last 7 days",
        _table(
            _stat_rows(
                (
                    ("Emails sent", digest.sent),
                    ("Follow-ups sent", digest.follow_ups_sent),
                    ("Replies", digest.replies),
                    ("Interviews", digest.interviews),
                    ("New roles found", digest.jobs_found),
                    ("Reply rate", f"{digest.response_rate:.0%}"),
                )
            )
        ),
    )

    queued = ""
    if digest.queued_emails or digest.follow_ups_due:
        queued = (
            f"<p style=\"color:{_MUTED};margin:20px 0 0;font-size:14px;"
            f"line-height:20px\">Queued for the week ahead: "
            f"{digest.queued_emails} emails ready to send, "
            f"{digest.follow_ups_due} follow-ups due.</p>"
        )

    return (
        f"<div style=\"font-family:{_FONT};max-width:560px;color:{_INK};"
        f"font-size:15px;line-height:22px\">"
        f"<p style=\"font-size:17px;line-height:25px;margin:0 0 4px;"
        f"font-weight:600\">{_esc(digest.headline)}</p>"
        f"{_html_drafts(digest)}"
        f"{_html_pipeline(digest)}"
        f"{_html_opportunities(digest)}"
        f"{week}"
        f"{queued}"
        f"<p style=\"margin:26px 0 0\"><a href=\"{_esc(app_url)}\" "
        f"style=\"color:#1d4ed8;font-weight:600;text-decoration:none\">"
        f"Open {_esc(settings.app_name)} &rarr;</a></p>"
        f"<hr style=\"border:0;border-top:1px solid {_RULE};margin:26px 0 12px\">"
        f"<p style=\"margin:0;font-size:12px;line-height:18px;color:{_MUTED}\">"
        f"<a href=\"{_esc(opt_out_url)}\" style=\"color:{_MUTED}\">"
        "Stop these weekly emails</a></p>"
        "</div>"
    )


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _record_attempt(
    db: Session, pref: DigestPreference, now: datetime, error: str
) -> None:
    """Book a try that produced no mail, with the reason it produced none.

    Committed on its own so the marker survives whatever the caller does next —
    the beat task rolls the session back when a user raises, and a marker lost
    to that rollback puts the same broken mailbox straight back in the next
    tick's work, which is the loop this exists to break.
    """
    pref.last_attempt_at = now
    pref.last_error = error[:500] or None
    db.commit()


def _claim(db: Session, pref: DigestPreference, now: datetime) -> bool:
    """Take this tick's digest for *pref*, or report that another run has it.

    :func:`is_due` is a decision about two columns — ``last_sent_at`` and
    ``last_attempt_at`` — read off the row *this session loaded*, and neither
    column moves until after the Gmail call at the far end of :func:`send`. A
    second run that loaded the row before the winner committed therefore holds
    a stale pair for as long as it keeps the object, because nothing refreshes
    it. It passed the same guard on the same stale values and mailed the user
    the same digest again.

    So the guard is finished here, by the database: a conditional ``UPDATE``
    that matches only while the row still holds the values the decision was
    made on. Matching *is* the claim, and stamping ``last_attempt_at`` is what
    makes the next caller's compare fail. A ``rowcount`` no stale attribute can
    fool replaces a comparison of two Python objects that were read minutes and
    one network call ago.

    Comparing the columns rather than re-deriving the schedule in SQL is
    deliberate. :func:`is_due` is a real predicate — a weekly slot, a minimum
    gap, a first-run case — and a second spelling of it in SQL would be a
    second thing to keep true. "Nothing moved under me" is the only property
    the claim actually needs, and it is one that cannot drift from the guard it
    protects.

    Committed rather than left open, for two reasons. The row lock would
    otherwise be held across ``build`` and a Gmail round-trip, which is a
    network call's worth of a lock on a row the user's own requests read. And
    the stamp has to be durable to be worth anything: the loser is not blocked
    on the winner here, it is refused by a value the winner has already
    written.

    Stamping the attempt *before* the work rather than after is what
    :func:`is_due` already asks for. A run that claims and then dies — worker
    killed, deploy mid-send — has spent the user's digest for the next
    :data:`MIN_GAP` rather than for the week: ``last_sent_at`` still predates
    the slot, so the digest goes out late, which this module has said all along
    is much the smaller failure.

    Flushed first because the sessions here are built ``autoflush=False``, so a
    caller holding an unflushed write to this row would otherwise have the
    claim compare against a value the database has not been told about yet.
    """
    seen_sent = pref.last_sent_at
    seen_attempt = pref.last_attempt_at
    db.flush()
    claimed = db.execute(
        update(DigestPreference)
        .where(
            DigestPreference.id == pref.id,
            DigestPreference.last_sent_at.is_not_distinct_from(seen_sent),
            DigestPreference.last_attempt_at.is_not_distinct_from(seen_attempt),
        )
        .values(last_attempt_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    # The commit also expires the session's copy of the row. A winner therefore
    # reads ``sent_count`` back off the row its own claim just matched before
    # incrementing it, rather than off whatever this session loaded earlier —
    # the loser never reaches that increment, because it returns on the False.
    db.commit()
    return claimed == 1


def send(
    db: Session,
    user: User,
    *,
    now: datetime | None = None,
    force: bool = False,
    skip_quiet: bool = True,
) -> dict:
    """Send *user*'s digest if it is due. Returns what happened, never raises.

    *force* bypasses the schedule (the "send me one now" button) but not the
    opt-out and not the mailbox pause — a user who unsubscribed does not get
    mail because they pressed a button in an app they may not have opened.

    Every outcome is a ``status`` string rather than an exception because the
    caller is a beat task sweeping every user: one unreachable mailbox must not
    stop the other ninety-nine digests.

    ``claimed_elsewhere`` means another run took this row between the schedule
    check and here; see :func:`_claim`. It is a status of its own rather than a
    quiet ``not_due`` so that overlapping sweeps show up in the task result
    instead of hiding inside the bucket every un-due user is already counted in.
    """
    now = now or datetime.now(UTC)
    pref = get_or_create(db, user)

    if not pref.enabled:
        return {"status": "unsubscribed", "user_id": user.id}
    if not force and not is_due(pref, now=now):
        return {"status": "not_due", "user_id": user.id}
    # The claim covers the forced path too. A button press bypasses the
    # *schedule*, which is a rule about time; it is not a reason to let two
    # concurrent presses each hand a message to Gmail.
    if not _claim(db, pref, now):
        return {"status": "claimed_elsewhere", "user_id": user.id}

    account = user.primary_gmail
    if account is None:
        # Recorded as an attempt: an account with no mailbox stays that way
        # until the user connects one, and re-deciding that hourly costs a query
        # per user per tick to reach the same answer.
        _record_attempt(db, pref, now, "No Gmail account is connected")
        return {"status": "no_mailbox", "user_id": user.id}

    paused_until = _aware(account.paused_until)
    if paused_until is not None and paused_until > now:
        # The mailbox is held for a bounce or complaint problem. Sending
        # anything at all from it, even to its owner, is the wrong move while
        # that is being worked out.
        _record_attempt(db, pref, now, "The mailbox is paused to protect its reputation")
        return {"status": "mailbox_paused", "user_id": user.id}

    digest = build(db, user, now=now)
    if skip_quiet and digest.is_quiet:
        # Nothing happened and nothing is waiting. Marked as sent so the next
        # sweep does not retry it hourly for the rest of the day.
        pref.last_sent_at = now
        pref.last_attempt_at = now
        pref.last_error = None
        db.commit()
        return {"status": "skipped_quiet", "user_id": user.id}

    opt_out = unsubscribe_url(pref)
    app_url = settings.frontend_url.rstrip("/")
    body_text = render_text(digest, app_url=app_url, opt_out_url=opt_out)

    try:
        gmail_service.send_email(
            account=account,
            to=user.email,
            subject=subject_line(digest),
            body_text=body_text,
            # No CAN-SPAM footer: this is not unsolicited commercial mail to a
            # stranger, it is the product reporting to its own user. The
            # List-Unsubscribe headers still go on, so Gmail shows its native
            # unsubscribe control — which is a better opt-out than a footer.
            #
            # The HTTPS half only (`mailto_unsubscribe` defaults off). This
            # message goes from the user's mailbox to the user's own address, so
            # the `mailto:` half asked them to unsubscribe by mailing
            # themselves — a request nothing reads, and one that would have
            # opted out `Recruiter` rows rather than this digest even if
            # something did. `unsubscribe_token` is this message's opt-out and
            # the URL above is where it lives.
            unsubscribe_url=opt_out,
            footer="",
            body_html=render_html(digest, app_url=app_url, opt_out_url=opt_out),
        )
    except (
        gmail_service.GmailAuthRevoked,
        gmail_service.GmailScopeInsufficient,
    ) as exc:
        # A dead grant, and the one send path that never said so. Every other
        # caller of the Gmail API — the send task, the inbox scanner, the reply
        # tasks, the backlog sweep — marks the mailbox here; this one caught the
        # exception in the generic clause below, wrote its text into
        # ``last_error``, and returned. So the row went on claiming
        # ``connected``, the account stayed in ``live_accounts`` for every other
        # piece of background work to choose, and the digest re-discovered the
        # same dead grant every day for as long as the user left it — while
        # nothing in the UI ever told them the one thing that would fix it.
        #
        # Marked *and* recorded: the mailbox needs reconnecting, and this
        # week's digest still owes the user a delivery.
        logger.warning("digest send failed for user %s: %s", user.id, exc)
        gmail_accounts.mark_revoked(db, account, str(exc))
        db.commit()
        _record_attempt(db, pref, now, str(exc))
        return {"status": "gmail_revoked", "user_id": user.id, "error": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 - one bad mailbox must not stall the sweep
        logger.warning(
            "digest send failed for user %s: %s", user.id, exc, exc_info=True
        )
        # ``last_sent_at`` deliberately stays put — this week's digest still owes
        # the user a delivery. ``last_attempt_at`` is what keeps the next try a
        # day away rather than an hour; see :func:`is_due`.
        #
        # The recorded reason is the classifier's sentence rather than
        # ``str(exc)`` whenever the classifier recognises the failure, because
        # ``last_error`` is shown to the user: a raw ``HttpError`` puts a
        # request URL and a JSON fragment in front of somebody who wanted to
        # know why their weekly summary did not arrive.
        reason = gmail_service.classify_transport_failure(exc).message
        if not isinstance(exc, (gmail_service.HttpError, *gmail_service._NETWORK_ERRORS)):
            reason = str(exc)
        _record_attempt(db, pref, now, reason)
        return {"status": "failed", "user_id": user.id, "error": reason[:200]}

    # The digest costs the user a send, so it is booked against the mailbox like
    # any other. Under-counting here would let the warm-up ramp believe it has
    # more headroom than Gmail does.
    reputation_service.record_send(account, now=now)
    pref.last_sent_at = now
    pref.last_attempt_at = now
    pref.last_error = None
    pref.sent_count = (pref.sent_count or 0) + 1
    db.commit()
    return {"status": "sent", "user_id": user.id, "to": user.email}


__all__ = [
    "LIST_LIMIT",
    "PERIOD_DAYS",
    "Digest",
    "DraftLine",
    "build",
    "get_or_create",
    "is_due",
    "render_html",
    "render_text",
    "send",
    "subject_line",
    "unsubscribe_url",
]
