"""Autopilot routes — set preferences once, then let the agent run.

    GET  /autopilot        -> current preferences (created lazily, off by default)
    PUT  /autopilot        -> update any subset of the knobs
    POST /autopilot/run    -> run one cycle now (worker, or inline in dev/tests)
    GET  /autopilot/dry-run -> the next 24h, explained, with nothing sent
    GET  /autopilot/reputation -> per-mailbox warm-up / deliverability status

    GET  /autopilot/auto-send         -> is outreach sending unreviewed, and why not
    POST /autopilot/auto-send/pause   -> hold auto-send, leaving the rest running
    POST /autopilot/auto-send/resume  -> lift the hold

Pause is its own pair of endpoints rather than a field on the PUT because it is
the emergency brake: it has to be one unambiguous click that cannot be confused
with editing settings, and the UI must not have to compute a timestamp to reach
it. It deliberately does *not* touch ``is_active`` — stopping the sending while
the pipeline keeps finding and drafting work is the whole point, so that resuming
costs nothing and nobody is tempted to leave autopilot off instead.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.patching import reject_nulls
from app.core.rate_limit import rate_limit
from app.models.autopilot import AutopilotPreference
from app.models.user import User
from app.schemas.autopilot import (
    AutopilotDryRunOut,
    AutopilotOut,
    AutopilotRunResult,
    AutopilotUpdate,
    AutoSendPause,
    AutoSendStatus,
)
from app.services import (
    auto_apply_service,
    autopilot_plan,
    fit_refresh,
    profile_service,
    reputation_service,
    send_policy,
    usage_events,
)
from app.services.auto_apply_service import run_user_autopilot
from app.tasks import job_tasks

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/autopilot", tags=["autopilot"])


def _get_or_create(db: Session, user: User) -> AutopilotPreference:
    """Every user has exactly one preference row; make it on first read."""
    pref = user.autopilot
    if pref is None:
        pref = AutopilotPreference(user_id=user.id)
        db.add(pref)
        db.commit()
        db.refresh(pref)
    return pref


def _dispatch_cycle(user: User) -> bool:
    """Hand one autopilot cycle to a worker. False when there was none to hand it to.

    A cycle scans job boards, scores, tailors and crawls for recruiter contacts:
    minutes of network and model time. It never runs in the request that asks for
    it unless there is no worker at all, so the caller decides what "no broker"
    means for it — :func:`run_now` runs inline and reports the result, while
    switching autopilot on falls back to the next beat tick.
    """
    if not settings.celery_enabled:
        return False
    try:
        from app.tasks.auto_apply_tasks import run_autopilot_for_user

        run_autopilot_for_user.delay(user.id)
        return True
    except Exception as exc:  # noqa: BLE001 - broker down; the caller decides
        logger.warning("autopilot dispatch failed for user %s: %s", user.id, exc)
        return False


@router.get("", response_model=AutopilotOut)
def get_autopilot(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> AutopilotPreference:
    return _get_or_create(db, user)


@router.put("", response_model=AutopilotOut)
def update_autopilot(
    payload: AutopilotUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AutopilotPreference:
    """Update any subset of the autopilot knobs (including the on/off switch)."""
    pref = _get_or_create(db, user)

    # Turning autopilot on requires the same prerequisites as any outreach.
    turning_on = payload.is_active is True and not pref.is_active
    if turning_on and not user.gmail_connected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect a Gmail account before turning on autopilot",
        )

    edited = payload.model_dump(exclude_unset=True)
    # An omitted key means "leave it alone"; an explicit null is a value, and on
    # a required column it is one the row cannot hold. See `app.core.patching`.
    reject_nulls(AutopilotPreference, edited)
    for name, value in edited.items():
        setattr(pref, name, value)
    # First explicit save completes the wizard's preferences step.
    if pref.configured_at is None:
        pref.configured_at = datetime.now(UTC)
    # One event name for every settings surface in the product, with `scope`
    # saying which screen. A name per screen would make "how many people change
    # settings at all" a sum somebody has to remember to compute, and would grow
    # the vocabulary by one every time a preferences form is added — which is
    # the cost that stops anyone instrumenting the next one.
    usage_events.record(
        db,
        "settings.changed",
        user_id=user.id,
        scope="autopilot",
        sections=sorted(edited),
        turning_on=turning_on,
    )
    db.commit()

    # With one profile, this form *is* that profile — see
    # profile_service.sync_default_profile for why the two must not drift, and
    # why only the fields this request touched are carried across.
    profile_service.ensure_default_profile(db, user)
    synced = profile_service.sync_default_profile(db, user, pref, set(edited))

    # And when it wrote through, the intent behind every score in the feed just
    # changed. This is the *common* way targeting moves — a single-profile user
    # edits it here, on the setup wizard, not on the profile manager — so a
    # rescore wired only into `/profiles` would have missed most of the edits it
    # exists for. `_SYNCED_FIELDS` maps preference names onto profile ones, so
    # the question asked of `fit_refresh` is about the columns that were written.
    if synced is not None:
        written = profile_service.synced_profile_fields(set(edited))
        if fit_refresh.affects_scoring(written):
            job_tasks.dispatch_rescore(db, user)

    # Switching on *starts* it. Autopilot ticks hourly, which is right for an
    # account already running and wrong for the moment someone finishes setup:
    # the wizard ended, nothing happened, and the fifty minutes before the first
    # tick were indistinguishable from a product that does not work. That wait
    # sat between signing up and the first email, and it was most of it.
    #
    # Best-effort by design. A failed dispatch is not a failed switch-on — the
    # user asked for autopilot, they have it, and beat picks the run up on the
    # next tick exactly as it did before this existed.
    if turning_on:
        _dispatch_cycle(user)

    db.refresh(pref)
    return pref


@router.post(
    "/run",
    response_model=AutopilotRunResult,
    # The most expensive single call in the API. One cycle scans job boards,
    # scores every hit, tailors a resume per application and crawls for
    # recruiter contacts — minutes of model time, and it spends the free tier
    # the whole deployment shares. Both dispatch paths cost: queued, it fills
    # the worker with duplicate cycles for one user; inline, it holds the only
    # worker there is. 3 per 15 minutes is well above the hourly beat tick that
    # normally drives this, and far below what a held-down button produces.
    dependencies=[Depends(rate_limit(3, 900, scope="autopilot-run"))],
)
def run_now(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> AutopilotRunResult:
    """Run one autopilot cycle right now.

    Dispatched to a worker when a broker is configured; otherwise (dev/tests) it
    runs inline so the caller sees the result. Autopilot must be active first.
    """
    pref = _get_or_create(db, user)
    if not pref.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Turn on autopilot before running it",
        )
    # Said here as well as enforced in the service, because the two answer
    # different questions. The lease is what makes a second cycle *safe* — it
    # would otherwise re-spend the whole daily budget — but a user who pressed
    # the button twice, or pressed it while the hourly sweep was still crawling,
    # would get "queued" for a run that then silently did nothing. A guard that
    # only ever shows up as inaction reads as a broken button.
    if auto_apply_service.cycle_is_running(pref):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A run is already in progress — give it a moment to finish",
        )

    if _dispatch_cycle(user):
        return AutopilotRunResult(status="queued", queued=True)

    result = run_user_autopilot(db, user)
    return AutopilotRunResult(**result.as_dict(), queued=False)


@router.get("/dry-run", response_model=AutopilotDryRunOut)
def dry_run(
    limit: int | None = Query(default=None, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AutopilotDryRunOut:
    """What autopilot will do in the next 24 hours, and why — without doing it.

    Deliberately a GET, and safe to poll: :func:`autopilot_plan.build_plan` makes
    no network calls, writes nothing and asks no model anything, so the answer is
    stable between reads. It is also the one endpoint that does *not* require
    autopilot to be on — "show me what would happen" is precisely the question
    asked before turning it on, and the response says so in ``blocked_reason``
    rather than refusing.

    *limit* trims the list for a preview; ``budget`` still reports the whole day,
    so a shortened list never reads as a smaller day's work.
    """
    return AutopilotDryRunOut(**autopilot_plan.build_plan(db, user, limit=limit).as_dict())


@router.get("/auto-send", response_model=AutoSendStatus)
def auto_send_status(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> AutoSendStatus:
    """Whether outreach is going out unreviewed right now, and what's stopping it."""
    pref = _get_or_create(db, user)
    return AutoSendStatus(**send_policy.status(db, user, pref))


