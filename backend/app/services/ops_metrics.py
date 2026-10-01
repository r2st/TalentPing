"""One read that answers "is the deployment working right now?".

Every number here was already derivable, and none of it was reachable. The
dashboard and analytics routers report per-user figures to the user they belong
to, which is the right design for a product and the wrong one for an operator:
they cannot say whether *the deployment* sent anything this morning without
querying Postgres by hand, and the operator is the person who finds out first
that it did not.

The specific failures this is shaped around are the ones that have actually
happened here, each of which was invisible for days:

- **The broker quietly duplicating work.** A deferred send is held unacked in
  worker memory, so an ``unacked`` count that climbs without a matching queue
  is the signature of the redelivery storm (see
  ``BROKER_VISIBILITY_TIMEOUT`` in :mod:`app.tasks.celery_app`). Nothing in the
  product exposed either number.
- **Beat stopping after a deploy.** ``systemctl is-active`` reports a
  crash-looping unit as active, so the honest test is not whether the process
  exists but whether the work it schedules has happened lately —
  ``scans.last_scan_at``.
- **Every Gmail grant dying at once.** Grants expire on a seven-day clock in a
  Testing-mode OAuth project. One revoked mailbox is a user problem; all of
  them on the same morning is a deployment problem, and only the count shows
  the difference.
- **Sends that stop without failing.** ``queued`` growing while ``sent_today``
  stays flat is the shape of a send path that is gated rather than broken —
  reputation hold, business-hours deferral, missing grant — and it looks like
  nothing at all in a log of errors.

Two decisions worth stating.

**Counts, not rows.** Everything is an aggregate over the whole deployment; no
message bodies, no addresses, no names. The endpoint is admin-only, but an
operational summary that cannot leak a user's correspondence is one fewer thing
to be careful with, and the operator's question is about volume anyway.

**A broker that cannot be reached is ``None``, never zero.** Zero queued
messages and an unreachable broker are opposite emergencies, and a metric that
renders them identically is worse than an absent one — which is the whole
lesson of this application's health endpoint. Every field that can be unknown
is typed to say so.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignStatus
from app.models.dead_letter import STATUS_NEW, DeadLetterJob
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_bounce import BounceKind, EmailBounce
from app.models.follow_up import FollowUp, FollowUpStatus
from app.models.gmail_account import GmailAccount
from app.models.recruiter_scan_run import RecruiterScanRun

logger = logging.getLogger(__name__)

def _day_start(now: datetime) -> datetime:
    """The window "today" means.

    UTC rather than any user's local day, because this is a deployment-wide
    figure and users span time zones — a per-user local day would make the
    total depend on who happens to be signed up.

    Worth knowing when reading ``sent_today``: sends are deliberately deferred
    into the *recipient's* business hours, so a low figure early in the UTC day
    is the scheduler working, not the sender failing. ``queued`` is the number
    that distinguishes them.
    """
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


#: How far back "recently" reaches for the scan health figures. An hour is
#: comfortably more than every inbound cadence in the beat schedule, so a
#: healthy deployment always has runs in the window and an empty window means
#: something stopped rather than that nothing was due.
_RECENT = timedelta(hours=1)

#: How long a campaign may stay ACTIVE before it is called stalled.
#:
#: Deliberately far past any legitimate slowness. Sends are deferred into each
#: recipient's business hours, so a batch launched on a Friday genuinely has
#: mail outstanding until Monday, and a threshold tight enough to catch a
#: campaign that died an hour ago would fire on every weekend this deployment
#: has. A week clears that, and clears the trickle a large batch takes by
#: design, so what is left is the case worth a sentence: a run whose worker
#: died mid-batch, which nothing else notices.
#:
#: The complementary failure — ACTIVE with *nothing* outstanding — is not here
#: because it is no longer reachable: `outreach_service.maybe_complete_campaign`
#: runs at every drain. This is the half that check cannot see, because the
#: work really is still pending; it is just never going to happen.
_STALLED_AFTER = timedelta(days=7)

#: Bound on the broker read. Same reasoning as the health check's: this runs
#: inside an HTTP request, and the interesting broker failure is the one that
#: accepts and then does not answer.
BROKER_TIMEOUT_SECONDS = 2.0

#: Kombu's Redis transport parks delivered-but-unacknowledged messages in a
#: hash under this key. Named rather than inlined because it is the number that
#: identifies the redelivery storm, and a reader has to be able to find out
#: what it means.
UNACKED_KEY = "unacked"


@dataclass(frozen=True)
class MailMetrics:
    sent_today: int
    failed_today: int
    replies_today: int
    hard_bounces_today: int
    soft_bounces_today: int
    #: Approved and waiting for the sender. Not a backlog by itself — see the
    #: module docstring on business-hours deferral.
    queued: int
    #: Drafts flagged for a human. The number that grows when the reputation
    #: gate is holding mail back.
    needs_review: int


@dataclass(frozen=True)
class QueueMetrics:
    #: Messages sitting in the Celery queue, or ``None`` if the broker could
    #: not be read.
    broker_depth: int | None
    #: Messages delivered to a worker and not yet acknowledged. Expected to be
    #: non-zero here — deferred sends live in this set legitimately — so it is
    #: the *trend* that matters, not the value.
    broker_unacked: int | None
    broker_error: str | None
    dead_letters_new: int
    follow_ups_due: int


@dataclass(frozen=True)
class ScanMetrics:
    #: The most recent inbound scan across every mailbox. The practical test of
    #: whether beat is alive.
    last_scan_at: datetime | None
    runs_last_hour: int
    failed_runs_last_hour: int
    detected_last_hour: int
    mailboxes_connected: int
    mailboxes_revoked: int


@dataclass(frozen=True)
class OpsSnapshot:
    generated_at: datetime
    mail: MailMetrics
    campaigns: dict[str, int]
    #: ACTIVE campaigns launched longer ago than :data:`_STALLED_AFTER`.
    #:
    #: A separate field rather than another key in ``campaigns``, which holds
    #: one entry per `CampaignStatus` and nothing else — a client rendering
    #: that dict as a status breakdown would show "stalled" as a status the
    #: product does not have, and a stalled campaign is also still counted
    #: under ``active``, so the two would not add up.
    campaigns_stalled: int
    queue: QueueMetrics
    scans: ScanMetrics
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot(db: Session, *, now: datetime | None = None) -> OpsSnapshot:
    """Everything above, in one call.

    *now* is injectable so the day boundary can be asserted rather than
    approached — a test that computes "today" the same way the code does
    proves only that the two agree.
    """
    now = now or datetime.now(UTC)
    since = _day_start(now)
    recent = now - _RECENT

    mail = _mail(db, since)
    queue, broker_error = _queue(db, now)
    scans = _scans(db, recent)
    stalled = _stalled_campaigns(db, now)
    return OpsSnapshot(
        generated_at=now,
        mail=mail,
        campaigns=_campaigns(db),
        campaigns_stalled=stalled,
        queue=queue,
        scans=scans,
        warnings=_warnings(mail, queue, scans, stalled, broker_error, now),
    )


def _count(db: Session, model: Any, *criteria: Any) -> int:
    return db.scalar(select(func.count(model.id)).where(*criteria)) or 0


def _mail(db: Session, since: datetime) -> MailMetrics:
    return MailMetrics(
        # `sent_at`, not `created_at`: a message composed yesterday and
        # delivered this morning is today's send, and the difference is the
        # normal case here rather than an edge one.
        sent_today=_count(
            db, Email, Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.SENT, Email.sent_at >= since,
        ),
        # A write-off has no timestamp of its own, so this leans on
        # ``updated_at`` (``onupdate`` on the mixin). Approximate in one
        # direction only: a FAILED row touched today for some other reason
        # would be counted. Nothing updates a written-off message again, so in
        # practice the two coincide — and an over-count on a failure metric is
        # the safe direction for it to be wrong in.
        failed_today=_count(
            db, Email, Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.FAILED, Email.updated_at >= since,
        ),
        # ``sent_at`` on an inbound row is when the *recruiter* sent it, not
        # when the scan found it (see ``inbox_tasks``, which stores the
        # message's own time). That is the right clock for this: a mailbox that
        # was unreachable all morning and catches up at noon reports its replies
        # under the hours they were written, not as a spike at noon.
        replies_today=_count(
            db, Email, Email.direction == EmailDirection.RECEIVED,
            Email.sent_at >= since,
        ),
        hard_bounces_today=_count(
            db, EmailBounce, EmailBounce.kind == BounceKind.HARD,
            EmailBounce.occurred_at >= since,
        ),
        soft_bounces_today=_count(
            db, EmailBounce, EmailBounce.kind == BounceKind.SOFT,
            EmailBounce.occurred_at >= since,
        ),
        queued=_count(
            db, Email, Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.QUEUED,
        ),
        needs_review=_count(
            db, Email, Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.DRAFT, Email.needs_attention.is_(True),
        ),
    )


def _campaigns(db: Session) -> dict[str, int]:
    """Every status, including the ones at zero.

    Absent keys would make a client render "no failed campaigns" and "this
    field was not reported" identically, which is the same mistake as a health
    check that cannot fail.
    """
    rows = db.execute(
        select(Campaign.status, func.count(Campaign.id)).group_by(Campaign.status)
    ).all()
    counts = {status.value: 0 for status in CampaignStatus}
    for status, count in rows:
        key = status.value if isinstance(status, CampaignStatus) else str(status)
        counts[key] = count
    return counts


def _stalled_campaigns(db: Session, now: datetime) -> int:
    """ACTIVE campaigns that started long enough ago to have finished by now.

    Judged on ``started_at`` and not on the age of the last message sent. The
    two differ exactly when the campaign has sent nothing at all — a run that
    died during discovery, before a single message existed — and that is the
    case with no other trace anywhere in this snapshot.

    A NULL ``started_at`` is not counted. It means the row never got as far as
    beginning, which is a different fault from stopping partway, and treating
    the two as one would put every such campaign permanently in the warning.
    """
    return _count(
        db,
        Campaign,
        Campaign.status == CampaignStatus.ACTIVE,
        Campaign.started_at.is_not(None),
        Campaign.started_at < now - _STALLED_AFTER,
    )


def _queue(db: Session, now: datetime) -> tuple[QueueMetrics, str | None]:
    depth, unacked, error = _broker_depth()
    return (
        QueueMetrics(
            broker_depth=depth,
            broker_unacked=unacked,
            broker_error=error,
            dead_letters_new=_count(
                db, DeadLetterJob, DeadLetterJob.status == STATUS_NEW
            ),
            # Due, not merely scheduled: a follow-up dated next Tuesday is not a
            # backlog, and counting it as one would put every healthy deployment
            # permanently in the red.
            follow_ups_due=_count(
                db, FollowUp, FollowUp.status == FollowUpStatus.SCHEDULED,
                FollowUp.scheduled_at <= now,
            ),
        ),
        error,
    )


def _broker_depth() -> tuple[int | None, int | None, str | None]:
    """Ask Redis how much work is waiting, without pretending to know if it can't.

    Reaches past Kombu to the Redis client because there is no transport-neutral
    way to ask this — ``LLEN`` on the queue and ``HLEN`` on the ``unacked`` hash
    are the Redis transport's own storage layout. That is a coupling worth
    naming: on a different broker these come back ``None`` with a reason, which
    is the correct answer rather than a crash.

    ``ensure_connection(max_retries=0)`` before touching ``default_channel``,
    and not merely for tidiness: reading the channel connects implicitly, under
    kombu's *default* failover policy, which retries with a growing backoff.
    A down broker therefore held this call — and the admin request carrying it,
    and the database connection that request had checked out — for a minute or
    more, which is the metrics page becoming an outage of its own during the
    incident it exists to describe.
    """
    try:
        from app.tasks.celery_app import celery_app

        queue = celery_app.conf.task_default_queue or "celery"
        connection = celery_app.connection_for_read()
        try:
            connection.ensure_connection(max_retries=0, timeout=BROKER_TIMEOUT_SECONDS)
            client = connection.default_channel.client
            return client.llen(queue), client.hlen(UNACKED_KEY), None
        finally:
            connection.release()
    except Exception as exc:  # noqa: BLE001 - an unreadable broker is a datum
        logger.warning("ops metrics: could not read broker depth", exc_info=True)
        return None, None, f"{type(exc).__name__}: {str(exc)[:120]}"


def _scans(db: Session, recent: datetime) -> ScanMetrics:
    runs = db.execute(
        select(
            func.count(RecruiterScanRun.id),
            func.sum(RecruiterScanRun.detected),
        ).where(RecruiterScanRun.created_at >= recent)
    ).one()
    return ScanMetrics(
        last_scan_at=db.scalar(select(func.max(GmailAccount.last_scan_at))),
        runs_last_hour=runs[0] or 0,
        failed_runs_last_hour=_count(
            db, RecruiterScanRun, RecruiterScanRun.created_at >= recent,
            RecruiterScanRun.error.is_not(None),
        ),
        detected_last_hour=int(runs[1] or 0),
        mailboxes_connected=_count(db, GmailAccount, GmailAccount.status == "connected"),
        mailboxes_revoked=_count(db, GmailAccount, GmailAccount.status == "revoked"),
    )


def _is_stale(last: datetime | None, now: datetime) -> bool:
    """Whether *last* is further back than the recency window.

    Normalises a naive timestamp to UTC first. SQLite hands back naive
    datetimes for a ``DateTime(timezone=True)`` column where Postgres hands
    back aware ones, and comparing the two raises — so the check that watches
    for a dead scheduler would itself be the thing that raised, on the one
    backend the tests run against.
    """
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last) > _RECENT


def _warnings(
    mail: MailMetrics,
    queue: QueueMetrics,
    scans: ScanMetrics,
    stalled: int,
    broker_error: str | None,
    now: datetime,
) -> list[str]:
    """The reading, not just the numbers.

    A snapshot of eighteen integers still requires the reader to know which
    combinations are bad, and the person looking at this at 3am is precisely
    the person who does not. Each line below corresponds to a failure this
    deployment has actually had.

    Deliberately conservative: every condition here is one that should be rare
    on a working deployment, because a warnings list that is never empty is one
    nobody reads.
    """
    out: list[str] = []
    if broker_error:
        out.append(f"broker unreadable: {broker_error}")
    if mail.queued and not mail.sent_today:
        out.append(
            f"{mail.queued} message(s) queued and none sent today - "
            "check mailbox grants, the reputation gate, and worker liveness"
        )
    if scans.mailboxes_connected and scans.last_scan_at is None:
        out.append("mailboxes are connected but none has ever been scanned")
    elif scans.mailboxes_connected and _is_stale(scans.last_scan_at, now):
        # Judged on ``last_scan_at`` rather than on the count of scan-run rows,
        # because the two can disagree and only one of them is the authority.
        # ``last_scan_at`` is written by the scan itself; a run row is written
        # by one of the scan paths. Warning on an empty row count therefore
        # reported "beat is dead" for a deployment whose mailboxes had just
        # been read — an alert that fires when nothing is wrong is one that
        # gets ignored when something is.
        out.append(
            f"no inbound scan since {scans.last_scan_at:%Y-%m-%d %H:%M} UTC - "
            "is celery beat running?"
        )
    if scans.mailboxes_revoked:
        out.append(
            f"{scans.mailboxes_revoked} mailbox grant(s) revoked - "
            "users must reconnect before their mail is read or sent"
        )
    if stalled:
        out.append(
            f"{stalled} campaign(s) still ACTIVE more than "
            f"{_STALLED_AFTER.days} days after starting - the run may have died "
            "mid-batch; check the worker and the dead-letter table"
        )
    if queue.dead_letters_new:
        out.append(f"{queue.dead_letters_new} unreviewed dead-letter job(s)")
    if mail.hard_bounces_today:
        out.append(f"{mail.hard_bounces_today} hard bounce(s) today")
    return out
