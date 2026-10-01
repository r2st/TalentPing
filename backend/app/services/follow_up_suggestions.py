"""Applications that went quiet with nobody scheduled to chase them.

The product has always had a follow-up *sequence*: a campaign says "three steps,
four days apart", :mod:`app.services.follow_up_service` writes them at send time,
and a beat task drains them. That covers the applications the sequence covered.

It does not cover the ones it never reached, and there are more of those than
anybody expected:

* a campaign created with ``follow_up_enabled`` off — nothing was ever scheduled;
* a sequence that *ran out*, three steps sent, still no reply, and the sequence
  has no opinion about what happens on day 30;
* an application whose sequence was cancelled by something that later turned out
  not to be a reply — a bounce that resolved, an out-of-office;
* outreach filed by hand, or by a path that never called the scheduler.

Every one of those is an application that went out, heard nothing, and has
nobody — no human and no task — planning to do anything about it. It appears in
no queue. The digest now counts them (``digest_service.PipelineHealth.stalled``);
this module is what turns the count into something the user can act on, one row
at a time, with the reason attached.

**A suggestion is not a scheduled send.** Nothing here writes a follow-up, and
nothing here touches a mailbox. It is a read that produces a list and an
explanation, and acting on one is a separate, explicit call
(:func:`accept`) that goes through the same
:func:`~app.services.follow_up_service.schedule_for_application` machinery every
other follow-up uses. The distinction matters because the whole population this
module surfaces is *outreach the automatic path already decided not to chase* —
silently resuming it on the user's behalf would be the product overruling its own
guardrails from a screen labelled "suggestions".

The refusals are the interesting part, and each is a case where sending would be
worse than staying quiet:

* **Somebody replied.** Reuses ``follow_up_service.thread_has_reply``, so an
  out-of-office still counts as silence here exactly as it does there, and a
  "who is this?" still counts as a reply.
* **A follow-up is already scheduled.** The agent is going to chase it, and a
  suggestion would be asking the user to duplicate their own agent's work.
* **The conversation is finished.** A rejection, an opt-out, a closed row.
* **The address does not work, or the person asked us to stop.** A hard bounce
  or an opt-out is not a candidate for a nudge at any interval.
* **The user set the contact aside.** ``Recruiter.excluded_at``, the third of
  the three axes that decide whether the agent writes to somebody, and the one
  that is the user's own decision rather than the recipient's or the address's.
  Suggesting a contact somebody has explicitly excluded is the product arguing
  with them.
* **Nothing was ever sent.** A queued application has said nothing yet, so there
  is nothing for anyone to be silent about.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.application import CLOSED_STATUSES, Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.follow_up import FollowUp, FollowUpStatus
from app.models.recruiter import DeliveryState, Recruiter
from app.models.status_event import StatusEventSource
from app.models.user import User
from app.services import follow_up_service, pipeline_board, send_time

logger = logging.getLogger(__name__)

# The shortest silence worth a suggestion. Below this the honest advice is
# "wait": a recruiter who has not answered in four days is a recruiter who had
# four days, and a product that says "chase them" on day five is teaching the
# user a habit that costs them the recruiters they most want.
#
# Deliberately lower than ``digest_service.STALL_DAYS`` (14). The digest is a
# weekly interruption and has to clear a higher bar to be worth one; this is a
# list the user opened on purpose, and a seven-day nudge they can act on or
# ignore is the right density for a screen they asked to see.
MIN_SILENT_DAYS = 7

# Where a silence stops being ordinary and starts being the end of the thread.
# Past this the suggestion changes its wording rather than disappearing: the
# useful advice at six weeks is a last note, not a fourth nudge.
COLD_DAYS = 21
LAST_CALL_DAYS = 42

# The most suggestions one read returns. A list this long is already a list
# nobody finishes; past it the honest thing is a count, not more rows.
LIMIT = 25

# What a caller passes for ``limit`` when it wants the whole population rather
# than a page: :func:`count`, and the hourly notification sweep, which states
# the number out loud. High enough that nobody reaches it, finite so a runaway
# read is still bounded.
#
# Worth a name rather than a literal at each call site, because the two callers
# that need it are the two whose whole output is that number — and one of them
# was quietly using :data:`LIMIT` instead, so a pipeline of forty stalled
# applications was announced as twenty-five.
UNCAPPED = 10_000

#: Urgency bands, ordered. The client styles from these and
#: ``tests/test_enum_drift.py`` checks the lookup table against them.
URGENCIES: tuple[str, ...] = ("due", "cold", "last_call")

# A conversation that is moving rather than stalled: a time is being agreed, an
# interview is booked, an offer is on the table. Not silence at any age, and not
# a thing to chase — see the note in :func:`build`.
#
# Derived from ``follow_up_service.TERMINAL_STATUSES`` rather than restated,
# because a hand-written copy is exactly how these three ended up being honoured
# by :func:`build` and by nothing else in this module. That set is what the
# drain path checks before it sends a step, so subtracting the closed half
# leaves precisely the statuses that stop a follow-up without ending the
# conversation — and any status added to it later lands here by construction.
LIVE_STATUSES: frozenset[ApplicationStatus] = (
    follow_up_service.TERMINAL_STATUSES - CLOSED_STATUSES
)

#: The same set in a stable order, for the ``NOT IN`` clause in :func:`build`.
_LIVE_IN_ORDER: tuple[ApplicationStatus, ...] = tuple(
    sorted(LIVE_STATUSES, key=lambda status: status.value)
)


def _moving(application: Application) -> str:
    """Why a live conversation is refused, in the status's own word."""
    return (
        "that conversation is already moving "
        f"({application.status.value.lower().replace('_', ' ')})"
    )