@router.post("/auto-send/pause", response_model=AutoSendStatus)
def pause_auto_send(
    payload: AutoSendPause | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AutoSendStatus:
    """Stop sending unreviewed outreach for a while. Drafting continues.

    Anything the pipeline produces during the pause lands in the review queue, so
    nothing is lost — the user can still send by hand, and when the pause lifts
    the backlog stays in the queue rather than flushing all at once. Whatever was
    already handed to the sender before the pause is *not* recalled; it may
    already be in flight, and pretending otherwise would be a worse promise than
    the one this makes.
    """
    pref = _get_or_create(db, user)
    send_policy.pause(pref, (payload or AutoSendPause()).hours)
    db.commit()
    return AutoSendStatus(**send_policy.status(db, user, pref))


@router.post("/auto-send/resume", response_model=AutoSendStatus)
def resume_auto_send(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> AutoSendStatus:
    """Lift an auto-send pause. Idempotent — resuming twice is not an error.

    Only clears the pause. A user still inside an auto-send trial, or over their
    daily ceiling, stays held by those; the response says which, rather than
    reporting success and quietly sending nothing.
    """
    pref = _get_or_create(db, user)
    send_policy.resume(pref)
    db.commit()
    return AutoSendStatus(**send_policy.status(db, user, pref))


@router.get("/reputation", response_model=list[dict])
def reputation(
    user: User = Depends(get_current_user),
) -> list[dict]:
    """Per-mailbox warm-up and deliverability status, for the UI."""
    return [
        reputation_service.status_summary(account)
        for account in user.gmail_accounts
        if account.status == "connected"
    ]
