"""Auto-send policy — the one place that answers "may this outreach skip review?".

``AutopilotPreference.auto_send`` is the user's *intent*. It is not the answer,
because three other things can hold sending back with that intent still on:

* an unfinished **trial** — the user asked to approve the first N emails by hand
  before the agent takes over (``auto_send_trial_approvals``),
* an active **pause** (``auto_send_paused_until``),
* a **daily ceiling** on unreviewed sends (``auto_send_daily_limit``).

Composing those correctly is fiddly enough that doing it at each call site was
how this would eventually send something nobody agreed to. So every producer of
outreach — the campaign pipeline, auto-apply, follow-ups — asks :func:`evaluate`
instead of reading the column.

**A block here is never a drop.** Every caller turns a ``False`` into a ``DRAFT``
row in the review queue, so the work is preserved and the user can send it by
hand. That is what makes it safe for this module to fail closed on anything it is
unsure about, which it does throughout.

This governs *initial outreach only*. Replies to a real human recruiter can also
send unreviewed, but they run their own policy in
``recruiter_reply_service.auto_reply_allowed`` — a server-wide switch, a per-user
switch and a rolling daily cap — and do not consult this module. The two are
deliberately separate: the trial and pause here are about what the agent says to
strangers, which is a different question from what it says back to someone who
wrote in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.application import Application
from app.models.autopilot import AutopilotPreference
from app.models.campaign import Campaign, CampaignStatus
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.user import User

logger = logging.getLogger(__name__)

# Longest pause a single call may set. A pause is meant to be a breather, not a
# way to switch auto-send off and forget which of the two knobs is holding it —
# for "off indefinitely" the honest control is the auto_send toggle itself.
MAX_PAUSE_HOURS = 24 * 30

# Why auto-send was declined. Stable strings: the UI branches on these, and they
# end up in campaign notes.
REASON_REVIEW_MODE = "review_mode"
REASON_PAUSED = "paused"
REASON_TRIAL = "trial"
REASON_DAILY_LIMIT = "daily_limit"


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips naive datetimes; the comparisons here need tz-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class AutoSendDecision:
    """Whether outreach may go out unreviewed, and why not when it may not."""

    enabled: bool
    # One of the REASON_* codes when ``enabled`` is False, else None.
    code: str | None = None
    # A sentence for the user, safe to show in the UI or a campaign note.
    reason: str | None = None
    # Trial progress, so the UI can say "2 of 3 approved" without a second call.
    approvals_needed: int = 0
    approvals_done: int = 0

    def __bool__(self) -> bool:  # pragma: no cover - convenience at call sites
        return self.enabled


def auto_sent_in_last_24h(db: Session, user_id: int) -> int:
    """Outreach this user sent *without approving it* in the last 24h.

    Counts on ``Email.auto_sent``, the flag the sender stamps at send time.
    Reviewed sends are deliberately excluded: the ceiling exists to bound what
    the agent does unsupervised, and a message the user read and approved is not
    that, so approving mail must not eat into the agent's own allowance.
    """
    since = datetime.now(UTC) - timedelta(hours=24)
    return (
        db.scalar(
            select(func.count(Email.id))
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .join(Application, EmailThread.application_id == Application.id)
            .where(
                Application.user_id == user_id,
                Email.direction == EmailDirection.SENT,
                Email.status == EmailStatus.SENT,
                Email.auto_sent.is_(True),
                Email.sent_at >= since,
            )
        )
        or 0
    )


def auto_send_committed(db: Session, user_id: int) -> int:
    """Unreviewed outreach already spoken for: sent in the last 24h, *plus*
    queued and not yet away.

    :func:`auto_sent_in_last_24h` counts only what has left, which is the right
    number for "is the ceiling spent right now" and the wrong one for "may I
    queue another 200". A campaign queues its whole batch in one go and the
    worker trickles them out over hours, so during that window the sent count
    reads near zero while the sends are already committed — and a second launch
    reading it gets the full allowance again.

    QUEUED rows carry ``auto_sent`` already: the producers stamp the flag when
    they choose the status, not at delivery, precisely so a pending unreviewed
    send is distinguishable from a pending approved one.

    **A paused campaign's queued mail is not a commitment.** ``send_outreach_email``
    refuses a PAUSED campaign's message and returns it to the broker's care
    *still QUEUED* — deliberately, so resuming re-dispatches it — and nothing
    else will touch that row until the user presses resume. Counting it as spent
    made a pause permanent in a second, invisible way: a user who paused a
    campaign holding as many queued messages as their ceiling allows had
    auto-send blocked for every *other* producer from then on, indefinitely,
    under the message "Auto-send has used today's allowance". The ceiling is a
    rolling 24h window and the block outlived it by any margin — a month-old
    pause still read as today's allowance, and the only cures were resuming the
    campaign or deleting it.

    Resuming restores the count by construction: the rows are still QUEUED and
    the campaign is ACTIVE again, so they are spoken for exactly while something
    is actually going to send them.
    """
    since = datetime.now(UTC) - timedelta(hours=24)
    return (
        db.scalar(
            select(func.count(Email.id))
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .join(Application, EmailThread.application_id == Application.id)
            .join(Campaign, Application.campaign_id == Campaign.id)
            .where(
                Application.user_id == user_id,
                Email.direction == EmailDirection.SENT,
                Email.auto_sent.is_(True),
                or_(
                    and_(
                        Email.status == EmailStatus.SENT,
                        Email.sent_at >= since,
                    ),
                    # No date filter on the queued side: a message waiting to go
                    # out is a commitment whenever it was written. Only a paused
                    # campaign's is not waiting — see above.
                    and_(
                        Email.status == EmailStatus.QUEUED,
                        Campaign.status != CampaignStatus.PAUSED,
                    ),
                ),
            )
        )
        or 0
    )


def remaining_allowance(
    db: Session, user: User, pref: AutopilotPreference | None
) -> int | None:
    """How many *more* messages may be sent unreviewed. ``None`` is unlimited.

    :func:`evaluate` answers the yes/no question, which is all a producer of one
    email needs. A producer of a *batch* needs the number, because the ceiling
    is per-message and asking the yes/no question once for a hundred of them is
    how the ceiling gets bypassed — see ``outreach_service``.

    Never negative: a ceiling lowered below what is already committed reads as
    zero, not as a debt the user has to work off.
    """
    limit = pref.auto_send_daily_limit if pref else None
    if limit is None:
        return None
    return max(0, limit - auto_send_committed(db, user.id))


def evaluate(
    db: Session,
    user: User,
    pref: AutopilotPreference | None,
    *,
    intent: bool | None = None,
    now: datetime | None = None,
) -> AutoSendDecision:
    """Decide whether *user*'s next outreach email may skip the review queue.

    Ordered cheapest-and-most-decisive first: intent, then pause, then trial,
    then the daily ceiling (the only branch that costs a query).

    *intent* is for callers that carry their own record of consent — the campaign
    pipeline passes ``campaign.auto_send``. It is an **additional** requirement,
    never a replacement: the campaign flag and the user's standing switch both
    have to be on. A campaign is not allowed to out-vote someone who went into
    settings and turned auto-send off.

    A *missing* preference row is the one case the two paths read differently,
    because it means different things to each. With no *intent* to go on the row
    is the only record of consent, and its absence is "never opted in" — review
    mode. With an *intent* of True the user has just asked for this campaign to
    send, and an absent row is merely unconfigured, not a refusal; blocking there
    is what stranded follow-ups for every user who never opened the autopilot
    page. Nothing is skipped as a result — a row-less user has no trial, no pause
    and no ceiling for the branches below to check.
    """
    now = now or datetime.now(UTC)

    if intent is None:
        wants = bool(pref and pref.auto_send)
    else:
        wants = intent and (pref is None or pref.auto_send)

    if not wants:
        return AutoSendDecision(
            False,
            REASON_REVIEW_MODE,
            "Review mode — every email waits for your approval.",
        )
    if pref is None:
        return AutoSendDecision(True)

    needed = max(0, pref.auto_send_trial_approvals or 0)
    done = max(0, pref.auto_send_approved_count or 0)

    paused_until = _aware(pref.auto_send_paused_until)
    if paused_until is not None and paused_until > now:
        return AutoSendDecision(
            False,
            REASON_PAUSED,
            f"Auto-send is paused until {paused_until:%b %d, %H:%M} UTC.",
            approvals_needed=needed,
            approvals_done=done,
        )

    if done < needed:
        return AutoSendDecision(
            False,
            REASON_TRIAL,
            f"Approve {needed - done} more by hand and auto-send takes over "
            f"({done} of {needed} done).",
            approvals_needed=needed,
            approvals_done=done,
        )

    limit = pref.auto_send_daily_limit
    if limit is not None:
        # Commitments, not deliveries. This asked ``auto_sent_in_last_24h``,
        # which counts only what has already left — and *nothing* has left while
        # a producer is still writing its batch. Every message in one run
        # therefore read the same pre-run number and every one of them passed:
        # the follow-up sweep actioned its whole due list unreviewed, and an
        # auto-apply run queued a message per posting, however small the dial
        # was set. ``outreach_service`` is the only producer that escaped it,
        # because it separately asks :func:`remaining_allowance` — which has
        # counted commitments all along, and is now the same number this branch
        # decides on, so the two can no longer disagree about one user.
        #
        # A per-message producer needs nothing else: each QUEUED row is flushed
        # as it is written, so the next message in the same run sees it.
        spent = auto_send_committed(db, user.id)
        if spent >= limit:
            return AutoSendDecision(
                False,
                REASON_DAILY_LIMIT,
                f"Auto-send has used today's allowance ({spent} of {limit}). "
                "Anything new waits for you.",
                approvals_needed=needed,
                approvals_done=done,
            )

    return AutoSendDecision(True, approvals_needed=needed, approvals_done=done)


def record_approval(pref: AutopilotPreference | None, *, count: int = 1) -> None:
    """Book *count* hand-approvals toward the trial.

    Called for every approval, not only during a trial: the counter is the
    product's record of how much outreach this user has actually read, and a user
    who turns a trial on later should get credit for what they already reviewed
    rather than starting from zero.

    **Outreach approvals only** — the caller filters. The trial asks "have you
    read enough of what this agent writes to strangers to let it send unread?",
    and approving a reply to a recruiter who wrote to *you* does not answer that
    question. Counting replies here let a busy inbox graduate the trial on its
    own, which is the one thing the trial exists to prevent.
    """
    if pref is None or count <= 0:
        return
    pref.auto_send_approved_count = (pref.auto_send_approved_count or 0) + count


def pause(
    pref: AutopilotPreference, hours: int, *, now: datetime | None = None
) -> datetime:
    """Hold auto-send for *hours*, returning when it resumes.

    Extends rather than shortens: pausing for an hour while a three-day pause is
    already running keeps the later deadline. "Pause" should never be the verb
    that made something start sending sooner.
    """
    now = now or datetime.now(UTC)
    hours = max(1, min(int(hours), MAX_PAUSE_HOURS))
    until = now + timedelta(hours=hours)
    current = _aware(pref.auto_send_paused_until)
    pref.auto_send_paused_until = max(until, current) if current else until
    return pref.auto_send_paused_until


def resume(pref: AutopilotPreference) -> None:
    """Clear an auto-send pause. A no-op when none is active."""
    pref.auto_send_paused_until = None


def status(
    db: Session,
    user: User,
    pref: AutopilotPreference | None,
    *,
    now: datetime | None = None,
) -> dict:
    """The whole auto-send picture for the settings UI, in one payload."""
    now = now or datetime.now(UTC)
    decision = evaluate(db, user, pref, now=now)
    paused_until = _aware(pref.auto_send_paused_until) if pref else None
    paused = bool(paused_until and paused_until > now)
    limit = pref.auto_send_daily_limit if pref else None

    return {
        "auto_send": bool(pref.auto_send) if pref else False,
        # The live answer, which is not the toggle — see the module docstring.
        "sending_now": decision.enabled,
        "blocked_reason_code": decision.code,
        "blocked_reason": decision.reason,
        "paused": paused,
        "paused_until": paused_until if paused else None,
        "trial_approvals": decision.approvals_needed,
        "approvals_done": decision.approvals_done,
        "trial_remaining": max(0, decision.approvals_needed - decision.approvals_done),
        "daily_limit": limit,
        # The number the decision above was actually made on, so the dial the
        # user reads and the answer they get cannot disagree. Reporting
        # deliveries here while `evaluate` counted commitments would show "2 of
        # 5 used" beside a refusal — which reads as a bug in the product rather
        # than as the ceiling doing its job.
        "auto_sent_today": auto_send_committed(db, user.id) if limit is not None else None,
    }


__all__ = [
    "MAX_PAUSE_HOURS",
    "REASON_DAILY_LIMIT",
    "REASON_PAUSED",
    "REASON_REVIEW_MODE",
    "REASON_TRIAL",
    "AutoSendDecision",
    "auto_send_committed",
    "auto_sent_in_last_24h",
    "evaluate",
    "pause",
    "record_approval",
    "remaining_allowance",
    "resume",
    "status",
]
