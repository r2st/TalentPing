"""Follow-up scheduling and composition — the nudge after the first email.

Timing comes from research §2.3, made explicit: a nudge on **day 3** and a final
touch on **day 7**, from ``settings.follow_up_step_days`` or a campaign's own
``follow_up_step_days``. The old ``interval * step + (step - 1) * (interval // 2)``
formula could not express that pair for any integer interval — it produced day 4
and day 10 at the shipped defaults.

Each step is then moved to the recipient's local weekday business morning by
:mod:`app.services.send_time`, so "day 3" means 09:00-11:00 where the recruiter
actually is rather than 06:00 UTC (which is 22:00 the previous evening on the US
west coast — reliably the worst slot available).

Two responsibilities live here:

* :func:`schedule_for_application` — write the whole sequence up-front when the
  first outreach lands, one row per step.
* :func:`compose_follow_up` — pick the right angle at *send* time, based on what
  the recruiter has done since. Composing early would mean sending a
  "just checking in" to someone who replied yesterday.

Cancellation is a status change, never a delete: "we stopped on day 4 because
they replied" is history worth keeping.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.application import (
    ENGAGED_STATUSES,
    Application,
    ApplicationStatus,
)
from app.models.campaign import Campaign, CampaignStatus
from app.models.email import Email, EmailDirection, EmailStatus, ReplyIntent
from app.models.email_thread import EmailThread
from app.models.follow_up import FollowUp, FollowUpStatus, FollowUpTemplate
from app.models.recruiter import Recruiter
from app.models.status_event import StatusEventSource
from app.services import (
    bounce_service,
    conversation_stage,
    send_time,
    spam_risk,
    thread_headers,
    untrusted,
)
from app.services.ai_composer import CandidateContext, RecruiterContext
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    extract_json_object,
    llm_is_configured,
    looks_like_reasoning,
)

logger = logging.getLogger(__name__)

# Statuses that mean the conversation is over — never nudge these, whatever the
# campaign's stop-on-reply setting says.
#
# ``OFFER`` belongs here and was missing: it reached ``ApplicationStatus`` after
# this set was written. With stop-on-reply *off* — a legitimate campaign setting,
# for a cadence that should run through a holding reply — nothing else stopped
# the sequence, so a candidate with an offer in hand could be sent "I'll leave
# this here so I'm not cluttering your inbox". Stop-on-reply is a choice about
# chasing; an offer is not a thing to chase past.
#
# ``SCHEDULING`` is the same bug wearing a different status: it means a
# recruiter proposed or asked for a time and the agent already drafted the
# reply that answers them (see ``reply_agent``'s SCHEDULING template) — the
# conversation is not stalled, it is actively being coordinated. With
# stop-on-reply off, nothing else stopped the drip either, so a candidate
# mid-negotiation over an interview slot could still be sent a "just
# following up on my application" nudge on top of the scheduling reply
# already in flight. Chasing is not a thing to do to a conversation that is
# already moving.
TERMINAL_STATUSES = frozenset(
    {
        ApplicationStatus.NOT_INTERESTED,
        ApplicationStatus.UNSUBSCRIBED,
        ApplicationStatus.CLOSED,
        ApplicationStatus.SCHEDULING,
        ApplicationStatus.INTERVIEW_SCHEDULED,
        ApplicationStatus.OFFER,
    }
)

# Statuses that mean the recruiter engaged but the conversation is still open.
# With stop-on-reply on (the default), these cancel the remaining sequence.
#
# Derived rather than restated: "did the recruiter engage?" is a fact the model
# owns (``ENGAGED_STATUSES``), and keeping a hand-written copy of it here is
# exactly how ``OFFER`` ended up in neither set. Subtracting the terminal ones
# leaves the same three members it always had, and any status added to
# ``ENGAGED_STATUSES`` from now on lands in one of the two by construction
# rather than falling through both.
REPLIED_STATUSES = ENGAGED_STATUSES - TERMINAL_STATUSES

def parse_step_days(raw: str) -> list[int]:
    """Parse ``"3,7"`` into ``[3, 7]``, ignoring anything unparseable."""
    offsets: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0:
            offsets.append(value)
    return offsets or [3, 7]


def default_offsets(first_gap: int | None, count: int) -> list[int]:
    """*count* day offsets: the configured sequence, re-anchored and extended.

    ``settings.follow_up_step_days`` ("3,7") gives the *shape* — a first nudge,
    then a close four days later. This turns that shape into a sequence of the
    length and starting point the campaign actually asked for.

    Two knobs were being read by nothing at all, and both are live controls on
    the preferences form:

    **``follow_up_interval_days``** is the form's "First one after N days". The
    module that replaced the old widening formula dropped it entirely and never
    put anything in its place, so every campaign nudged on day 3 and day 7 no
    matter what the user picked — while the form, still computing the retired
    formula in JavaScript, told them "around day 4 and day 10". The whole
    sequence is shifted so its first step lands where they asked, keeping the
    gaps between later steps intact: at "3,7", picking 7 gives day 7 and day 11.

    **``follow_up_count``** could ask for up to five follow-ups, and the
    configured sequence has two entries; ``[:count]`` cannot make a list longer
    than it is, so 3, 4 and 5 all silently meant 2. Steps past the configured
    list continue at its final gap.

    A campaign whose first gap matches the configured first step — the shipped
    default, once the two agree — gets the documented day-3/day-7 cadence
    unchanged.
    """
    base = parse_step_days(settings.follow_up_step_days)
    shift = first_gap - base[0] if first_gap and first_gap > 0 else 0
    offsets = [day + shift for day in base]
    # The last gap in the configured shape, repeated to extend it. A one-entry
    # sequence has no gap to read, so it repeats its own offset — "every N days".
    gap = base[-1] - base[-2] if len(base) >= 2 else base[0]
    while len(offsets) < count:
        offsets.append(offsets[-1] + max(1, gap))
    return [day for day in offsets if day > 0][:count]


def sequence_offsets(campaign: Campaign) -> list[int]:
    """Day offsets from the initial send, one per follow-up step.

    The campaign's own explicit list wins; otherwise the sequence is generated
    from its interval and count — see :func:`default_offsets`. Sorted and
    deduplicated so two steps can't land on the same day, then capped by
    ``follow_up_count``.
    """
    count = max(0, campaign.follow_up_count)
    if not count:
        return []
    raw = campaign.follow_up_step_days or default_offsets(
        campaign.follow_up_interval_days, count
    )
    offsets: set[int] = set()
    for value in raw:
        try:
            day = int(value)
        except (TypeError, ValueError):
            continue
        if day > 0:
            offsets.add(day)
    return sorted(offsets)[:count]


def optimal_send_time(after: datetime, tz=None) -> datetime:
    """Move *after* forward to the recipient's next weekday business morning.

    Kept as the module's public name for timing (it is exported and called from
    the scheduler), but the policy now lives in :mod:`app.services.send_time`.

    The old Tue-Thu constraint is gone. It came from submission-timing research
    about *job applications* and was generalized to follow-ups; restricting
    nudges to three days a week fights an explicit day-3/day-7 cadence, because
    a Thursday send pushes the day-3 nudge to the following Tuesday — five days
    late. Weekday mornings keep the defensible part of the finding.
    """
    return send_time.next_slot(after, tz)


def _local_day(when: datetime, tz) -> date:
    """The calendar day *when* falls on where the recruiter is."""
    return when.astimezone(tz or send_time.default_timezone()).date()


def _day_after(when: datetime, tz) -> datetime:
    """Midnight, in the recruiter's zone, on the day after *when*."""
    zone = tz or send_time.default_timezone()
    local = when.astimezone(zone) + timedelta(days=1)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _template_for_step(step: int, total: int) -> FollowUpTemplate:
    """The default angle for a step, before the thread's state is known."""
    if step >= total:
        return FollowUpTemplate.FINAL
    return FollowUpTemplate.NO_RESPONSE


