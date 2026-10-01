"""Where applications stop moving, measured from the transitions already stored.

:class:`~app.models.status_event.ApplicationStatusEvent` has recorded every
status an application has ever held, append-only, since the board learned to let
a user drag a card. It is read by exactly one thing —
``routers/board`` rendering the history panel for a single application — so the
product can say how *one* application got where it is and has never been able to
say where applications in general stop.

``DashboardStats.median_days_to_reply`` is the closest thing, and it answers a
different question: how long the *first* reply takes. It cannot see a thread that
reached ``SCHEDULING`` and sat there for five weeks, because nothing after the
first reply moves that number at all.

**The measurement.** Each event starts a dwell in its ``to_status``; the next
event for that application ends it. The last event of a chain has no successor,
so that dwell is still running.

**Those two are not the same measurement and must never be pooled.** A finished
dwell is an observation. A running one is a lower bound — the card has been in
``CONTACTED`` for eleven days *so far*, and averaging eleven into the median as
though it were the final answer is how a stage full of stuck applications reports
a healthy median. Excluding running dwells entirely is the opposite error and the
more seductive one, because it looks rigorous: the applications that never left
are exactly the ones the question is about, and dropping them means the median is
computed over survivors only and every stage looks fast.

So both are reported, separately and side by side: :attr:`StageVelocity.median_days`
over dwells that ended, and :attr:`StageVelocity.stuck` / :attr:`StageVelocity.
longest_stuck_days` over the ones that have not. A stage whose median is two days
and whose ``stuck`` is forty is not a fast stage.

**Terminal statuses are not stuck.** An application sitting in ``NOT_INTERESTED``
is not waiting for anything — it is over. ``CLOSED_STATUSES`` decides this, and
without it every rejection the user ever received would be reported as a stalled
card in the stage that loses the most applications.

**Re-entry counts twice.** A card dragged back to a stage it already visited
starts a second dwell there. Both are real; the stage was genuinely occupied
twice, and collapsing them would understate the total time spent in it.

Pure apart from one read: a session and a user in, a report out.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.application import CLOSED_STATUSES, ApplicationStatus
from app.models.status_event import ApplicationStatusEvent
from app.models.user import User

#: Applications that must have passed through a stage before its numbers are
#: reported. Below this a median is one or two cards and reads as a finding.
#: The same discipline as ``analytics.MIN_FOCUS_SAMPLE``, for the same reason.
MIN_STAGE_SAMPLE = 3

#: Transitions the user must have overall before any of this is said aloud.
MIN_TOTAL_EVENTS = 10


def _utc(when: datetime | None) -> datetime | None:
    """A timestamp read off a row, made comparable.

    SQLite hands these back naive, and subtracting a naive datetime from an
    aware one raises rather than answering wrongly — so it would not go
    unnoticed. It would go *uncaught*, in a report, at the point where the
    numbers are already on screen.
    """
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


@dataclass(frozen=True)
class StageVelocity:
    """How long applications spend in one stage, and how many never leave."""

    status: str
    #: Distinct visits to this stage — a card dragged back counts twice.
    entered: int
    #: Visits that ended, i.e. the application moved on afterwards.
    moved_on: int
    #: Median days of the visits that *ended*. ``None`` when none have.
    median_days: float | None
    #: The longest a visit took, among those that ended.
    slowest_days: float | None
    #: Live applications sitting in this stage right now, terminal ones
    #: excluded — a rejection is finished, not stalled.
    stuck: int
    #: How long the longest of those has been sitting. ``None`` when none are.
    longest_stuck_days: float | None

    @property
    def exit_rate(self) -> float:
        """Share of visits that ended. The number that says where things die."""
        return round(self.moved_on / self.entered, 3) if self.entered else 0.0

    @property
    def reliable(self) -> bool:
        """Whether this row carries enough visits to read as a finding."""
        return self.entered >= MIN_STAGE_SAMPLE


@dataclass(frozen=True)
class VelocityReport:
    stages: list[StageVelocity] = field(default_factory=list)
    #: Applications with at least one recorded transition.
    applications: int = 0
    #: Transitions read.
    events: int = 0
    #: The stage worth acting on: the slowest-exiting reliable one that still
    #: has applications sitting in it. ``None`` when nothing qualifies.
    worst_stage: str | None = None
    #: Why the report is empty, when it is.
    note: str = ""
    min_stage_sample: int = MIN_STAGE_SAMPLE

    @property
    def has_findings(self) -> bool:
        return bool(self.stages)


@dataclass
class _Visit:
    """One occupancy of one stage."""

    status: ApplicationStatus
    started: datetime
    ended: datetime | None = None

    def days(self, now: datetime) -> float:
        end = self.ended or now
        return round(max((end - self.started).total_seconds(), 0.0) / 86400, 2)

    @property
    def finished(self) -> bool:
        return self.ended is not None


def _visits(db: Session, user: User) -> tuple[list[_Visit], int, int]:
    """Every stage occupancy the user's applications have had.

    One query, four columns. Ordered by ``(application_id, created_at, id)`` —
    the id breaks ties, because two transitions written in the same commit share
    a timestamp to the microsecond often enough to matter, and ordering them
    wrongly reverses a dwell and produces a negative one.
    """
    rows = list(
        db.execute(
            select(
                ApplicationStatusEvent.application_id,
                ApplicationStatusEvent.to_status,
                ApplicationStatusEvent.created_at,
            )
            .where(ApplicationStatusEvent.user_id == user.id)
            .order_by(
                ApplicationStatusEvent.application_id,
                ApplicationStatusEvent.created_at,
                ApplicationStatusEvent.id,
            )
        )
    )

    by_application: dict[int, list[_Visit]] = defaultdict(list)
    for application_id, to_status, created_at in rows:
        started = _utc(created_at)
        if started is None:  # pragma: no cover - the column is non-null
            continue
        chain = by_application[application_id]
        if chain:
            # The previous visit ends where this one starts.
            chain[-1].ended = started
        chain.append(_Visit(status=to_status, started=started))

    visits = [visit for chain in by_application.values() for visit in chain]
    return visits, len(by_application), len(rows)


def report(db: Session, user: User, *, now: datetime | None = None) -> VelocityReport:
    """Per-stage dwell for *user*, or an explanation of why there isn't one."""
    now = now or datetime.now(UTC)
    visits, applications, events = _visits(db, user)

    if events < MIN_TOTAL_EVENTS:
        return VelocityReport(
            applications=applications,
            events=events,
            note=(
                f"{events} status changes recorded so far. At "
                f"{MIN_TOTAL_EVENTS} there is enough here to say where "
                "applications stop moving."
            ),
        )

    grouped: dict[ApplicationStatus, list[_Visit]] = defaultdict(list)
    for visit in visits:
        grouped[visit.status].append(visit)

    stages: list[StageVelocity] = []
    for status, rows in grouped.items():
        finished = [v for v in rows if v.finished]
        # A visit that has not ended is only "stuck" if the application is still
        # waiting on something. A card resting in NOT_INTERESTED has finished
        # its journey; counting it as stalled would put every rejection the user
        # ever received into the stage that loses the most applications.
        running = [
            v for v in rows if not v.finished and status not in CLOSED_STATUSES
        ]
        durations = [v.days(now) for v in finished]

        stages.append(
            StageVelocity(
                status=status.value,
                entered=len(rows),
                moved_on=len(finished),
                median_days=round(median(durations), 2) if durations else None,
                slowest_days=max(durations) if durations else None,
                stuck=len(running),
                longest_stuck_days=(
                    max(v.days(now) for v in running) if running else None
                ),
            )
        )

    # The pipeline's own order, so the report reads as a funnel rather than as a
    # ranking. Which stage is worst is answered by `worst_stage`, once, instead
    # of by making the reader infer it from a re-sorted list.
    order = {status: index for index, status in enumerate(ApplicationStatus)}
    stages.sort(key=lambda s: order[ApplicationStatus(s.status)])

    return VelocityReport(
        stages=stages,
        applications=applications,
        events=events,
        worst_stage=_worst(stages),
        min_stage_sample=MIN_STAGE_SAMPLE,
    )


def _worst(stages: list[StageVelocity]) -> str | None:
    """The stage worth doing something about.

    Lowest exit rate among stages that have enough visits to mean anything *and*
    still have applications sitting in them. Both conditions matter: a stage with
    two visits is not evidence, and a stage nothing is waiting in is not a place
    to spend an afternoon however badly it scored historically.
    """
    candidates = [s for s in stages if s.reliable and s.stuck > 0]
    if not candidates:
        return None
    # Ties broken by how many are stuck, then by name, so the answer does not
    # move between two reads of the same data.
    return min(candidates, key=lambda s: (s.exit_rate, -s.stuck, s.status)).status


__all__ = [
    "MIN_STAGE_SAMPLE",
    "MIN_TOTAL_EVENTS",
    "StageVelocity",
    "VelocityReport",
    "report",
]
