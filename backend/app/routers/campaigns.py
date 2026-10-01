"""Campaign routes — step 3 of the wizard: press start, walk away.

    POST /campaigns              -> create and immediately launch the autopilot
    GET  /campaigns              -> list
    GET  /campaigns/{id}         -> one campaign (live progress counters)
    POST /campaigns/{id}/pause   -> stop sending
    POST /campaigns/{id}/resume  -> pick up where it stopped
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import events
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import Page, page_params, paginate
from app.core.rate_limit import rate_limit
from app.models.campaign import Campaign, CampaignStatus
from app.models.gmail_account import GmailAccount
from app.models.user import User
from app.schemas.campaign import CampaignCreate, CampaignOut, CampaignStart
from app.services import usage_events
from app.services.outreach_service import resolve_resume, run_autopilot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/campaigns", tags=["campaigns"])

# Statuses from which a campaign can be (re)started.
_RESUMABLE = {CampaignStatus.DRAFT, CampaignStatus.PAUSED, CampaignStatus.FAILED}


def _get_owned(
    db: Session, user: User, campaign_id: int, *, for_update: bool = False
) -> Campaign:
    if for_update:
        stmt = select(Campaign).where(Campaign.id == campaign_id).with_for_update()
        campaign = db.scalar(stmt)
    else:
        campaign = db.get(Campaign, campaign_id)
    if campaign is None or campaign.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found"
        )
    return campaign


def _launch(db: Session, campaign: Campaign) -> tuple[bool, str | None]:
    """Hand the campaign to a worker, or run it inline if no broker is reachable.

    Returns ``(queued, detail)``. The inline path keeps single-process
    deployments and tests working without Redis; in production, where
    ``celery_enabled`` is on and a worker is running, the task always wins.
    """
    if not settings.celery_enabled:
        _announce(db, campaign, "inline", "celery disabled")
        result = run_autopilot(db, campaign)
        db.refresh(campaign)
        return False, result.get("reason") or "; ".join(result.get("notes", [])[:3]) or None

    try:
        from app.tasks.email_tasks import start_campaign

        start_campaign.delay(campaign.id)
        _announce(db, campaign, "queued", None)
        return True, None
    except Exception as exc:  # noqa: BLE001 - broker down or not configured
        logger.warning(
            "campaign %s: broker unavailable (%s) — running autopilot inline",
            campaign.id,
            exc,
        )
        _announce(db, campaign, "inline", f"broker unavailable: {exc}")
        result = run_autopilot(db, campaign)
        db.refresh(campaign)
        detail = result.get("reason") or "; ".join(result.get("notes", [])[:3]) or None
        return False, detail


def _announce(db: Session, campaign: Campaign, dispatch: str, reason: str | None) -> None:
    """Record that a campaign started, and by which of the two routes.

    ``dispatch`` is the field worth having. The inline fallback is a correctness
    feature and an operational warning at the same time: it keeps a campaign
    working when the broker is down, and it does so by running the entire
    outreach pipeline — discovery crawls and an LLM draft per contact — inside
    one HTTP request. A deployment where that has quietly become the normal path
    is one where `celery_enabled` is wrong or Redis has been unreachable for
    days, and the only symptom is slow requests.
    """
    events.emit(
        events.CAMPAIGN_LAUNCHED,
        campaign_id=campaign.id,
        user_id=campaign.user_id,
        dispatch=dispatch,
        reason=reason,
    )
    # The usage half of the same fact. `_announce` is where both doors into a
    # launch meet, so instrumenting here rather than in the two handlers is what
    # keeps "campaigns launched" from silently counting only the ones created
    # from scratch. No commit: `_launch` is always inside a handler that is
    # about to make one.
    usage_events.record(
        db,
        "campaign.launched",
        user_id=campaign.user_id,
        campaign_id=campaign.id,
        dispatch=dispatch,
    )


@router.get("", response_model=list[CampaignOut])
def list_campaigns(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    page: Page = Depends(page_params),
) -> list[Campaign]:
    """Newest first. Bounded like every list here; see `app.core.pagination`."""
    stmt = (
        select(Campaign).where(Campaign.user_id == user.id).order_by(Campaign.id.desc())
    )
    return paginate(db, stmt, page, response)


@router.post(
    "",
    response_model=CampaignStart,
    status_code=status.HTTP_201_CREATED,
    # Creating a campaign launches it — `_launch` runs the whole outreach
    # pipeline, so this is `/autopilot/run` wearing a different name: a
    # discovery crawl per expanded target company, then an LLM-written draft per
    # contact it found. Queued that is a worker saturated by one user; with no
    # broker `run_autopilot` does all of it on the request thread.
    #
    # Deliberately looser than autopilot's 3/900. The two 409s above — no Gmail,
    # no resume — are raised *after* this dependency runs, so a new user
    # fumbling the setup spends the budget without ever reaching the pipeline,
    # and locking them out for fifteen minutes for that is a worse bug than the
    # one this prevents.
    dependencies=[Depends(rate_limit(5, 300, scope="campaign-launch"))],
)
def create_campaign(
    payload: CampaignCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CampaignStart:
    """Create a campaign and start the autopilot in one call.

    This is the only write the user makes to launch outreach: everything from
    finding contacts to spacing out the sends happens after this returns.
    """
    if not user.gmail_connected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect a Gmail account before starting outreach",
        )
    resume = resolve_resume(db, user, payload.resume_id)
    if resume is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload a resume before starting outreach",
        )

    # Refused rather than silently ignored: a campaign started "from" an address
    # that would in fact send from a different one is worse than an error, and
    # the user is looking at the picker as they read the message.
    if payload.gmail_account_id is not None:
        chosen = db.get(GmailAccount, payload.gmail_account_id)
        if chosen is None or chosen.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Gmail account not found"
            )
        if chosen.status != "connected":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{chosen.email} needs reconnecting before it can send",
            )

    targets = payload.target_companies or payload.target_industries
    campaign = Campaign(
        user_id=user.id,
        resume_id=resume.id,
        gmail_account_id=payload.gmail_account_id,
        name=payload.name or f"{resume.display_label} → {', '.join(targets[:3])}"[:255],
        target_companies=payload.target_companies,
        target_industries=payload.target_industries,
        target_roles=payload.target_roles or resume.target_roles or [],
        auto_send=payload.auto_send,
        follow_up_enabled=payload.follow_up_enabled and payload.follow_up_count > 0,
        follow_up_count=payload.follow_up_count,
        follow_up_interval_days=payload.follow_up_interval_days,
        follow_up_stop_on_reply=payload.follow_up_stop_on_reply,
        follow_up_step_days=payload.follow_up_step_days,
        # Claimed, not left DRAFT — the same claim `resume_campaign` makes, for
        # the same reason and against the same second caller.
        #
        # The lock there closes resume-against-resume. It cannot close
        # create-against-resume, because there is no row to lock until this one
        # exists: `_launch` below dispatches `start_campaign` and returns while
        # the campaign is still sitting in whatever status this constructor gave
        # it, and DRAFT is resumable. So a user who presses start and then
        # presses restart — a double-click on a wizard whose next screen is the
        # tracker, or a client retrying a slow POST — got two concurrent
        # `run_autopilot` calls over one campaign, each writing an `Application`
        # per recruiter into a table with a `(campaign_id, recruiter_id)`
        # uniqueness constraint. The loser crashed on it and the campaign came
        # back FAILED, with a raw database error as its reason, seconds after
        # being created.
        #
        # DISCOVERING costs nothing `run_autopilot` was not about to write
        # anyway, and it is the more truthful answer besides: a campaign whose
        # run is already dispatched is not a draft, and the tracker only polls
        # for statuses it considers live (`LIVE_CAMPAIGN_STATUSES`), which DRAFT
        # is not — so the first seconds of every new campaign were unwatched.
        status=CampaignStatus.DISCOVERING,
    )
    db.add(campaign)
    # Flushed rather than committed, so the usage row can carry the id the
    # database just assigned and still ride the same transaction as the
    # campaign it describes. A create that fails after this point records
    # neither the campaign nor a use of the feature, which is the honest pair.
    db.flush()
    usage_events.record(
        db,
        "campaign.created",
        user_id=user.id,
        campaign_id=campaign.id,
        auto_send=campaign.auto_send,
        follow_ups=campaign.follow_up_count if campaign.follow_up_enabled else 0,
        targeting="companies" if payload.target_companies else "industries",
    )
    db.commit()
    db.refresh(campaign)

    queued, detail = _launch(db, campaign)
    return CampaignStart(
        campaign=CampaignOut.model_validate(campaign), queued=queued, detail=detail
    )


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Campaign:
    return _get_owned(db, user, campaign_id)


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
def pause_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Campaign:
    """Stop the outreach programme. Queued mail waits for a resume.

    Cold outreach and its follow-ups only. A reply to a recruiter who has
    written back still goes out — pausing is a volume control over mail *we*
    initiate, and a warm conversation left unanswered because the campaign
    behind it was paused is not what anyone means by it.
    """
    # Locked for `resume_campaign`'s reason: this is a read-then-write against
    # the same status column, and the two routes write it in opposite
    # directions. Unlocked, a pause that read PAUSED while a resume was still
    # committing returned "paused" to the user and wrote nothing, leaving the
    # campaign running — a stop button that answered 200 and did not stop it.
    campaign = _get_owned(db, user, campaign_id, for_update=True)
    if campaign.status in (CampaignStatus.COMPLETED, CampaignStatus.PAUSED):
        return campaign
    campaign.status = CampaignStatus.PAUSED
    usage_events.record(
        db, "campaign.paused", user_id=user.id, campaign_id=campaign.id
    )
    db.commit()
    db.refresh(campaign)
    return campaign


@router.post(
    "/{campaign_id}/resume",
    response_model=CampaignStart,
    # The same budget `create_campaign` draws on, because this is the same work:
    # both end in `_launch`, and `_launch` runs the whole outreach pipeline.
    # Metered apart, the 5/300 above is not a limit at all — create one campaign,
    # then press resume in a loop for an unbounded number of discovery crawls and
    # LLM drafts. The status claim `create_campaign` now makes stops the *first*
    # press of that loop, which is the one that duplicated a live run; it does
    # not stop the loop, because pause-then-resume is a legitimate pair and each
    # round of it is a full pipeline run. The shared budget is what bounds it.
    dependencies=[Depends(rate_limit(5, 300, scope="campaign-launch"))],
)
def resume_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CampaignStart:
    """Restart a paused, failed, or never-started campaign.

    Locked, and claimed here rather than left ``DRAFT`` for the worker to
    advance. Two presses of this button — a double-click, or a client retry
    racing its own timeout — used to both read the same resumable status
    before either commit landed, both pass, and both dispatch a full
    ``run_autopilot``: two concurrent runs discovering the same companies and
    writing an ``Application`` per recruiter into a table with a
    ``(campaign_id, recruiter_id)`` uniqueness constraint. The one that lost
    the race did not queue a duplicate message — it crashed on that
    constraint, unhandled, and the campaign came back ``FAILED`` with a raw
    database error as its reason, right after the user asked it to resume.

    The lock closes the window a plain read-then-write leaves open: a second
    request blocks here until the first's transaction commits, then reads the
    status that transaction left — ``DISCOVERING``, not ``DRAFT`` — and is
    correctly refused below rather than reaching ``_launch`` at all.
    Advancing straight to ``DISCOVERING`` costs nothing ``run_autopilot``
    wasn't already going to do: it sets that status itself, unconditionally,
    as the first thing it does.
    """
    campaign = _get_owned(db, user, campaign_id, for_update=True)
    if campaign.status not in _RESUMABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Campaign is {campaign.status.value.lower()} and cannot be resumed",
        )
    if not user.gmail_connected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect a Gmail account before resuming outreach",
        )

    if campaign.is_system:
        return _rearm(db, campaign)

    campaign.status = CampaignStatus.DISCOVERING
    campaign.last_error = None
    usage_events.record(
        db, "campaign.resumed", user_id=user.id, campaign_id=campaign.id
    )
    db.commit()

    queued, detail = _launch(db, campaign)
    return CampaignStart(
        campaign=CampaignOut.model_validate(campaign), queued=queued, detail=detail
    )


def _rearm(db: Session, campaign: Campaign) -> CampaignStart:
    """Un-pause a container, rather than running the outreach pipeline over it.

    "Autopilot" and "Inbound recruiter replies" are rows the product creates to
    file work under; the user never configured a target list for either, because
    neither is a piece of outreach they asked for. `run_autopilot` over one of
    them therefore does not resume anything — `expand_targets` returns nothing,
    the run marks the campaign FAILED with "No target companies or industries
    were provided", and nothing anywhere resets a container out of FAILED, so
    every autopilot application and every inbound reply from then on is filed
    under a campaign the tracker renders as failed and `_maybe_complete_campaign`
    will never touch again.

    Two clicks reached it. The campaign strip lists containers alongside real
    campaigns and offers a paused row a restart, so: pause the Autopilot row,
    press restart. There was no third click that undid it.

    What resume *means* for a container is the whole of what pause meant: the
    send-time guard in `email_tasks` stops a PAUSED campaign's queued mail and
    leaves it QUEUED precisely so this can release it. So ACTIVE, and
    re-dispatch — no crawl, no composer, no LLM.
    """
    campaign.status = CampaignStatus.ACTIVE
    campaign.last_error = None
    # A container that drained was marked COMPLETED by `maybe_complete_campaign`
    # on its last send. Re-arming it without clearing this leaves the API
    # serving a completion date for a campaign that is running.
    campaign.completed_at = None
    usage_events.record(
        db, "campaign.resumed", user_id=campaign.user_id, campaign_id=campaign.id
    )
    db.commit()

    detail: str | None = None
    queued = 0
    try:
        from app.tasks.email_tasks import enqueue_campaign_sends

        queued, dispatch_error = enqueue_campaign_sends(db, campaign.id)
        detail = dispatch_error
    except Exception as exc:  # noqa: BLE001 - broker down; the rows stay QUEUED
        logger.warning(
            "campaign %s re-armed but its sends could not be dispatched: %s",
            campaign.id,
            exc,
        )
        detail = f"Sending is delayed — the task queue is unreachable ({exc})"

    db.refresh(campaign)
    return CampaignStart(
        campaign=CampaignOut.model_validate(campaign),
        queued=bool(queued),
        detail=detail,
    )