def schedule_for_application(
    db: Session,
    application: Application,
    campaign: Campaign,
    *,
    sent_at: datetime | None = None,
) -> list[FollowUp]:
    """Write the campaign's whole follow-up sequence for one application.

    Idempotent: an application that already has scheduled follow-ups gets none
    added, so a re-sent or retried outreach can't double the sequence.
    """
    if not campaign.follow_up_enabled or campaign.follow_up_count < 1:
        return []

    existing = db.scalar(
        select(FollowUp.id).where(FollowUp.application_id == application.id)
    )
    if existing is not None:
        return []

    base = sent_at or datetime.now(UTC)
    offsets = sequence_offsets(campaign)[:5]
    if not offsets:
        return []
    total = len(offsets)

    # The recruiter's local morning, not ours. Resolved once for the whole
    # sequence rather than per step.
    recruiter = db.get(Recruiter, application.recruiter_id)
    tz = send_time.resolve_timezone(db, recruiter, application=application)

    created: list[FollowUp] = []
    previous: datetime | None = None
    for step, offset_days in enumerate(offsets, start=1):
        due = optimal_send_time(base + timedelta(days=offset_days), tz)
        # `sequence_offsets` guarantees the *offsets* are distinct days, but the
        # weekday-morning move can collapse two of them back onto one morning: a
        # send on Friday before the window puts day 2 (Sunday) and day 3 (Monday
        # before 09:00) both on Monday. The scheduler drains due rows in
        # `scheduled_at` order and the slot carries a random offset inside the
        # 09:00-11:00 window, so half the time the later step drew the earlier
        # minute and the FINAL "last note from me" went out ahead of the first
        # nudge — to the same recruiter, the same morning, an hour apart.
        if previous is not None and _local_day(due, tz) <= _local_day(previous, tz):
            due = optimal_send_time(_day_after(previous, tz), tz)
        previous = due
        follow_up = FollowUp(
            application_id=application.id,
            step=step,
            scheduled_at=due,
            status=FollowUpStatus.SCHEDULED,
            template=_template_for_step(step, total),
        )
        db.add(follow_up)
        created.append(follow_up)

    db.flush()
    return created


