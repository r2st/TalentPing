"""Does chasing work? — the persistence curve.

    persistence(threads) -> FollowUpEffect

The overview already reports how much mail went out (``OutreachVolume``) and how
often an application came back (``response_rate``). Neither answers the question
a candidate actually has at the end of a week: *is the third follow-up earning
anything, or am I spending a mailbox's reputation on it?*

The unit is a **touch** — one outbound message that actually left, counted in
the order it left, and only up to the first reply. Everything sent after the
recruiter writes back is a conversation, not a chase; folding those in would
make every thread that went well look like it took four attempts to land.

Each application contributes to every step it *reached* and to at most one step
as a reply::

    sent at step k      the thread was still silent when touch k went out
    replied at step k   the first reply landed after touch k and before k+1

so the rate at step k is a conditional — "of the threads still silent after k
touches, how many answered this one" — and not a share of all applications. The
share version falls at every step by construction, because each step is sent to
fewer threads than the one before it, and it would make a follow-up that works
perfectly well look like a failing one.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.models.email import Email, EmailDirection, EmailStatus

# A step's rate is only worth reading — and only ever worth acting on — once
# this many applications have reached it. Higher than the ranked lists'
# ``MIN_SAMPLE`` on purpose: a thin row in a table is discounted by the reader's
# own eyes, and this number is attached to advice about giving up on a step.
MIN_STEP_SENDS = 5

# Below this many contacted applications the block reports its counts and makes
# no claim. Persistence advice off six threads is how a user talks themselves
# into deleting the follow-up that was about to work.
MIN_CONTACTED = 10

# The ladder never runs longer than this. A campaign's own sequence is bounded
# by its configuration, but the ladder is derived from mail rather than from
# that configuration, and an imported thread can carry any number of outbound
# messages before its first reply. A reply beyond the cap is attributed to the
# last reported step rather than dropped.
MAX_STEPS = 8


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; the comparisons below need tz-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def step_label(step: int) -> str:
    """How a rung of the ladder reads. Step 0 is the opener, not a follow-up."""
    return "First email" if step == 0 else f"Follow-up {step}"


@dataclass
class StepOutcome:
    """One rung: how many threads reached it, and how many answered it."""

    step: int
    label: str
    # Applications still silent when this touch went out.
    sent: int = 0
    # Of those, the ones whose first reply landed before the next touch.
    replies: int = 0
    reply_rate: float = 0.0
    # False when too few threads reached this rung for its rate to mean
    # anything. The row is still returned — captioned, not hidden, the same way
    # the ranked lists treat a thin segment.
    reliable: bool = False


@dataclass
class FollowUpEffect:
    steps: list[StepOutcome] = field(default_factory=list)
    # Applications with at least one message actually sent.
    contacted: int = 0
    replied: int = 0
    # Replies that arrived only after at least one follow-up — the number the
    # whole block exists for. Without it a user cannot tell whether their reply
    # rate would survive switching follow-ups off.
    replies_after_follow_up: int = 0
    share_from_follow_ups: float = 0.0
    # The furthest step that has ever earned a reply. None when nothing has
    # replied at all, which is a different problem and gets a different sentence.
    last_productive_step: int | None = None
    # Sends spent past ``last_productive_step`` for nothing.
    wasted_sends: int = 0
    # Where to stop, offered only when the dead tail is big enough to be
    # evidence rather than a run of bad luck. None means "keep going".
    suggested_last_step: int | None = None
    summary: str = ""
    min_step_sends: int = MIN_STEP_SENDS
    min_contacted: int = MIN_CONTACTED


def _ladder(emails: list[Email]) -> tuple[int, bool] | None:
    """One application's ``(touches reached, did it reply)``.

    ``None`` for an application nothing was ever sent for — it has not reached
    step 0 and belongs in no denominator here.

    A reply timestamped at the same instant as the send it answers is credited
    to that send rather than to nothing: the clamp below is what stops a thread
    whose imported reply shares a second with its outreach from counting as a
    reply that belongs to no step, which would leave ``replied`` above the sum
    of the ladder.
    """
    sends = sorted(
        at
        for e in emails
        if e.direction == EmailDirection.SENT
        and e.status == EmailStatus.SENT
        and (at := _aware(e.sent_at)) is not None
    )
    if not sends:
        return None

    # Inbound mail predating the outreach is not a reply to it — see
    # ``reply_metrics.reply_latency_days`` for the same guard and why.
    first_reply = min(
        (
            at
            for e in emails
            if e.direction == EmailDirection.RECEIVED
            and (at := _aware(e.sent_at)) is not None
            and at >= sends[0]
        ),
        default=None,
    )
    if first_reply is None:
        return min(len(sends), MAX_STEPS), False
    reached = max(1, sum(1 for at in sends if at < first_reply))
    return min(reached, MAX_STEPS), True


def persistence(threads: Iterable[list[Email]]) -> FollowUpEffect:
    """Build the ladder from one email list per application."""
    sent_at_step: dict[int, int] = defaultdict(int)
    replies_at_step: dict[int, int] = defaultdict(int)
    contacted = replied = replies_after_follow_up = 0

    for emails in threads:
        outcome = _ladder(emails)
        if outcome is None:
            continue
        reached, did_reply = outcome
        contacted += 1
        for step in range(reached):
            sent_at_step[step] += 1
        if did_reply:
            replied += 1
            landed = reached - 1
            replies_at_step[landed] += 1
            if landed > 0:
                replies_after_follow_up += 1

    steps = [
        StepOutcome(
            step=step,
            label=step_label(step),
            sent=sent_at_step[step],
            replies=replies_at_step.get(step, 0),
            reply_rate=(
                round(replies_at_step.get(step, 0) / sent_at_step[step], 3)
                if sent_at_step[step]
                else 0.0
            ),
            reliable=sent_at_step[step] >= MIN_STEP_SENDS,
        )
        for step in sorted(sent_at_step)
    ]

    productive = [s.step for s in steps if s.replies]
    last_productive = max(productive) if productive else None
    wasted = (
        sum(s.sent for s in steps if s.step > last_productive)
        if last_productive is not None
        else 0
    )
    # Only a dead tail with real volume behind it earns the "stop here"
    # recommendation. One unanswered send past the last reply is noise, and the
    # cost of acting on it is a follow-up the user never sends again.
    suggested_last_step = (
        last_productive
        if (
            last_productive is not None
            and last_productive < (steps[-1].step if steps else 0)
            and wasted >= MIN_STEP_SENDS
            and contacted >= MIN_CONTACTED
        )
        else None
    )

    return FollowUpEffect(
        steps=steps,
        contacted=contacted,
        replied=replied,
        replies_after_follow_up=replies_after_follow_up,
        share_from_follow_ups=(
            round(replies_after_follow_up / replied, 3) if replied else 0.0
        ),
        last_productive_step=last_productive,
        wasted_sends=wasted,
        suggested_last_step=suggested_last_step,
        summary=_summarise(
            contacted=contacted,
            replied=replied,
            replies_after_follow_up=replies_after_follow_up,
            steps=steps,
            suggested_last_step=suggested_last_step,
            wasted=wasted,
        ),
    )


def _summarise(
    *,
    contacted: int,
    replied: int,
    replies_after_follow_up: int,
    steps: list[StepOutcome],
    suggested_last_step: int | None,
    wasted: int,
) -> str:
    """The ladder in one sentence, or an honest admission that it is too early."""
    if not contacted:
        return "Nothing has been sent yet."
    if contacted < MIN_CONTACTED:
        return (
            f"{contacted} applications contacted. At {MIN_CONTACTED} there is "
            "enough here to say whether following up is worth it."
        )
    if not replied:
        total_sends = sum(s.sent for s in steps)
        return (
            f"{total_sends} messages across {contacted} applications and no "
            "replies at any step — the problem is upstream of persistence."
        )

    opener = replied - replies_after_follow_up
    lead = (
        f"{replies_after_follow_up} of your {replied} replies arrived only "
        f"after a follow-up; {opener} came back on the first email."
    )
    if suggested_last_step is None:
        return lead
    dead = [s for s in steps if s.step > suggested_last_step]
    first_dead = dead[0].label.lower() if dead else "the next step"
    return (
        f"{lead} Nothing past {step_label(suggested_last_step).lower()} has ever "
        f"answered — {first_dead} onward has gone out {wasted} times for nothing. "
        f"Stop after {step_label(suggested_last_step).lower()}."
    )