@dataclass
class Suggestion:
    """One application worth chasing, with everything the row needs to say so.

    Carries the *reason* rather than only the numbers, because a suggestion the
    user cannot second-guess is a suggestion they either obey blindly or ignore
    entirely, and both are worse than one they can judge.
    """

    application_id: int
    company: str | None
    recruiter_name: str | None
    recruiter_email: str
    subject: str | None
    last_sent_at: datetime
    silent_days: int
    touches: int
    urgency: str
    reason: str
    # Whether a sequence can actually be written for this one. False when the
    # application's campaign has follow-ups switched off — the suggestion is
    # still worth showing (the user can send by hand, or switch the campaign
    # setting) but the one-click action would silently do nothing.
    can_schedule: bool = True

    @property
    def is_last_call(self) -> bool:
        return self.urgency == "last_call"


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips naive datetimes; the arithmetic here needs tz-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _urgency(silent_days: int) -> str:
    if silent_days >= LAST_CALL_DAYS:
        return "last_call"
    if silent_days >= COLD_DAYS:
        return "cold"
    return "due"


def _reason(silent_days: int, touches: int, urgency: str) -> str:
    """Why this row is here, in the words the UI shows next to it.

    Written from the two facts the user cannot see on the row itself — how long
    it has been and how many times we have already written — because "follow up"
    with neither is advice that reads the same on day 8 and day 80.
    """
    ago = f"{silent_days} days"
    contacted = (
        "one email" if touches == 1 else f"{touches} emails"
    )
    if urgency == "last_call":
        return (
            f"Silent for {ago} after {contacted}. Worth one last note that "
            "closes the loop rather than another nudge."
        )
    if urgency == "cold":
        return (
            f"Silent for {ago} after {contacted}. Cold enough that a fresh "
            "angle will do more than a reminder."
        )
    return f"Silent for {ago} after {contacted} and nothing is scheduled."


def _last_outbound(db: Session, application_ids: list[int]) -> dict[int, datetime]:
    """The most recent *delivered* message per application.

    ``EmailStatus.SENT`` only, for the reason
    :func:`~app.services.follow_up_service._has_sent_outreach` gives: a draft is
    waiting on the user, a queued row is waiting on the broker, and a failed one
    never arrived. None of the three is a message a recruiter could have
    answered, and counting one as the start of a silence would date the silence
    from a message that does not exist on their side.
    """
    if not application_ids:
        return {}
    rows = db.execute(
        select(EmailThread.application_id, func.max(Email.sent_at))
        .join(Email, Email.thread_id == EmailThread.id)
        .where(
            EmailThread.application_id.in_(application_ids),
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.SENT,
        )
        .group_by(EmailThread.application_id)
    ).all()
    return {app_id: sent for app_id, sent in rows if sent is not None}


