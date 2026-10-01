"""Administrator API — the deployment's own state, as endpoints.

Rotating the Google OAuth client used to mean SSH onto the box, edit
``/opt/TalentPing/.env``, restart three units. Finding out what the workers had
been failing at meant reading three journals. Both are this router:

    GET    /admin/credentials         — every managed key, masked
    PUT    /admin/credentials/{key}   — set an override
    DELETE /admin/credentials/{key}   — drop it, restoring the .env value
    GET    /admin/users               — accounts, for granting the role
    PATCH  /admin/users/{id}/role     — promote or demote
    GET    /admin/ops                 — is the deployment working right now
    GET    /admin/usage               — which features people actually use
    GET    /admin/dead-letters        — tasks that failed for the last time
    GET    /admin/dead-letters/stats  — counts, for a badge
    GET    /admin/dead-letters/{id}   — one failure, with its traceback
    POST   /admin/dead-letters/{id}/replay — run it again
    POST   /admin/dead-letters/{id}/ignore — close it without running it

Four properties hold the credential half together, and each is here because its
absence would be a hole rather than an inconvenience:

- **Administrators only.** These credentials identify the *deployment* to Google
  and to the LLM providers, not one user to us. A user who could write
  ``google_client_id`` could point the consent flow at a client they control and
  collect every other user's mailbox grant; one who could read the provider keys
  would be reading a shared bill. Every handler takes ``get_current_admin``
  rather than relying on the mount, so the guard is visible at the call site.
- **Secrets are never returned.** Not on read, not in the response to the write
  that set them, not in a log line. What comes back is a mask plus provenance
  (``database`` / ``environment`` / ``unset``) — which is what an operator
  actually needs: *which* project is live, and whether it came from the UI or
  the deploy. Client ids and the redirect URI come back whole, because they are
  in the consent URL every user already sees and hiding them would defeat the
  purpose of the screen.
- **The key must be in the registry.** ``MANAGED_CREDENTIALS`` is an allowlist.
  Without it this is a write primitive over the whole ``Settings`` object, which
  also holds ``jwt_secret`` and ``token_encryption_key``.
- **An admin cannot demote themselves.** Not paternalism: it is the only rule
  that keeps a deployment from having zero administrators and therefore no way
  back into this screen short of SQL on production.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_admin
from app.core.pagination import MAX_DB_INT, Page, page_params, paginate
from app.core.pii import mask_email
from app.core.sql_text import SEARCH_MAX_LENGTH, search_clause
from app.models.app_credential import AppCredential
from app.models.dead_letter import (
    STATUS_IGNORED,
    STATUS_NEW,
    STATUS_REPLAYED,
    DeadLetterJob,
)
from app.models.user import ROLE_ADMIN, ROLES, User
from app.schemas.usage import FeatureUsageOut, PeriodUsageOut, UsageReportOut
from app.services import credential_store, integrity, ops_metrics, usage_events
from app.services.credential_store import BY_KEY, MANAGED_CREDENTIALS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


class CredentialOut(BaseModel):
    key: str
    label: str
    category: str
    is_secret: bool
    help: str
    #: "database" (an override is set), "environment" (the deployed value is in
    #: use), or "unset" (neither — whatever depends on it is dark).
    source: str
    #: Masked for secrets, whole for non-secrets, "" when unset.
    value: str
    has_env_fallback: bool
    updated_at: datetime | None = None
    updated_by: str | None = None


class CredentialListOut(BaseModel):
    credentials: list[CredentialOut]
    #: When the worker that answered this request last applied database values.
    #: Overrides reach other workers on their next refresh, so a stale timestamp
    #: here is the honest answer to "has my change taken effect everywhere yet?".
    applied_at: datetime | None = None


class CredentialIn(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


class AdminUserOut(BaseModel):
    id: int
    email: str
    full_name: str | None = None
    role: str
    is_active: bool
    is_admin: bool

    model_config = {"from_attributes": True}


class RoleIn(BaseModel):
    role: str


class MailMetricsOut(BaseModel):
    sent_today: int
    failed_today: int
    replies_today: int
    hard_bounces_today: int
    soft_bounces_today: int
    queued: int
    needs_review: int


class QueueMetricsOut(BaseModel):
    #: ``None`` means the broker could not be read, which is emphatically not
    #: the same as an empty queue — see :mod:`app.services.ops_metrics`.
    broker_depth: int | None = None
    broker_unacked: int | None = None
    broker_error: str | None = None
    dead_letters_new: int
    follow_ups_due: int


class ScanMetricsOut(BaseModel):
    last_scan_at: datetime | None = None
    runs_last_hour: int
    failed_runs_last_hour: int
    detected_last_hour: int
    mailboxes_connected: int
    mailboxes_revoked: int


class OpsSnapshotOut(BaseModel):
    generated_at: datetime
    mail: MailMetricsOut
    campaigns: dict[str, int]
    #: ACTIVE campaigns old enough that the run behind them has probably died.
    #: Counted separately from ``campaigns`` because it is not a status — these
    #: rows are also counted under ``active`` there.
    campaigns_stalled: int = 0
    queue: QueueMetricsOut
    scans: ScanMetricsOut
    #: Plain sentences naming the conditions that look wrong, so the reader does
    #: not have to know which combinations of the numbers above are bad.
    warnings: list[str] = Field(default_factory=list)


class IntegrityFindingOut(BaseModel):
    check: str
    severity: str
    summary: str
    #: ``-1`` when the check itself could not run. Distinct from ``0``, which
    #: means it ran and found nothing — an audit that reported a broken query as
    #: "clean" would be worse than not having the audit.
    count: int
    sample: list[dict] = Field(default_factory=list)


class IntegrityReportOut(BaseModel):
    checked_at: datetime
    users: int
    clean: bool
    findings: list[IntegrityFindingOut] = Field(default_factory=list)


def _describe(
    cred, rows: dict[str, AppCredential], overrides: dict[str, str]
) -> CredentialOut:
    row = rows.get(cred.key)
    env = credential_store.env_value(cred.key)
    stored = overrides.get(cred.key, "")

    if stored:
        source, effective = "database", stored
    elif env:
        source, effective = "environment", env
    else:
        source, effective = "unset", ""

    return CredentialOut(
        key=cred.key,
        label=cred.label,
        category=cred.category,
        is_secret=cred.secret,
        help=cred.help,
        source=source,
        value=credential_store.mask(effective) if cred.secret else effective,
        has_env_fallback=bool(env),
        updated_at=row.updated_at if row else None,
        updated_by=row.updated_by if row else None,
    )


def _rows_by_key(db: Session) -> dict[str, AppCredential]:
    return {r.key: r for r in db.scalars(select(AppCredential)).all()}


def _snapshot(db: Session) -> list[CredentialOut]:
    overrides = credential_store.load_overrides(db)
    rows = _rows_by_key(db)
    return [_describe(c, rows, overrides) for c in MANAGED_CREDENTIALS]


@router.get("/credentials", response_model=CredentialListOut)
def list_credentials(
    _admin: User = Depends(get_current_admin), db: Session = Depends(get_db)
) -> CredentialListOut:
    """Every managed credential, with where its current value came from."""
    return CredentialListOut(
        credentials=_snapshot(db), applied_at=credential_store.last_applied()
    )


@router.put("/credentials/{key}", response_model=CredentialOut)
def set_credential(
    key: str,
    payload: CredentialIn,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> CredentialOut:
    """Store an override for *key* and apply it to this worker immediately."""
    if key not in BY_KEY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown credential '{key}'",
        )
    try:
        credential_store.set_credential(db, key, payload.value, updated_by=admin.email)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    overrides = credential_store.load_overrides(db)
    return _describe(BY_KEY[key], _rows_by_key(db), overrides)


@router.delete("/credentials/{key}", response_model=CredentialOut)
def clear_credential(
    key: str,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> CredentialOut:
    """Drop the override, restoring whatever the environment deployed.

    Idempotent: clearing a key that has no override is a success, because the
    caller's intent — "the environment value should be in force" — is already
    true, and a 404 here would read as "no such credential".
    """
    if key not in BY_KEY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown credential '{key}'",
        )
    credential_store.clear_credential(db, key, cleared_by=admin.email)

    overrides = credential_store.load_overrides(db)
    return _describe(BY_KEY[key], _rows_by_key(db), overrides)


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    response: Response,
    _admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
    q: str | None = Query(
        default=None, max_length=SEARCH_MAX_LENGTH, description="Match email or name"
    ),
    page: Page = Depends(page_params),
) -> list[User]:
    """Accounts, so the admin role can be granted from the product.

    Without this the only ways to make a second administrator are an environment
    variable and a redeploy, or SQL on production. Both are the loop this whole
    feature exists to close.

    Bounded like every other list here. This one is the signup table, so its
    length is a function of how well the product is doing — the row count only
    ever goes up, nobody deletes from it, and it was the list most certain to
    outgrow a single response. ``X-Total-Count`` reports how many exist.

    Which makes the search the load-bearing half of this change rather than a
    nicety: the whole point of the screen is to find one known person and
    promote them, and paging alone would have turned that into clicking through
    pages of strangers. Matched through ``search_needle`` so an address
    containing ``_`` — which is most of them — is matched as text rather than as
    a single-character wildcard.
    """
    stmt = select(User)
    if (clause := search_clause(q, User.email, User.full_name)) is not None:
        stmt = stmt.where(clause)
    return paginate(db, stmt.order_by(User.id), page, response)


@router.patch("/users/{user_id}/role", response_model=AdminUserOut)
def set_role(
    user_id: int,
    payload: RoleIn,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> User:
    """Promote or demote one account."""
    if payload.role not in ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"role must be one of {', '.join(ROLES)}",
        )
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    if user.id == admin.id and payload.role != ROLE_ADMIN:
        # The lockout guard. One administrator demoting themselves is the single
        # move that can leave a deployment with nobody able to reach this screen
        # — and it is an easy misclick on a list where your own row looks like
        # everyone else's. Demoting *another* admin is allowed, because the
        # caller necessarily remains one.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "You cannot remove your own administrator access. Ask another "
                "administrator to do it."
            ),
        )

    user.role = payload.role
    db.commit()
    db.refresh(user)
    logger.info(
        "Role for %s set to %s by %s",
        mask_email(user.email),
        payload.role,
        mask_email(admin.email),
    )
    return user


# ---------------------------------------------------------------------------
# Dead letters
#
# `task_ignore_result=True` is set globally, so a task that exhausts its retries
# writes a row in `dead_letter_jobs` and nothing else (see
# `app.tasks.dead_letter`). That capture is only half of it: a table nobody can
# read is a log file with extra steps, and the reason the table exists is to
# make the work runnable again once the cause is fixed.
#
# Administrators only, and for a sharper reason than the credential screen
# above. A replay dispatches a task *by name with stored arguments*, on behalf
# of whichever user the failure belonged to — `send_campaign_email` replayed is
# mail leaving the deployment. The endpoint is therefore built as though it were
# a send button, because it is one.
# ---------------------------------------------------------------------------


class DeadLetterOut(BaseModel):
    """One failure, as the list renders it.

    Deliberately without `traceback`, `args_json` and `kwargs_json`: a page is
    up to 500 rows and each of those columns is capped at 4000 characters, so
    carrying them here turns a list request into several megabytes to render a
    table of task names. `GET /admin/dead-letters/{id}` has them.
    """

    id: int
    task_name: str
    task_id: str | None = None
    queue: str | None = None
    user_id: int | None = None
    reason: str
    exception_type: str | None = None
    exception_message: str | None = None
    retries: int
    occurrences: int
    first_failed_at: datetime
    last_failed_at: datetime
    replayable: bool
    status: str
    replayed_at: datetime | None = None
    replayed_task_id: str | None = None
    resolved_by: str | None = None

    model_config = {"from_attributes": True}


class DeadLetterDetailOut(DeadLetterOut):
    args_json: str | None = None
    kwargs_json: str | None = None
    traceback: str | None = None


class DeadLetterStatsOut(BaseModel):
    """The numbers a badge needs, without pulling a page of rows for them."""

    #: Open failures — distinct rows, not occurrences.
    new: int
    replayed: int
    ignored: int
    #: How many times the open failures have actually happened. The gap between
    #: this and `new` is the collapse doing its job: 3 rows / 900 occurrences is
    #: one bad afternoon, 3 rows / 3 occurrences is three separate bugs.
    open_occurrences: int
    #: When the oldest open failure was first seen — "how long has this been
    #: broken", which no per-row field answers on its own.
    oldest_open_at: datetime | None = None


class DeadLetterResolveIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


def _dead_letter_or_404(db: Session, job_id: int) -> DeadLetterJob:
    row = db.get(DeadLetterJob, job_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dead letter not found"
        )
    return row


@router.get("/ops", response_model=OpsSnapshotOut)
def ops_snapshot(
    _admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> OpsSnapshotOut:
    """Is the deployment working right now, in one read.

    The counterpart to ``/health``: that answers whether the dependencies are
    reachable, this answers whether the work is happening. Both were needed to
    notice the outages this application has actually had, and a deployment can
    fail either check while passing the other — a perfectly healthy Postgres
    and Redis with beat dead is every dependency green and nothing being sent.

    Admin-only and deployment-wide. The per-user versions of these figures
    already exist in ``/dashboard`` and ``/analytics``; what was missing was a
    number nobody has to be signed in as the right user to see.

    Counts only, never rows — see :mod:`app.services.ops_metrics` for why.
    """
    return OpsSnapshotOut(**ops_metrics.snapshot(db).as_dict())


@router.get("/usage", response_model=UsageReportOut)
def usage_report(
    _admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
    days: int = Query(
        default=usage_events.DEFAULT_WINDOW_DAYS,
        ge=1,
        le=usage_events.MAX_WINDOW_DAYS,
        description="Look back N days",
    ),
    bucket: str = Query(default="day", pattern="^(day|week)$"),
) -> UsageReportOut:
    """Which features people actually use, deployment-wide.

    The counterpart to ``/admin/ops`` in the same way ``/admin/ops`` is the
    counterpart to ``/health``: that one says whether the work is happening,
    this one says whether anybody asked for it. A feature that is shipped,
    working, and used by nobody passes every other check this deployment has.

    **Counts, never rows, and never a user id.** Same rule as ``/admin/ops``,
    and here it matters more: the underlying table records what individual job
    seekers looked at, and a screen that could break it down per account would
    be a surveillance tool wearing a product-metrics label. The aggregate
    answers every question it was built for; the per-account view answers none
    of them and creates a new liability.

    ``unused`` is the column worth reading first. Every other figure ranks what
    people do, and a ranking cannot show a feature that produces no rows — the
    thing nobody has touched since it shipped is invisible in a top-N list by
    construction, which is exactly the finding this report was built to
    surface.
    """
    result = usage_events.report(db, days=days, bucket=bucket)
    return UsageReportOut(
        since=result.since,
        until=result.until,
        bucket=result.bucket,
        features=[
            FeatureUsageOut(
                name=row.name,
                area=row.area,
                label=row.label,
                events=row.events,
                users=row.users,
                repeat_users=row.repeat_users,
                retention=row.retention,
                last_used=row.last_used,
            )
            for row in result.features
        ],
        active=[
            PeriodUsageOut(period=row.period, users=row.users, events=row.events)
            for row in result.active
        ],
        active_users=result.active_users,
        unused=result.unused,
        adoption_days=usage_events.ADOPTION_DAYS,
        # Published rather than inferred from an empty `features` list: a
        # collector that is switched off and a product nobody uses produce the
        # identical empty table, and telling them apart is the entire argument
        # this feature was built on.
        collecting=settings.usage_analytics_enabled,
    )


@router.get("/integrity", response_model=IntegrityReportOut)
def integrity_report(
    _admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
    user_id: int | None = Query(
        default=None,
        ge=1,
        le=MAX_DB_INT,
        description="Audit one account instead of the deployment",
    ),
) -> IntegrityReportOut:
    """States the schema cannot forbid, listed with the rows that are in them.

    Deployment-wide by default; ``user_id`` narrows it to one account, which is
    what an operator debugging a single tenant wants.

    Reads only. Repairing is a separate decision from noticing, and for two of
    these three checks the repair is a product question rather than a query —
    see :mod:`app.services.integrity`.

    Admin-only, and not because the findings are secret: the sweep reads every
    application, every thread and every mailbox on the deployment, which is not
    something a route a tenant can reach should do on demand.
    """
    report = integrity.audit(db, user_id=user_id)
    return IntegrityReportOut(
        checked_at=report.checked_at,
        users=integrity.user_count(db),
        clean=report.clean,
        findings=[
            IntegrityFindingOut(
                check=f.check,
                severity=f.severity,
                summary=f.summary,
                count=f.count,
                sample=f.sample,
            )
            for f in report.findings
        ],
    )


@router.get("/dead-letters", response_model=list[DeadLetterOut])
def list_dead_letters(
    response: Response,
    _admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
    job_status: str | None = Query(
        default=STATUS_NEW,
        alias="status",
        max_length=SEARCH_MAX_LENGTH,
        description=f"One of {', '.join((STATUS_NEW, STATUS_REPLAYED, STATUS_IGNORED))}, or `all`",
    ),
    task_name: str | None = Query(
        default=None, max_length=SEARCH_MAX_LENGTH, description="Exact task name"
    ),
    page: Page = Depends(page_params),
) -> list[DeadLetterJob]:
    """Failed tasks, most recently failed first.

    Defaults to open failures rather than to everything, because the question
    this screen answers is "what is broken now?" — and a replayed row that never
    came back is precisely the one nobody needs to look at again.
    """
    stmt = select(DeadLetterJob)
    if job_status and job_status != "all":
        if job_status not in (STATUS_NEW, STATUS_REPLAYED, STATUS_IGNORED):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"status must be one of {STATUS_NEW}, {STATUS_REPLAYED}, "
                    f"{STATUS_IGNORED}, all"
                ),
            )
        stmt = stmt.where(DeadLetterJob.status == job_status)
    if task_name and (needle := task_name.strip()):
        stmt = stmt.where(DeadLetterJob.task_name == needle)
    return paginate(
        db, stmt.order_by(DeadLetterJob.last_failed_at.desc(), DeadLetterJob.id.desc()),
        page, response,
    )


@router.get("/dead-letters/stats", response_model=DeadLetterStatsOut)
def dead_letter_stats(
    _admin: User = Depends(get_current_admin), db: Session = Depends(get_db)
) -> DeadLetterStatsOut:
    """Counts by status, in one query rather than one per status."""
    counts = {
        row.status: (row.rows, row.occurrences)
        for row in db.execute(
            select(
                DeadLetterJob.status,
                func.count().label("rows"),
                func.coalesce(func.sum(DeadLetterJob.occurrences), 0).label(
                    "occurrences"
                ),
            ).group_by(DeadLetterJob.status)
        )
    }
    oldest = db.scalar(
        select(func.min(DeadLetterJob.first_failed_at)).where(
            DeadLetterJob.status == STATUS_NEW
        )
    )
    return DeadLetterStatsOut(
        new=counts.get(STATUS_NEW, (0, 0))[0],
        replayed=counts.get(STATUS_REPLAYED, (0, 0))[0],
        ignored=counts.get(STATUS_IGNORED, (0, 0))[0],
        open_occurrences=counts.get(STATUS_NEW, (0, 0))[1],
        oldest_open_at=oldest,
    )


@router.get("/dead-letters/{job_id}", response_model=DeadLetterDetailOut)
def get_dead_letter(
    job_id: int,
    _admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> DeadLetterJob:
    """One failure with its traceback and the arguments it ran with."""
    return _dead_letter_or_404(db, job_id)


@router.post("/dead-letters/{job_id}/replay", response_model=DeadLetterOut)
def replay_dead_letter(
    job_id: int,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> DeadLetterJob:
    """Dispatch the failed task again, with the arguments it failed on.

    Four refusals, each of which is the difference between a retry button and a
    way to hurt the deployment:

    **The task name must be one a worker registers.** Without that check this is
    `send_task(<anything>, <anything>)` over the broker, driven by a column —
    and the column is written on the failure path, from a task name this process
    did not choose. The Celery registry is the allowlist, and it is the same
    registry the worker will match the message against.

    **The row must still be open.** Replaying is not idempotent: the tasks
    behind these rows send mail and call paid APIs. A row moves `new →
    replayed` once, and the second click gets a 409 rather than a second send.
    The status is claimed *before* the dispatch for that reason — if the two
    have to disagree, "possibly ran but marked replayed" is a recoverable
    mistake and "ran twice" is not.

    The claim is a conditional `UPDATE ... WHERE status = 'new'` rather than the
    obvious read-then-assign, and that is the whole of its value. Two operators
    on the same incident — or one double-click — put two requests in flight at
    once; under `READ COMMITTED` both read `new`, both assign, both dispatch,
    and the 409 above never fires because neither transaction can see the
    other's write. Letting the database decide the winner makes the check and
    the claim one operation: the loser's `WHERE` re-evaluates against the
    committed row, matches nothing, and it gets the 409 it should have had.

    **The arguments must be the ones that failed.** `replayable` is false when
    redaction or truncation rewrote them for storage, and dispatching the
    rewritten version is a new bug rather than a retry of the old one.

    **A worker has to exist.** With `celery_enabled` off there is nothing to
    receive the message. Running the task inline instead — which is what the
    campaign routes do — is wrong here: these are the long tasks, in a request.
    """
    row = _dead_letter_or_404(db, job_id)

    if row.status != STATUS_NEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This failure was already {row.status}.",
        )
    if not row.replayable:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The stored arguments were redacted or truncated, so replaying "
                "would not re-run what failed. Dispatch it by hand instead."
            ),
        )
    if not settings.celery_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Background workers are disabled on this deployment.",
        )

    try:
        args = json.loads(row.args_json) if row.args_json else []
        kwargs = json.loads(row.kwargs_json) if row.kwargs_json else {}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The stored arguments are not readable as JSON.",
        ) from exc
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The stored arguments are not a list and a mapping.",
        )

    from app.tasks.celery_app import celery_app

    # Populate the registry the way a worker does, so the allowlist is the set
    # of tasks that can actually receive this message rather than the set whose
    # modules this API process happened to have imported.
    celery_app.loader.import_default_modules()
    if row.task_name not in celery_app.tasks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"No worker registers '{row.task_name}' — replaying it would "
                "publish a message nothing can receive."
            ),
        )

    # Claim first, and claim conditionally. See the docstring: a lost dispatch
    # is recoverable by hand, a double send is not — and the `status != new`
    # check above cannot stop a concurrent second replay on its own, because
    # both requests read the row before either writes it.
    claimed = db.execute(
        update(DeadLetterJob)
        .where(DeadLetterJob.id == row.id, DeadLetterJob.status == STATUS_NEW)
        .values(
            status=STATUS_REPLAYED,
            replayed_at=datetime.now(UTC),
            resolved_by=admin.email,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if not claimed:
        # Someone else claimed it between the check above and here.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This failure was already being replayed.",
        )

    try:
        async_result = celery_app.send_task(row.task_name, args=args, kwargs=kwargs)
    except Exception as exc:  # noqa: BLE001 - broker down or misconfigured
        row.status = STATUS_NEW
        row.replayed_at = None
        row.resolved_by = None
        db.commit()
        logger.warning("dead-letter %s: replay dispatch failed: %s", job_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reach the broker. The failure is still open.",
        ) from exc

    row.replayed_task_id = str(getattr(async_result, "id", "") or "")[:64] or None
    db.commit()
    db.refresh(row)
    logger.info(
        "dead-letter %s (%s) replayed by %s as %s",
        job_id, row.task_name, admin.email, row.replayed_task_id,
    )
    return row


@router.post("/dead-letters/{job_id}/ignore", response_model=DeadLetterOut)
def ignore_dead_letter(
    job_id: int,
    payload: DeadLetterResolveIn | None = None,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> DeadLetterJob:
    """Close a failure that needs no re-run, keeping it as a record.

    The row is not deleted. It is the audit trail of a decision, and — because
    the capture opens a *new* row rather than collapsing into a closed one — it
    is also what makes the same failure recurring afterwards visible as news.

    Conditional on the row not already being replayed, for the same reason the
    replay claim is conditional: an ignore racing a replay would otherwise
    overwrite the status of a task that has genuinely been dispatched, leaving a
    row that reads `ignored` while carrying the `replayed_task_id` of work now
    running on a worker. Ignoring an already-ignored row stays harmless — it is
    the same decision, recorded again.
    """
    row = _dead_letter_or_404(db, job_id)
    if row.status == STATUS_REPLAYED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This failure was already replayed.",
        )
    closed = db.execute(
        update(DeadLetterJob)
        .where(DeadLetterJob.id == row.id, DeadLetterJob.status != STATUS_REPLAYED)
        .values(status=STATUS_IGNORED, resolved_by=admin.email)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if not closed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This failure was already replayed.",
        )
    if payload and payload.note:
        logger.info(
            "dead-letter %s ignored by %s: %s",
            job_id,
            mask_email(admin.email),
            payload.note,
        )
    db.refresh(row)
    return row