def cancel_for_application(
    db: Session, application_id: int, note: str
) -> int:
    """Cancel every still-scheduled follow-up on an application. Returns the count."""
    pending = db.scalars(
        select(FollowUp).where(
            FollowUp.application_id == application_id,
            FollowUp.status == FollowUpStatus.SCHEDULED,
        )
    ).all()
    for follow_up in pending:
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.note = note
    return len(pending)


def _has_sent_outreach(db: Session, application_id: int) -> bool:
    """Did any email on this application's threads actually leave?

    ``EmailStatus.SENT`` and nothing else. DRAFT is waiting on the user, QUEUED
    is waiting on the broker, and FAILED never arrived — none of the three is a
    message a follow-up can refer back to.
    """
    return (
        db.scalar(
            select(Email.id)
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .where(
                EmailThread.application_id == application_id,
                Email.direction == EmailDirection.SENT,
                Email.status == EmailStatus.SENT,
            )
            .limit(1)
        )
        is not None
    )


def thread_has_reply(db: Session, application_id: int) -> bool:
    """True when a *person* has written back on this application's threads.

    The reliable stop signal. The status check below it is not: statuses are set
    from the *classified intent* of a reply, and the intent map has no entry for
    ``QUESTION`` or ``OTHER`` — so a recruiter who wrote back "Who is this? What
    role?" left the application on ``OUTREACH_SENT`` and kept getting nudged.
    Bounces cannot be mistaken for a reply here: they are intercepted in
    ``inbox_tasks.poll_thread`` and never stored as RECEIVED rows.

    **An out-of-office is not a reply**, and it is the one inbound message this
    has to look at the intent of. ``inbox_tasks._apply_intent`` has always said
    so — it deliberately does not cancel the sequence for one, and says why —
    but that special case did nothing, because the responder is stored as a
    RECEIVED row like any other and this function stopped the drip on the row's
    existence alone. So the two modules disagreed and the stricter one won: a
    candidate who wrote to a recruiter on holiday got a single touch, one
    machine-generated "I am away until the 12th", and no follow-up ever again.
    That is the precise case the sequence exists for, and it was the one case
    that silently turned it off.

    ``OUT_OF_OFFICE`` is the only intent excluded, and it is excluded rather
    than every unmapped one: ``QUESTION``, ``OTHER`` and an unclassified null
    are all things a human may have typed, and the bias when we cannot tell is
    to stop chasing. Only a message the responder itself declared automatic is
    treated as not-a-reply.
    """
    found = db.scalar(
        select(Email.id)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .where(
            EmailThread.application_id == application_id,
            Email.direction == EmailDirection.RECEIVED,
            # ``is_distinct_from`` rather than ``!=``: SQL's inequality drops
            # NULLs, and a null intent is a reply nobody classified — which
            # must go on stopping the sequence.
            Email.intent.is_distinct_from(ReplyIntent.OUT_OF_OFFICE),
        )
        .limit(1)
    )
    return found is not None


