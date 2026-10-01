"""Reading the database for the states the schema cannot forbid.

Every foreign key in this schema carries an ``ondelete``, no ``SET NULL`` points
at a ``NOT NULL`` column, and every user-scoped table cascades to ``users``.
:mod:`tests.test_data_integrity` asserts all three, so this module is
deliberately *not* about any of them — a check that can be made structural
should be structural, and a runtime audit for it is a slower way of learning
what a constraint already guarantees.

What is left is the set of invariants a relational schema cannot express, and
this codebase has three kinds:

**Cross-tenant references.** ``applications`` carries ``user_id`` *and*
``recruiter_id``, and nothing forces those two to agree — a foreign key can say
"this recruiter exists", never "this recruiter is yours". Every route checks
ownership by hand, which means every route is one forgotten check away from an
application filed under one account against another account's contact. That is
a data leak, not a tidiness problem: the tracker joins through the FK and would
render the other tenant's recruiter name and company.

**Decisions that did not reach the queue.** A user setting a contact aside
(``recruiters.excluded_at``) stops *future* mail; the send task refuses queued
mail for an excluded contact at dispatch. Both of those are true and neither
removes the row, so a queue holding messages that will never send is a normal,
invisible, and slightly dishonest state — the tracker shows them as pending.
The recruiter inbox has the mirror image: its row records that a reply was
handed to the sender and, until recently, never learned what the sender did
with it, so a message written off at the transport still counted as answered.

**Rows whose owner is gone.** Every other link in this schema is a foreign key,
and Postgres will not let one dangle — so orphan detection is not a thing this
module needs to do table by table. There is exactly one exception, and it is
deliberate: ``dead_letter_jobs.user_id`` is a plain integer with no constraint,
because the row exists to record that something failed and a constraint that
could *reject* the record defeats the point. Erasure sweeps that table by hand
(:func:`account_deletion.sweep_uncascaded`), which closes the ordinary case and
leaves the race: a task that fails after the sweep writes a row naming an
account that no longer exists. ``tests/test_index_coverage.py`` is what ties
the exemption to the check below, so the one unenforced link in the schema
cannot quietly become an unwatched one.

**Counters that drifted.** ``gmail_accounts.sent_total`` drives the
bounce-rate guardrail that pauses a mailbox. It is incremented by
``record_send`` and never recomputed, so a restore, a replayed dead letter or a
bug leaves the denominator of a reputation calculation wrong in a direction
nothing else notices.

Every check is a bounded read and returns rows, not booleans: an audit that
answers "yes, there is a problem" without saying which rows is an audit somebody
has to redo by hand. Nothing here writes. Repairing is a separate decision from
noticing, and the repair for two of these three is a product question rather
than a query.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.application import Application
from app.models.campaign import Campaign
from app.models.dead_letter import DeadLetterJob
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.fit_score import FitScore
from app.models.gmail_account import GmailAccount
from app.models.job import JobPosting
from app.models.recruiter import Recruiter
from app.models.recruiter_email import RecruiterEmail, RecruiterEmailStatus
from app.models.tailored_resume import TailoredResume
from app.models.user import User

logger = logging.getLogger(__name__)

#: Most rows any one finding will name. An audit that returns fifty thousand
#: rows is an audit nobody runs twice; the count is always exact, and the
#: sample is what makes it actionable.
SAMPLE = 20


@dataclass
class Finding:
    """One kind of problem, how many there are, and enough rows to go look."""

    check: str
    #: ``warning`` for a state that is wrong but harmless to leave;
    #: ``error`` for one that leaks data or makes a safety number wrong.
    severity: str
    summary: str
    count: int
    sample: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.count == 0


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def clean(self) -> bool:
        return all(f.ok for f in self.findings)

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok]


def _finding(
    check: str, severity: str, summary: str, rows: list[dict], count: int
) -> Finding:
    return Finding(
        check=check, severity=severity, summary=summary, count=count, sample=rows[:SAMPLE]
    )


# --------------------------------------------------------------------------
# Cross-tenant references.
#
# Each of these is the same shape: a table carrying its own ``user_id`` beside a
# foreign key to another user-scoped table, where the two are supposed to name
# the same account and only application code says so.
# --------------------------------------------------------------------------

#: ``(label, child model, fk attribute, parent model)``. Adding a row here is
#: how a new cross-tenant reference gets audited; the check itself is generic.
_TENANT_LINKS = (
    ("application.recruiter", Application, Application.recruiter_id, Recruiter),
    ("application.campaign", Application, Application.campaign_id, Campaign),
    ("fit_score.job_posting", FitScore, FitScore.job_posting_id, JobPosting),
    ("tailored_resume.job_posting", TailoredResume, TailoredResume.job_posting_id, JobPosting),
)


def cross_tenant_references(db: Session, *, user_id: int | None = None) -> list[Finding]:
    """Rows pointing at another account's data.

    ``user_id`` narrows the audit to one account, which is what an operator
    debugging a single tenant wants; without it this is the deployment-wide
    sweep.

    Note which side is scoped: the filter is on the *child*'s ``user_id``,
    because the question is "does this account hold a row pointing somewhere it
    should not", not "is this account's data pointed at". Both are worth
    knowing; only the first is this account's problem.
    """
    findings = []
    for label, child, fk, parent in _TENANT_LINKS:
        stmt = (
            select(child.id, child.user_id, fk, parent.user_id.label("parent_user_id"))
            .join(parent, fk == parent.id)
            .where(child.user_id != parent.user_id)
        )
        if user_id is not None:
            stmt = stmt.where(child.user_id == user_id)

        rows = db.execute(stmt.limit(SAMPLE + 1)).all()
        count = (
            db.scalar(
                select(func.count())
                .select_from(child)
                .join(parent, fk == parent.id)
                .where(
                    child.user_id != parent.user_id,
                    *( [child.user_id == user_id] if user_id is not None else [] ),
                )
            )
            or 0
        )
        findings.append(
            _finding(
                f"cross_tenant:{label}",
                "error",
                f"{child.__name__} rows whose {label.split('.')[1]} belongs to a "
                "different account",
                [
                    {
                        "id": r[0],
                        "user_id": r[1],
                        "references": r[2],
                        "owned_by": r[3],
                    }
                    for r in rows
                ],
                count,
            )
        )
    return findings


# --------------------------------------------------------------------------
# Decisions that did not reach the queue.
# --------------------------------------------------------------------------


def stranded_by_exclusion(db: Session, *, user_id: int | None = None) -> Finding:
    """Mail still queued to a contact the user has set aside.

    The send task refuses these at dispatch, so nothing wrong is sent — but the
    row stays ``QUEUED`` forever and the tracker counts it as pending work. The
    honest number of things about to happen is what this restores.
    """
    filters = [
        Email.direction == EmailDirection.SENT,
        Email.status == EmailStatus.QUEUED,
        Recruiter.excluded_at.is_not(None),
    ]
    if user_id is not None:
        filters.append(Application.user_id == user_id)

    base = (
        select(Email.id, Application.user_id, Recruiter.id.label("recruiter_id"), Recruiter.email)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .join(Recruiter, Application.recruiter_id == Recruiter.id)
        .where(*filters)
    )
    rows = db.execute(base.limit(SAMPLE + 1)).all()
    count = (
        db.scalar(
            select(func.count())
            .select_from(Email)
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .join(Application, EmailThread.application_id == Application.id)
            .join(Recruiter, Application.recruiter_id == Recruiter.id)
            .where(*filters)
        )
        or 0
    )
    return _finding(
        "stranded:excluded_recruiter",
        "warning",
        "Queued mail addressed to a contact the user has excluded. It will never "
        "send, and the tracker counts it as pending.",
        [
            {"email_id": r[0], "user_id": r[1], "recruiter_id": r[2], "to": r[3]}
            for r in rows
        ],
        count,
    )


def stranded_by_opt_out(db: Session, *, user_id: int | None = None) -> Finding:
    """The same shape, for the recipient's own decision rather than the user's.

    Kept as a separate finding rather than folded in with exclusion, because the
    two are not the same fact and must never be reported as one: an opt-out is a
    legal obligation and an exclusion is a preference. An operator reading this
    report needs to be able to tell them apart at a glance.
    """
    filters = [
        Email.direction == EmailDirection.SENT,
        Email.status == EmailStatus.QUEUED,
        Recruiter.opted_out.is_(True),
    ]
    if user_id is not None:
        filters.append(Application.user_id == user_id)

    joined = (
        select(Email.id, Application.user_id, Recruiter.email)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .join(Recruiter, Application.recruiter_id == Recruiter.id)
        .where(*filters)
    )
    rows = db.execute(joined.limit(SAMPLE + 1)).all()
    count = (
        db.scalar(
            select(func.count())
            .select_from(Email)
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .join(Application, EmailThread.application_id == Application.id)
            .join(Recruiter, Application.recruiter_id == Recruiter.id)
            .where(*filters)
        )
        or 0
    )
    return _finding(
        "stranded:opted_out_recruiter",
        "error",
        "Queued mail addressed to a contact who opted out. The send gate refuses "
        "it, but a row that exists is a row something could learn to send.",
        [{"email_id": r[0], "user_id": r[1], "to": r[2]} for r in rows],
        count,
    )


def replies_queued_that_never_went(
    db: Session, *, user_id: int | None = None
) -> Finding:
    """Recruiter-inbox rows still saying ``REPLY_QUEUED`` for mail that did not go.

    ``recruiter_inbox._REPLIED`` counts ``REPLY_QUEUED`` as *replied* — it feeds
    the "Replied" chip, the "Replied" filter and the auto-reply tally — and for
    the whole life of that pipeline nothing ever moved a row out of it:
    ``RecruiterEmailStatus.REPLIED`` is read in four places and assigned in
    none. So a reply the transport refused, or parked back into the review
    queue, left the row reading answered.

    The transport writes the outcome back now
    (:func:`app.services.recruiter_reply_service.record_send_failure`), which
    fixes it going forward and does nothing for the rows already on disk. This
    is how many of those there are and which ones — a repair is a separate
    decision, as it is for everything else in this module.

    ``FAILED`` and ``DRAFT`` are the two states that contradict the row.
    ``QUEUED`` does not: mail genuinely waiting on the throttled sender is the
    ordinary case and is exactly what ``REPLY_QUEUED`` means.
    """
    filters = [
        RecruiterEmail.status == RecruiterEmailStatus.REPLY_QUEUED,
        Email.status.in_((EmailStatus.FAILED, EmailStatus.DRAFT)),
    ]
    if user_id is not None:
        filters.append(RecruiterEmail.user_id == user_id)

    joined = (
        select(
            RecruiterEmail.id,
            RecruiterEmail.user_id,
            Email.id.label("email_id"),
            Email.status,
            RecruiterEmail.from_address,
        )
        .join(Email, RecruiterEmail.reply_email_id == Email.id)
        .where(*filters)
    )
    rows = db.execute(joined.limit(SAMPLE + 1)).all()
    count = (
        db.scalar(
            select(func.count())
            .select_from(RecruiterEmail)
            .join(Email, RecruiterEmail.reply_email_id == Email.id)
            .where(*filters)
        )
        or 0
    )
    return _finding(
        "stranded:reply_queued_never_sent",
        "warning",
        "Recruiter messages counted as replied whose reply was written off or "
        "parked. The user is told the agent answered them, so nobody goes and "
        "answers them by hand.",
        [
            {
                "recruiter_email_id": r[0],
                "user_id": r[1],
                "email_id": r[2],
                "email_status": r[3].value if r[3] else None,
                "from": r[4],
            }
            for r in rows
        ],
        count,
    )


def drafts_whose_reply_already_went(
    db: Session, *, user_id: int | None = None
) -> Finding:
    """Recruiter-inbox rows still saying ``DRAFTED`` for a reply that was sent.

    The mirror of :func:`replies_queued_that_never_went`, and the worse of the
    two. ``DRAFTED`` is the ordinary route for an inbound reply — the ``Email``
    waits in ``/review`` and the user approves it — and ``review.approve``
    queues the mail without touching the recruiter row. Nothing on the way out
    touched it either, so every reply a user ever approved by hand left the row
    saying a draft was still waiting.

    Three readers are wrong as a result, and only the first two are cosmetic:
    the "Drafts" chip counts it forever against a review queue with nothing in
    it, and "Replied" undercounts by the same amount. The third is why this is
    an ``error``. ``recruiter_follow_up.ENGAGED_STATUSES`` deliberately excludes
    ``DRAFTED`` — a draft nobody approved is not a conversation — so a row stuck
    there is invisible to ``find_prior_engagement``, the check that stops a
    recruiter's second message from being auto-replied to. Platform senders
    (Gem, Loxo, Bullhorn) start a fresh thread every time, which is exactly the
    case that check exists for.

    The transport writes the outcome back now
    (:func:`app.services.recruiter_reply_service.record_send_success`). That
    fixes it going forward and does nothing for the rows already on disk, which
    is what this counts.

    Only ``SENT`` contradicts the row. ``DRAFT`` is what ``DRAFTED`` means, and
    ``QUEUED`` is an approved draft on its way — neither is a disagreement.
    """
    filters = [
        RecruiterEmail.status == RecruiterEmailStatus.DRAFTED,
        Email.status == EmailStatus.SENT,
    ]
    if user_id is not None:
        filters.append(RecruiterEmail.user_id == user_id)

    joined = (
        select(
            RecruiterEmail.id,
            RecruiterEmail.user_id,
            Email.id.label("email_id"),
            Email.sent_at,
            RecruiterEmail.from_address,
        )
        .join(Email, RecruiterEmail.reply_email_id == Email.id)
        .where(*filters)
    )
    rows = db.execute(joined.limit(SAMPLE + 1)).all()
    count = (
        db.scalar(
            select(func.count())
            .select_from(RecruiterEmail)
            .join(Email, RecruiterEmail.reply_email_id == Email.id)
            .where(*filters)
        )
        or 0
    )
    return _finding(
        "stranded:drafted_reply_already_sent",
        "error",
        "Recruiter messages counted as awaiting a draft whose reply was "
        "delivered. The follow-up check reads these as never answered, so the "
        "sender's next message can be auto-replied to a second time.",
        [
            {
                "recruiter_email_id": r[0],
                "user_id": r[1],
                "email_id": r[2],
                "sent_at": r[3].isoformat() if r[3] else None,
                "from": r[4],
            }
            for r in rows
        ],
        count,
    )


# --------------------------------------------------------------------------
# Counters that drifted.
# --------------------------------------------------------------------------

#: How far ``sent_total`` may sit above the delivered mail this deployment can
#: still see before it is worth reporting. Not zero: mail is pruned, mailboxes
#: are re-added, and a mailbox that legitimately sent before this deployment
#: kept the rows would otherwise be reported forever.
COUNTER_TOLERANCE = 5


def drifted_send_counters(db: Session, *, user_id: int | None = None) -> Finding:
    """Mailboxes whose ``sent_total`` is below the mail that address really sent.

    ``sent_total`` is the denominator of :func:`reputation_service.bounce_rate`,
    which is what pauses a mailbox. Understated, the guardrail trips early and
    stops a healthy mailbox from sending; the failure is silent in both
    directions, which is why it is worth a report.

    A reconciler for this already exists —
    ``email_tasks.reconcile_warmup_ramps`` runs six-hourly and calls
    ``adopt_send_history``, which raises ``sent_total`` to the observed count.
    So this check is not a substitute for it: **a non-zero answer here means
    that reconciler is not doing its job.** That is a thing worth being able to
    ask, because this codebase has already had one reconciler that ran
    faithfully and undid the wrong column.

    The count comes from :func:`gmail_accounts.observed_send_history` rather
    than from a query written here, deliberately. A second definition of "what
    this address has sent" would eventually disagree with the reconciler's, and
    then the audit would be reporting drift between two audits.

    Only understatement is reported. ``sent_total`` above the visible mail is
    the ordinary state of any mailbox older than the rows in ``emails``, and
    reporting it would make this check noise on every healthy deployment.
    """
    from app.services import gmail_accounts

    stmt = select(GmailAccount)
    if user_id is not None:
        stmt = stmt.where(GmailAccount.user_id == user_id)

    rows = []
    for account in db.scalars(stmt):
        _, observed = gmail_accounts.observed_send_history(
            db, account.user_id, account.email
        )
        if (account.sent_total or 0) + COUNTER_TOLERANCE >= observed:
            continue
        rows.append(
            {
                "gmail_account_id": account.id,
                "user_id": account.user_id,
                "email": account.email,
                "sent_total": account.sent_total or 0,
                "on_record": observed,
            }
        )

    return _finding(
        "drift:gmail_sent_total",
        "warning",
        "Mailboxes whose sent_total is below what the address actually sent. It "
        "is the denominator of the bounce-rate guardrail, so an understated one "
        "trips the pause early — and reconcile_warmup_ramps should have fixed it.",
        rows,
        len(rows),
    )


# --------------------------------------------------------------------------
# Rows whose owner is gone.
# --------------------------------------------------------------------------


def orphaned_dead_letters(db: Session, *, user_id: int | None = None) -> Finding:
    """Dead-letter rows naming a user who no longer exists.

    The only orphan this schema can produce. Every other child column is a
    foreign key and Postgres refuses to strand it; ``dead_letter_jobs.user_id``
    is an unconstrained integer on purpose, so that recording a failure can
    never itself fail (see :mod:`app.models.dead_letter`).

    Erasure deletes this user's rows before the account goes, so the ordinary
    path leaves nothing. What remains is the race the sweep cannot close: a
    Celery task that was already running when the account was deleted, fails
    afterwards, and writes a row naming an id that has stopped meaning anybody.
    Nothing will ever delete it — the sweep only runs during a deletion, and
    that deletion has happened.

    Which makes this a retention finding rather than a tidiness one. A dead
    letter carries the task's arguments, and those routinely name an address, a
    recruiter or a message; a row keyed to a departed account is exactly the
    personal data that account asked to have removed. So the severity is
    ``error``, and the sample carries the ids needed to delete them.

    ``user_id`` scoping is honoured for symmetry with the other checks, but a
    caller asking about one live user cannot see an orphan by definition — the
    audit that finds these is the deployment-wide one.
    """
    stmt = (
        select(
            DeadLetterJob.id,
            DeadLetterJob.user_id,
            DeadLetterJob.task_name,
            DeadLetterJob.created_at,
        )
        .where(
            DeadLetterJob.user_id.is_not(None),
            ~select(User.id)
            .where(User.id == DeadLetterJob.user_id)
            .exists(),
        )
        .order_by(DeadLetterJob.id)
    )
    if user_id is not None:
        stmt = stmt.where(DeadLetterJob.user_id == user_id)

    rows = [
        {
            "dead_letter_job_id": row.id,
            "user_id": row.user_id,
            "task_name": row.task_name,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in db.execute(stmt)
    ]
    return _finding(
        "orphan:dead_letter_user",
        "error",
        "Dead-letter rows keyed to a deleted account. Nothing else will remove "
        "them — the erasure sweep only runs during a deletion that has already "
        "happened — and the stored task arguments are the personal data that "
        "deletion was meant to destroy.",
        rows,
        len(rows),
    )


# --------------------------------------------------------------------------
# The whole audit.
# --------------------------------------------------------------------------


def audit(db: Session, *, user_id: int | None = None) -> AuditReport:
    """Run every check. Reads only; never repairs.

    A check that raises is reported as a finding rather than ending the audit:
    the point of running this is to learn everything that is wrong at once, and
    a schema old enough to break one query is exactly the deployment whose other
    answers are most worth having.
    """
    report = AuditReport()
    checks = (
        ("cross_tenant", cross_tenant_references),
        ("stranded:excluded_recruiter", stranded_by_exclusion),
        ("stranded:opted_out_recruiter", stranded_by_opt_out),
        ("stranded:reply_queued_never_sent", replies_queued_that_never_went),
        ("stranded:drafted_reply_already_sent", drafts_whose_reply_already_went),
        ("orphan:dead_letter_user", orphaned_dead_letters),
        ("drift:gmail_sent_total", drifted_send_counters),
    )
    for name, check in checks:
        try:
            result = check(db, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 - one bad query must not end the audit
            logger.exception("integrity check %s failed", name)
            db.rollback()
            report.findings.append(
                Finding(
                    check=name,
                    severity="error",
                    summary=f"This check could not run: {exc}",
                    count=-1,
                )
            )
            continue
        if isinstance(result, list):
            report.findings.extend(result)
        else:
            report.findings.append(result)
    return report


def user_count(db: Session) -> int:
    """How many accounts the sweep covered, for the report's header."""
    return int(db.scalar(select(func.count(User.id))) or 0)