def _touch_counts(db: Session, application_ids: list[int]) -> dict[int, int]:
    if not application_ids:
        return {}
    rows = db.execute(
        select(EmailThread.application_id, func.count(Email.id))
        .join(Email, Email.thread_id == EmailThread.id)
        .where(
            EmailThread.application_id.in_(application_ids),
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.SENT,
        )
        .group_by(EmailThread.application_id)
    ).all()
    return dict(rows)


def _with_any_inbound(db: Session, application_ids: list[int]) -> set[int]:
    """Applications that have received *anything*, of any intent.

    A pre-filter, not a decision. The decision is
    :func:`~app.services.follow_up_service.thread_has_reply`, which has to be
    asked one application at a time because it turns on the intent of each
    inbound message — an out-of-office is not a reply, an unclassified message
    is. That per-row question is fine for a request and wrong for the hourly
    notification sweep, which runs this for every active user: a candidate with
    four hundred open applications was four hundred round trips a tick, forever,
    to answer "no" for nearly all of them.

    Nearly all, because an application that never received anything cannot have
    a reply by any definition — so one query removes the whole common case and
    the careful per-row check runs only on the handful that actually got mail.
    The semantics are identical; only the cost moves.
    """
    if not application_ids:
        return set()
    return set(
        db.scalars(
            select(EmailThread.application_id)
            .join(Email, Email.thread_id == EmailThread.id)
            .where(
                EmailThread.application_id.in_(application_ids),
                Email.direction == EmailDirection.RECEIVED,
            )
            .distinct()
        )
    )


def _subjects(db: Session, application_ids: list[int]) -> dict[int, str | None]:
    """The first thread's subject per application, so a row names its conversation.

    Batched like every other read in this module, and it is the one that was
    not. It ran inside :func:`build`'s loop — one round trip per qualifying
    application — which is the precise cost :func:`_with_any_inbound` exists to
    remove, one function further down and in the same two callers:

    * the hourly notification sweep, which builds this list for every active
      user on every tick and reads nothing off it but the oldest row's company
      and silent-day count. A user with four hundred stalled applications paid
      four hundred queries an hour for subjects nobody looked at.
    * ``GET /follow-ups/suggestions``, which pays it twice on any full page —
      once for the page and once inside :func:`count`, whose whole output is a
      length.

    The subjects are also fetched *before* the sort and the truncation, so the
    cost was never bounded by ``limit`` either.

    "First thread by id" is what the per-application query said, kept exactly:
    the rows arrive ordered and ``setdefault`` takes the lowest id per
    application.
    """
    if not application_ids:
        return {}
    rows = db.execute(
        select(EmailThread.application_id, EmailThread.subject)
        .where(EmailThread.application_id.in_(application_ids))
        .order_by(EmailThread.application_id, EmailThread.id)
    ).all()
    subjects: dict[int, str | None] = {}
    for app_id, subject in rows:
        subjects.setdefault(app_id, subject)
    return subjects


def _scheduled(db: Session, application_ids: list[int]) -> set[int]:
    if not application_ids:
        return set()
    return set(
        db.scalars(
            select(FollowUp.application_id).where(
                FollowUp.application_id.in_(application_ids),
                FollowUp.status == FollowUpStatus.SCHEDULED,
            )
        )
    )