def choose_template(
    db: Session, application: Application, follow_up: FollowUp
) -> FollowUpTemplate:
    """Pick the angle at send time from what the thread actually shows.

    * a reply with no decision  -> PARTIAL_RESPONSE
    * several sends, no reply   -> OPENED_NO_REPLY (they've seen it by now)
    * last step in the sequence -> FINAL
    * otherwise                 -> NO_RESPONSE

    ``OPENED_NO_REPLY`` briefs the composer with "they have seen the message",
    so the count behind it has to be of mail that actually reached the
    recipient. It was not: the query selected an ``Email.id`` rather than a
    count, so the test read ``is not None`` — true from the original outreach
    alone — and the step number was doing all the work. A step-2 follow-up whose
    step-1 nudge had been parked in the review queue and never approved was
    written as though the recruiter had been mailed twice, when they had been
    mailed once. Same shape as a draft counted as sent anywhere else in this
    service: ``FollowUp.status`` says the step was actioned, and only the email
    row says whether it went.
    """
    if application.status in REPLIED_STATUSES:
        return FollowUpTemplate.PARTIAL_RESPONSE

    if follow_up.template == FollowUpTemplate.FINAL:
        return FollowUpTemplate.FINAL

    sent_count = (
        db.scalar(
            select(func.count(Email.id))
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .where(
                EmailThread.application_id == application.id,
                Email.direction == EmailDirection.SENT,
                Email.status == EmailStatus.SENT,
            )
        )
        or 0
    )
    # More than the original outreach has gone out: they've had it twice.
    if sent_count > 1 and follow_up.step > 1:
        return FollowUpTemplate.OPENED_NO_REPLY
    return FollowUpTemplate.NO_RESPONSE


@dataclass
class ComposedFollowUp:
    subject: str
    body: str
    generated_with: str = "template"


# The angle each template takes, given to both the LLM and the deterministic path.
_TEMPLATE_BRIEF: dict[FollowUpTemplate, str] = {
    FollowUpTemplate.NO_RESPONSE: (
        "No reply yet. Send a short, friendly bump that adds one new piece of "
        "value or context — never just 'following up on my last email'."
    ),
    FollowUpTemplate.OPENED_NO_REPLY: (
        "They have seen the message but not replied. Assume they're busy, make "
        "replying as cheap as possible, and offer a single yes/no question."
    ),
    FollowUpTemplate.PARTIAL_RESPONSE: (
        "They replied but nothing was decided. Acknowledge their reply, then "
        "move the conversation one concrete step forward."
    ),
    FollowUpTemplate.FINAL: (
        "This is the last touch. Close the loop gracefully, leave the door open, "
        "and do not ask for anything demanding."
    ),
}


def _template_body(
    template: FollowUpTemplate, cand: CandidateContext, rec: RecruiterContext, step: int
) -> str:
    """Deterministic follow-up used when no LLM is available (and in tests)."""
    who = rec.name or (f"{rec.company} team" if rec.company else "there")
    role = (cand.target_roles or ["the role"])[0]

    bodies = {
        FollowUpTemplate.NO_RESPONSE: (
            f"Hi {who},\n\n"
            f"I wrote last week about {role} — I know inboxes get full, so I "
            f"wanted to put this back on top of yours.\n\n"
            f"Happy to send over a short summary of the work I've done that's "
            f"closest to what your team is building, if that's useful.\n\n"
            f"Best,\n{cand.name}"
        ),
        FollowUpTemplate.OPENED_NO_REPLY: (
            f"Hi {who},\n\n"
            f"Quick one so this is easy to answer: is {role} something your team "
            f"is actively hiring for right now?\n\n"
            f"If it's not the right time, just say so and I'll stop here.\n\n"
            f"Best,\n{cand.name}"
        ),
        FollowUpTemplate.PARTIAL_RESPONSE: (
            f"Hi {who},\n\n"
            f"Thanks for getting back to me. To keep this moving — would a short "
            f"call work in the next week or two? I'm flexible on timing.\n\n"
            f"Best,\n{cand.name}"
        ),
        FollowUpTemplate.FINAL: (
            f"Hi {who},\n\n"
            f"I'll leave this here so I'm not cluttering your inbox. If a "
            f"{role} opening comes up later, I'd be glad to hear about it.\n\n"
            f"Thanks for your time either way.\n\n"
            f"Best,\n{cand.name}"
        ),
    }
    return bodies[template]


_SYSTEM_PROMPT = (
    "You write one short follow-up email from a job seeker to a recruiter who "
    "has not moved the conversation forward. Rules: under 90 words; no guilt, no "
    "pressure, no 'just circling back'; exactly one easy call to action; never "
    "invent facts about the candidate or claim knowledge of open roles; keep the "
    "same thread subject.\n\n"
    'Return ONLY a JSON object: {"body": str}'
)


def compose_follow_up(
    template: FollowUpTemplate,
    cand: CandidateContext,
    rec: RecruiterContext,
    *,
    step: int,
    thread_subject: str | None,
) -> ComposedFollowUp:
    """Write the follow-up body, degrading to a template when the LLM can't."""
    # A follow-up goes out on the thread's own subject so it lands in the same
    # conversation. Prepending ``Re: `` unconditionally stacked a second marker
    # onto one that was already there — and on the fourth follow-up in a chain
    # that is what the subject looks like. Only a thread with no subject to
    # reply to gets a new one.
    subject = (
        conversation_stage.reply_subject(thread_subject)
        if (thread_subject or "").strip()
        else f"Following up — {cand.name}"
    )
    fallback = ComposedFollowUp(
        subject=subject, body=_template_body(template, cand, rec, step)
    )
    if not llm_is_configured():
        return fallback

    try:
        raw = chat_completion(
            [
                {"role": "system", "content": untrusted.guarded(_SYSTEM_PROMPT)},
                {
                    "role": "user",
                    "content": "\n".join(
                        [
                            f"Situation: {_TEMPLATE_BRIEF[template]}",
                            f"This is follow-up number {step}.",
                            f"Candidate: {cand.name}"
                            f"{f', {cand.headline}' if cand.headline else ''}",
                            f"Candidate skills: {', '.join((cand.skills or [])[:8]) or 'n/a'}",
                            f"Target role: {(cand.target_roles or ['n/a'])[0]}",
                            # The candidate's record ends there. The recruiter's
                            # name and company came off a crawled page or a From
                            # header, and the thread subject is the recruiter's
                            # own subject line carried back in — see
                            # :mod:`app.services.untrusted`. The "greet the
                            # team" rule stays outside the fence, because it is
                            # an instruction and an instruction inside a fence
                            # is what the clause tells the model to ignore.
                            "The recruiter and the thread, as recorded. If the "
                            "name below is n/a, greet the team rather than "
                            "inventing a person:",
                            untrusted.fence(
                                "\n".join(
                                    [
                                        f"Recruiter: {rec.name or 'n/a'}",
                                        f"Company: {rec.company or 'n/a'}",
                                        f"Thread subject: {thread_subject or 'n/a'}",
                                    ]
                                ),
                                label="recruiter record",
                            ),
                        ]
                    ),
                },
            ],
            model=settings.openrouter_model,
            temperature=0.6,
            max_tokens=1500,
        )
    except OpenRouterError as exc:
        logger.info("follow-up composition fell back to template: %s", exc)
        return fallback

    body = (extract_json_object(raw) or {}).get("body")
    if isinstance(body, str) and body.strip() and not looks_like_reasoning(body):
        return ComposedFollowUp(subject=subject, body=body.strip(), generated_with="llm")
    return fallback