def build(
    db: Session, user: User, *, now: datetime | None = None, limit: int = LIMIT
) -> list[Suggestion]:
    """Applications worth chasing, longest silence first. Reads only.

    Ordered oldest-silence-first rather than newest, which is the opposite of
    every other list in this product and is right here: a queue truncated at
    :data:`LIMIT` should keep the rows nobody is ever going to get to otherwise.
    The freshly-due ones will still be here next week; the six-week-old one is
    the one about to become unreachable.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=MIN_SILENT_DAYS)

    # One query for the candidate set, filtering everything SQL can decide, then
    # per-application work only for what survives. The alternative — walking
    # every application and asking five questions each — is the shape that turns
    # a page load into a thousand round trips once someone has a real pipeline.
    rows = db.execute(
        select(Application, Recruiter, Campaign)
        .join(Recruiter, Application.recruiter_id == Recruiter.id)
        .join(Campaign, Application.campaign_id == Campaign.id)
        .where(
            Application.user_id == user.id,
            Application.status.not_in(list(CLOSED_STATUSES)),
            # An offer or a scheduled interview is a live conversation, not a
            # silence — whatever the last *email* timestamp says. Deals move to
            # phone calls, and a product that nudges a recruiter mid-offer is
            # actively embarrassing its user.
            Application.status.not_in(_LIVE_IN_ORDER),
            Recruiter.opted_out.is_(False),
            Recruiter.delivery_state != DeliveryState.HARD_BOUNCED,
            # The third axis. `opted_out` is the recipient's decision and
            # `delivery_state` is the address's; this one is the *user's own* —
            # "stop emailing this person", from `POST /recruiters/{id}/exclude`.
            # Checking two of the three put a contact the user had explicitly
            # set aside back on a screen headed with the advice to chase them.
            Recruiter.excluded_at.is_(None),
        )
    ).all()

    by_id = {app.id: (app, recruiter, campaign) for app, recruiter, campaign in rows}
    ids = list(by_id)
    last_sent = _last_outbound(db, ids)
    touches = _touch_counts(db, ids)
    scheduled = _scheduled(db, ids)
    heard_from = _with_any_inbound(db, ids)
    subjects = _subjects(db, ids)

    suggestions: list[Suggestion] = []
    for app_id, (_application, recruiter, campaign) in by_id.items():
        if app_id in scheduled:
            continue
        sent_at = _aware(last_sent.get(app_id))
        if sent_at is None or sent_at > cutoff:
            continue
        # The one check that cannot be batched, and the one whose subtleties
        # matter most (out-of-office is not a reply, an unclassified message
        # is). Reused rather than reimplemented so the two cannot drift — and
        # asked only of applications that actually received something, which is
        # what keeps the hourly sweep from being one round trip per open
        # application per user. See :func:`_with_any_inbound`.
        if app_id in heard_from and follow_up_service.thread_has_reply(db, app_id):
            continue

        silent_days = max(0, (now - sent_at).days)
        urgency = _urgency(silent_days)
        count = touches.get(app_id, 1)
        suggestions.append(
            Suggestion(
                application_id=app_id,
                company=recruiter.company,
                recruiter_name=recruiter.name,
                recruiter_email=recruiter.email,
                subject=subjects.get(app_id),
                last_sent_at=sent_at,
                silent_days=silent_days,
                touches=count,
                urgency=urgency,
                reason=_reason(silent_days, count, urgency),
                can_schedule=bool(
                    campaign.follow_up_enabled and campaign.follow_up_count >= 1
                ),
            )
        )

    suggestions.sort(key=lambda s: (-s.silent_days, s.application_id))
    return suggestions[:limit]


def count(db: Session, user: User, *, now: datetime | None = None) -> int:
    """How many suggestions there are, uncapped by :data:`LIMIT`.

    A separate call rather than ``len(build(...))`` so a badge does not silently
    stop counting at 25 — which is exactly the point at which the number starts
    mattering.
    """
    return len(build(db, user, now=now, limit=UNCAPPED))


def _claim(application_id: int, user_id: int):
    """The application row, locked for the duration of the transaction.

    Without this, "is a follow-up already scheduled?" is a read followed by an
    unguarded insert, and ``follow_ups`` has no unique constraint that would
    catch the loser: two accepts arriving together — a double-click, a retried
    request, the same row open in two tabs — both find nothing scheduled and
    both write the whole sequence. The recruiter then gets each step twice, and
    nothing downstream can tell the copies apart, because they are two
    legitimate rows rather than one row claimed twice.

    A plain ``FOR UPDATE`` rather than ``skip_locked``. The loser here is a
    person waiting on a button, not a worker with other work to do: it should
    block briefly and then be told "already scheduled", which is the true
    answer. ``skip_locked`` would tell them the application does not exist.

    SQLite ignores row locking entirely, which is why
    ``tests/test_follow_up_suggestions.py`` asserts the compiled statement as
    well as the behaviour — the same argument ``test_concurrent_ingestion``
    makes for its claims.
    """
    return (
        select(Application)
        .where(Application.id == application_id, Application.user_id == user_id)
        .with_for_update()
    )


def accept(
    db: Session, user: User, application_id: int, *, now: datetime | None = None
) -> tuple[list[FollowUp], str | None]:
    """Schedule the campaign's sequence for one suggested application.

    Returns ``(follow_ups, refusal)``. A refusal is a sentence, never an
    exception, because every reason this can decline is a race the user lost
    rather than a bug: they clicked on a row that a background task, an inbound
    reply, or their own second tab had already resolved.

    **Re-checks every condition** rather than trusting the list the row came
    from. The list is a read, the read is a page that stays open, and the gap
    between "we showed this" and "they clicked it" is where the recruiter's
    reply lands. Scheduling a chase on top of a reply that arrived four minutes
    ago is precisely the failure this whole feature is supposed to prevent.

    The sequence is dated from *now* rather than from the original send. The
    campaign's offsets are "days after outreach", and applying them to a message
    from six weeks ago would schedule every step in the past and fire the whole
    sequence at the next tick — three emails to one recruiter in one minute.
    """
    now = now or datetime.now(UTC)
    application = db.scalar(_claim(application_id, user.id))
    if application is None:
        return [], "that application no longer exists"
    if application.status in CLOSED_STATUSES:
        return [], "that conversation is already closed"
    # The list already refuses these; accepting used to not, and the two
    # together are the whole race this function exists for. A card dragged to
    # OFFER while the page was open leaves no inbound row behind it — the offer
    # came over the phone, which is the ordinary way an offer arrives — so
    # `thread_has_reply` below answers no and every other check passes. The
    # sequence was written, the user was told "3 scheduled", and each step was
    # then silently cancelled by ``follow_up_service.TERMINAL_STATUSES`` on its
    # way out. Refusing here says the true thing instead of scheduling work that
    # cannot run.
    if application.status in LIVE_STATUSES:
        return [], _moving(application)
    if follow_up_service.thread_has_reply(db, application_id):
        return [], "they have replied since this was suggested"

    already = db.scalar(
        select(FollowUp.id).where(
            FollowUp.application_id == application_id,
            FollowUp.status == FollowUpStatus.SCHEDULED,
        )
    )
    if already is not None:
        return [], "a follow-up is already scheduled for this one"

    recruiter = db.get(Recruiter, application.recruiter_id)
    if recruiter is None:
        return [], "that contact no longer exists"
    if recruiter.opted_out:
        return [], f"{recruiter.email} has asked not to be contacted"
    if recruiter.delivery_state == DeliveryState.HARD_BOUNCED:
        return [], f"mail to {recruiter.email} does not arrive"
    if recruiter.is_excluded:
        return [], f"you set {recruiter.email} aside"

    campaign = db.get(Campaign, application.campaign_id)
    if campaign is None:
        return [], "that campaign no longer exists"
    if not campaign.follow_up_enabled or campaign.follow_up_count < 1:
        return [], "follow-ups are switched off for this campaign"

    # `schedule_for_application` refuses when *any* follow-up row exists, not
    # only a scheduled one — which is correct for its own caller (a re-sent
    # outreach must not double a sequence) and wrong here: the whole population
    # this module surfaces includes applications whose sequence already ran to
    # completion. Those rows are SENT, not SCHEDULED, and the user asking for
    # one more note is not a retry.
    exhausted = db.scalars(
        select(FollowUp).where(FollowUp.application_id == application_id)
    ).all()
    if exhausted:
        follow_ups = _extend(db, application, campaign, exhausted, now)
    else:
        follow_ups = follow_up_service.schedule_for_application(
            db, application, campaign, sent_at=now
        )

    if not follow_ups:
        return [], "there was nothing left to schedule"

    db.commit()
    logger.info(
        "scheduled %d follow-up(s) for application %s from a suggestion",
        len(follow_ups),
        application_id,
    )
    return follow_ups, None


def _extend(
    db: Session,
    application: Application,
    campaign: Campaign,
    existing: list[FollowUp],
    now: datetime,
) -> list[FollowUp]:
    """One more step on a sequence that already finished.

    A single step rather than a fresh sequence. The candidate has already been
    written to three or four times; re-running the whole cadence at them is how
    a follow-up feature turns its user into the thing recruiters filter.

    Numbered past the highest existing step so the thread's history stays
    readable, and templated ``FINAL`` because by definition it is: anything
    after an exhausted sequence is the last note, and the composer's brief for
    ``FINAL`` is the one that closes the loop gracefully instead of asking again.
    """
    from app.models.follow_up import FollowUpTemplate

    recruiter = db.get(Recruiter, application.recruiter_id)
    tz = send_time.resolve_timezone(db, recruiter, application=application)
    step = max(f.step for f in existing) + 1
    # The campaign's own first gap, so "one more note" lands on the cadence the
    # user configured rather than on a constant invented here.
    gap = follow_up_service.default_offsets(campaign.follow_up_interval_days, 1)[0]
    due = follow_up_service.optimal_send_time(now + timedelta(days=gap), tz)
    row = FollowUp(
        application_id=application.id,
        step=step,
        scheduled_at=due,
        status=FollowUpStatus.SCHEDULED,
        template=FollowUpTemplate.FINAL,
        note="added from a follow-up suggestion after the sequence ran out",
    )
    db.add(row)
    db.flush()
    return [row]


def dismiss(
    db: Session, user: User, application_id: int, *, note: str | None = None
) -> str | None:
    """Stop suggesting this one, by closing the conversation it belongs to.

    Returns a refusal sentence, or ``None`` on success.

    **Re-checks the conversation first**, for the same reason :func:`accept`
    does and with more at stake — see the note below.

    Implemented as a status change rather than as a "dismissed" flag on a row
    that does not exist. A suggestion is derived, not stored — there is nothing
    to mark — and the user's actual meaning when they wave one away is "this
    one is over", which the pipeline already has a word for. Recording it as
    ``NO_RESPONSE`` also keeps the response-rate arithmetic honest: an
    application the candidate gave up on is a non-response, and quietly hiding
    it from this list while leaving it "awaiting reply" would inflate the number
    of things the user believes are still live.
    """
    application = db.scalar(_claim(application_id, user.id))
    if application is None:
        return "that application no longer exists"
    if application.status in CLOSED_STATUSES:
        return None  # already gone; nothing to do and nothing to complain about

    # Dismissing re-checks the same race :func:`accept` does, and it matters
    # more here: accept writes rows the drain path can still refuse, while this
    # overwrites the status outright and cancels whatever was scheduled. A row
    # rendered as "silent for 30 days" and clicked after the recruiter finally
    # wrote back — or after the user moved the card to OFFER themselves — filed
    # a live conversation as NO_RESPONSE, took an engaged application out of
    # every rate the product reports, and left the reply sitting in an inbox
    # thread whose application says nobody answered. That is the precise
    # arithmetic this function's docstring claims to protect.
    #
    # Both refusals name what changed, so the user can close it deliberately
    # from the tracker if it really is over. Neither costs them anything: the
    # row is already gone from the list on the next read.
    if application.status in LIVE_STATUSES:
        return f"{_moving(application)} — close it from the tracker if it is over"
    if follow_up_service.thread_has_reply(db, application_id):
        return "they have replied since this was suggested"

    # Through the board's recorder, not by assigning the column. The history
    # table is the product's answer to "how did this application get here?",
    # and its own docstring calls itself a complete account rather than a log of
    # the manual half — but this transition wrote no row, so the one ending a
    # user chose *by hand* was the one ending with no explanation behind it. A
    # card that fell into *Rejected* on its own, with the tracker's history
    # panel empty, reads as the product having given up rather than as the
    # candidate having closed it.
    #
    # ``MANUAL`` because it is: the user waved the suggestion away. That is the
    # distinction the ``source`` column exists to draw, and getting it wrong
    # here would file a candidate's decision as an inference of ours.
    pipeline_board.record_change(
        db,
        application,
        ApplicationStatus.NO_RESPONSE,
        source=StatusEventSource.MANUAL,
        reason=note or "closed from a follow-up suggestion",
    )
    cancelled = follow_up_service.cancel_for_application(
        db, application_id, note or "closed from a follow-up suggestion"
    )
    db.commit()
    logger.info(
        "closed application %s from a suggestion (%d follow-up(s) cancelled)",
        application_id,
        cancelled,
    )
    return None


__all__ = [
    "COLD_DAYS",
    "LAST_CALL_DAYS",
    "LIMIT",
    "LIVE_STATUSES",
    "MIN_SILENT_DAYS",
    "UNCAPPED",
    "URGENCIES",
    "Suggestion",
    "accept",
    "build",
    "count",
    "dismiss",
]