def due_follow_ups(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 200,
    for_update: bool = False,
) -> list[FollowUp]:
    """Every scheduled follow-up whose time has come.

    *for_update* claims each row with ``FOR UPDATE SKIP LOCKED``, and the sweep
    that actions them passes it. Reading a row and finding it ``SCHEDULED`` is
    not a claim: two sweeps overlapping both see the same due list, both call
    :func:`prepare_follow_up_email` on it, and the recruiter gets the same nudge
    twice from the same candidate — the duplicate ``email_tasks._claim_for_send``
    prevents for a single row, arriving one step earlier as two rows.

    They do overlap. The ``process-due-follow-ups`` beat entry carries no
    ``expires``, so a worker that falls behind works through a queue of ticks
    rather than dropping the stale ones, and ``task_acks_late`` replays a sweep
    whose worker died before the ack. Nothing between the read and the write
    stopped a second sweep from starting.

    A claiming sweep holds its rows to its own commit, so the other one skips
    them and takes the next batch instead of blocking behind work that is
    already being done. Off by default: SQLite ignores row locking, and the
    read-only callers (and the tests that inspect a due list without actioning
    it) have nothing to claim.
    """
    stmt = (
        select(FollowUp)
        .where(
            FollowUp.status == FollowUpStatus.SCHEDULED,
            FollowUp.scheduled_at <= (now or datetime.now(UTC)),
        )
        .order_by(FollowUp.scheduled_at)
        .limit(limit)
    )
    if for_update:
        stmt = stmt.with_for_update(skip_locked=True)
    return list(db.scalars(stmt))


def prepare_follow_up_email(
    db: Session, follow_up: FollowUp
) -> tuple[Email | None, str | None]:
    """Turn a due follow-up into a queued email, or explain why it was skipped.

    Returns ``(email, skip_reason)``. A skip usually CANCELLEDs the row, so the
    caller only has to commit — but not always: a paused campaign leaves the row
    SCHEDULED so the step survives the pause. The caller tells the two apart by
    reading ``follow_up.status`` back, which is why it is the row and not the
    return value that carries the outcome.
    """
    application = db.get(Application, follow_up.application_id)
    if application is None:
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.note = "application no longer exists"
        return None, follow_up.note

    campaign = db.get(Campaign, application.campaign_id)
    recruiter = db.get(Recruiter, application.recruiter_id)

    # Deferred, not cancelled, and checked before anything is composed.
    #
    # `pause_campaign` promises to stop "cold outreach and its follow-ups", and
    # the second half of that was only half true: the send-time guard in
    # `email_tasks` refused to deliver a paused campaign's message, but nothing
    # stopped this function from *writing* it. So every step that came due
    # during a pause spent an LLM call, consumed the follow-up row — SENT, which
    # `due_follow_ups` never looks at again — and left a message QUEUED behind a
    # guard that would not release it and a stranded-send sweep that skips a
    # paused campaign on purpose. The sequence was burned down a step at a time
    # by a campaign that was supposed to be standing still.
    #
    # Leaving the row SCHEDULED costs one cheap re-examination per sweep and
    # gives the resume something to resume.
    if campaign is not None and campaign.status is CampaignStatus.PAUSED:
        return None, "the campaign is paused"

    if recruiter is None or recruiter.opted_out:
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.note = "recruiter opted out"
        return None, follow_up.note

    if recruiter.is_excluded:
        # Excluding a contact has to reach the work already queued against them,
        # or the follow-up scheduled last week still goes out this week — the
        # exact failure the flag exists to prevent.
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.note = "you excluded this contact"
        return None, follow_up.note

    if bounce_service.is_suppressed(recruiter):
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.note = "address is undeliverable"
        return None, follow_up.note

    if application.status in TERMINAL_STATUSES:
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.note = f"conversation closed ({application.status.value.lower()})"
        return None, follow_up.note

    stop_on_reply = campaign is None or campaign.follow_up_stop_on_reply
    # Ordered before the status check because it is strictly more reliable —
    # see thread_has_reply.
    if stop_on_reply and (
        thread_has_reply(db, application.id) or application.status in REPLIED_STATUSES
    ):
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.note = "recruiter replied"
        return None, follow_up.note

    thread = db.scalar(
        select(EmailThread)
        .where(EmailThread.application_id == application.id)
        .order_by(EmailThread.id)
    )
    if thread is None:
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.note = "no outreach thread to follow up on"
        return None, follow_up.note

    # And a thread is not a delivery. Every check above asks whether the
    # conversation should continue; none of them asked whether it had started.
    # A first touch parked in the review queue leaves a thread, an application
    # and a scheduled sequence behind it, so a step coming due against an
    # unapproved draft composed "just following up on my last email" and queued
    # it — arriving at a stranger as the first thing they had ever received
    # from this candidate. QUEUED counts as nothing too: it records that we
    # intend to send, and the broker may not have run yet.
    if not _has_sent_outreach(db, application.id):
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.note = "the outreach it follows was never sent"
        return None, follow_up.note

    # Import here: outreach_service imports this module for scheduling, and a
    # module-level import in both directions would be circular.
    from app.services.outreach_service import resolve_resume

    user = application.campaign.user if application.campaign else None
    resume = resolve_resume(db, user, campaign.resume_id) if user and campaign else None
    cand = CandidateContext.from_resume(
        resume, fallback_name=(user.full_name or user.email.split("@")[0]) if user else "there"
    )

    template = choose_template(db, application, follow_up)
    composed = compose_follow_up(
        template,
        cand,
        RecruiterContext.from_recruiter(recruiter),
        step=follow_up.step,
        thread_subject=thread.subject,
    )

    # A follow-up is outreach the recipient never asked for, so it answers to the
    # same policy as the first touch: a pause or a spent daily ceiling parks it as
    # a draft. An orphaned application with no campaign keeps its old behaviour of
    # queueing — there is no user intent on record to consult.
    from app.services import send_policy

    auto_send = (
        True
        if campaign is None or user is None
        else send_policy.evaluate(
            db, user, user.autopilot, intent=campaign.auto_send
        ).enabled
    )
    # And the same content gate the first touch answers to. A follow-up is the
    # second, third and fourth message a filter sees from this mailbox to this
    # recipient, so risky copy here is scored repeatedly against a sender whose
    # reputation is the thing the sequence is spending.
    auto_send, content = spam_risk.screen(
        composed.subject, composed.body, auto_send=auto_send
    )
    if content.should_review:
        logger.info(
            "follow-up %s held for review on content risk %d (application %s)",
            follow_up.step,
            content.risk,
            application.id,
        )

    # The RFC 5322 threading headers. Gmail's ``threadId`` — which the sender
    # sets from ``thread.gmail_thread_id`` — threads this for recipients reading
    # in Gmail; these are what everyone else threads on. A follow-up is the case
    # that needs them most: it is the second, third and fourth near-identical
    # message from one sender to one recipient, and without them each one lands
    # as an unrelated cold email in Outlook, Apple Mail and every ATS.
    #
    # ``(None, None)`` when nothing on the thread has a ``Message-ID`` we know —
    # an outreach sent before ``rfc_message_id`` existed, or one whose read-back
    # failed. Then the follow-up goes out exactly as it always did.
    in_reply_to, references = thread_headers.headers_for_next_message(db, thread)

    email = Email(
        thread_id=thread.id,
        direction=EmailDirection.SENT,
        status=EmailStatus.QUEUED if auto_send else EmailStatus.DRAFT,
        to_address=recruiter.email,
        subject=composed.subject,
        body_text=composed.body,
        auto_sent=auto_send,
        in_reply_to=in_reply_to,
        email_references=references,
    )
    db.add(email)
    db.flush()

    thread.message_count = (thread.message_count or 0) + 1
    follow_up.template = template
    follow_up.email_id = email.id
    follow_up.status = FollowUpStatus.SENT
    if auto_send:
        # Only the step that handed a message to the sender gets a send time. A
        # step parked for review has produced a draft that may never go out, and
        # `sent_at` is the one field on this row whose name is a claim about
        # delivery. `status` is not: it reads SENT for both, meaning "actioned".
        follow_up.sent_at = datetime.now(UTC)

    if application.status == ApplicationStatus.OUTREACH_SENT:
        # Imported here rather than at module scope: pipeline_board is small and
        # model-only, but this module is already the bottom of one import cycle
        # (outreach_service), and a second entry point into it is not worth the
        # risk.
        from app.services import pipeline_board

        pipeline_board.record_change(
            db,
            application,
            ApplicationStatus.FOLLOW_UP,
            source=StatusEventSource.AUTOMATIC,
            reason=f"follow-up {follow_up.step} sent",
        )

    return email, None


__all__ = [
    "REPLIED_STATUSES",
    "TERMINAL_STATUSES",
    "ComposedFollowUp",
    "cancel_for_application",
    "choose_template",
    "compose_follow_up",
    "default_offsets",
    "due_follow_ups",
    "optimal_send_time",
    "parse_step_days",
    "prepare_follow_up_email",
    "schedule_for_application",
    "sequence_offsets",
    "thread_has_reply",
]
