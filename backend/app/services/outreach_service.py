"""Outreach orchestration — the campaign pipeline, minus the transport.

Two steps live here, both reusable from the API and from Celery workers:

* :func:`generate_drafts_for_campaign` — recruiters in, personalized emails out.
* :func:`run_autopilot` — the whole thing: discover contacts, generate emails,
  hand the queue to the throttled sender.

Sending itself lives in ``tasks/email_tasks``; the split keeps this module
testable without a broker.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import events
from app.core.config import settings
from app.models.application import Application, ApplicationStatus
from app.models.campaign import STARTABLE_STATUSES, Campaign, CampaignStatus
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.recruiter import Recruiter
from app.models.resume import Resume
from app.models.subject_variant import SubjectVariant
from app.models.user import User
from app.services import (
    bounce_service,
    gmail_accounts,
    send_policy,
    spam_risk,
    subject_ab_service,
)
from app.services.ai_composer import (
    CandidateContext,
    Personalization,
    RecruiterContext,
    compose_outreach,
)
from app.services.recruiter_discovery import discover_for_companies, expand_targets

logger = logging.getLogger(__name__)


def resolve_resume(db: Session, user: User, resume_id: int | None) -> Resume | None:
    """The resume a campaign runs off: the named one, else the default, else newest."""
    if resume_id is not None:
        resume = db.get(Resume, resume_id)
        if resume is not None and resume.user_id == user.id:
            return resume
    return db.scalar(
        select(Resume)
        .where(Resume.user_id == user.id)
        .order_by(Resume.is_default.desc(), Resume.id.desc())
    )


def cancel_unsent_for_recruiter(db: Session, recruiter_id: int) -> int:
    """Write off every message to this contact that has not gone out yet.

    Called when a recruiter opts out. The send path refuses them anyway — see
    ``tasks.email_tasks._send_outreach_email`` — so this changes nothing about
    what reaches the recipient; it changes what the *user* sees. A campaign
    queues its messages hours ahead, so without this the tracker goes on showing
    mail as still coming for hours after the recipient asked us to stop, and the
    broker goes on waking up for sends that are already decided.

    DRAFT rows go too. A draft is a message the user is about to be invited to
    approve, and offering the send button for someone who has opted out is an
    invitation to make the mistake this whole apparatus exists to prevent.

    ``FAILED`` because it is the only terminal status an outbound row has, and it
    is the same verdict the send path would reach at due time. The caller
    commits.
    """
    unsent = db.scalars(
        select(Email)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.recruiter_id == recruiter_id,
            Email.direction == EmailDirection.SENT,
            Email.status.in_([EmailStatus.QUEUED, EmailStatus.DRAFT]),
        )
    ).all()
    touched: set[int] = set()
    for email in unsent:
        email.status = EmailStatus.FAILED
        touched.add(email.thread_id)
    # These rows were the only thing some campaign was still waiting on, and
    # writing them off is a drain the send path never sees. See
    # :func:`maybe_complete_campaign` for why that leaves a campaign ACTIVE
    # forever otherwise.
    for campaign in _campaigns_of_threads(db, touched):
        maybe_complete_campaign(db, campaign)
    return len(unsent)


def _campaigns_of_threads(db: Session, thread_ids: set[int]) -> list[Campaign]:
    """The campaigns behind a set of threads, deduplicated.

    Ids first, then a load per distinct id — rather than ``SELECT DISTINCT`` over
    the campaign rows themselves. ``Campaign`` carries three ``JSON`` columns,
    and Postgres has no equality operator for ``json`` (only ``jsonb``), so
    de-duplicating whole rows is a statement that runs on SQLite and raises
    "could not identify an equality operator for type json" in production. The
    ids are integers and dedupe in Python for free; the loads come out of the
    identity map for anything the caller is already holding.
    """
    if not thread_ids:
        return []
    campaign_ids = set(
        db.scalars(
            select(Application.campaign_id)
            .join(EmailThread, EmailThread.application_id == Application.id)
            .where(EmailThread.id.in_(thread_ids))
        )
    )
    found = (db.get(Campaign, campaign_id) for campaign_id in campaign_ids)
    return [campaign for campaign in found if campaign is not None]


def campaign_for_email(db: Session, email: Email) -> Campaign | None:
    """The campaign an outbound row belongs to, or ``None``.

    Resolved from the row rather than passed in, because the callers that need
    it most are the ones that retire a message *without* having loaded the
    campaign: a dismissal in the review queue, a terminal send failure, an
    opt-out sweeping the rest of a batch.
    """
    return db.scalar(
        select(Campaign)
        .join(Application, Application.campaign_id == Campaign.id)
        .join(EmailThread, EmailThread.application_id == Application.id)
        .where(EmailThread.id == email.thread_id)
    )


def maybe_complete_campaign(db: Session, campaign: Campaign | None) -> None:
    """Mark a campaign COMPLETED once nothing is left to send.

    Counts what is *actually* outstanding. It used to allow one — the email
    currently being sent, which had not been committed as SENT yet — and that
    allowance is now wrong twice over: the send commits before this runs, so the
    row is no longer pending, and forgiving one pending row after the fact
    completes a campaign while its last message is still queued.

    **A send is not the only way the pending set empties.** This lived in
    ``tasks.email_tasks`` and was called from exactly one place: the bookkeeping
    that runs after a message is successfully delivered. Every other way an
    outbound row leaves QUEUED or DRAFT left the campaign ACTIVE with nothing
    outstanding and nothing that would ever look again —

    * the send path writing a message off (``_fail_send``): the recipient opted
      out, the user excluded them, the address hard-bounced. All three arrive
      *during* the hours a campaign trickles over, which is exactly when the
      last message of a batch is still queued;
    * :func:`cancel_unsent_for_recruiter`, which writes off the rest of a batch
      the moment a recipient unsubscribes;
    * a draft dismissed from the review queue, which deletes the row outright.

    An ACTIVE campaign with an empty queue is not cosmetic: the tracker treats
    ACTIVE as live and keeps polling it for the life of the account, the strip
    offers neither a restart nor a completion, and the campaign never records a
    ``completed_at``. So the check belongs at every drain, not at the happy one.

    Does not commit — every caller is already inside a transaction that does.
    """
    if campaign is None or campaign.status != CampaignStatus.ACTIVE:
        return
    # Every session in this codebase is ``autoflush=False``, and every caller
    # but one reaches here holding the very write that drained the campaign —
    # a status set to FAILED, a draft deleted. Unflushed, the count below reads
    # the pre-drain state and the campaign is never completed, which is the bug
    # this function was spread out to fix wearing a different hat.
    db.flush()
    remaining = db.scalar(
        select(func.count(Email.id))
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.campaign_id == campaign.id,
            Email.direction == EmailDirection.SENT,
            Email.status.in_([EmailStatus.QUEUED, EmailStatus.DRAFT]),
        )
    )
    if not remaining:
        campaign.status = CampaignStatus.COMPLETED
        campaign.completed_at = datetime.now(UTC)
        # Emitted here rather than at the four callers for the same reason the
        # check itself was moved here: a send is not the only drain, and an
        # event attached to the send path would record the completions that
        # happen to end on a delivery and miss the ones that end on an
        # unsubscribe, a write-off or a dismissed draft.
        events.emit(
            events.CAMPAIGN_COMPLETED,
            campaign_id=campaign.id,
            user_id=campaign.user_id,
            contacts_found=campaign.contacts_found,
            emails_generated=campaign.emails_generated,
        )


def maybe_complete_campaign_for_email(db: Session, email: Email) -> None:
    """:func:`maybe_complete_campaign`, for a caller holding only the email."""
    maybe_complete_campaign(db, campaign_for_email(db, email))


# How many ids one ``IN`` clause carries. A launch can arrive with thousands of
# recruiter ids, and a single parameter list that long is refused outright by
# some drivers and planned badly by the rest — so the batch read is chunked
# rather than merely being "not one query per row".
_ID_CHUNK = 500


def _recruiters_by_id(
    db: Session, user: User, recruiter_ids: list[int]
) -> dict[int, Recruiter]:
    """*user*'s recruiters among *recruiter_ids*, keyed by id.

    Filtered by owner here rather than checked per row afterwards, which is the
    same answer: the caller's note for a recruiter belonging to somebody else is
    the same "not found" it gives for an id that does not exist, and it has to
    be — a distinguishable message would confirm the other user's row.
    """
    found: dict[int, Recruiter] = {}
    unique = list(dict.fromkeys(recruiter_ids))
    for start in range(0, len(unique), _ID_CHUNK):
        chunk = unique[start : start + _ID_CHUNK]
        for row in db.scalars(
            select(Recruiter).where(
                Recruiter.id.in_(chunk), Recruiter.user_id == user.id
            )
        ):
            found[row.id] = row
    return found


def generate_drafts_for_campaign(
    db: Session, user: User, campaign: Campaign, recruiter_ids: list[int]
) -> tuple[list[int], list[str]]:
    """Create an Application + thread + outreach email per recruiter.

    Emails land as ``QUEUED`` when the campaign is on autopilot and ``DRAFT``
    when the user wants to review first. Idempotent per (campaign, recruiter):
    a recruiter already in the campaign is skipped with a note.

    Returns ``(created_application_ids, notes)``.
    """
    resume = resolve_resume(db, user, campaign.resume_id)
    cand = CandidateContext.from_resume(
        resume, fallback_name=user.full_name or user.email.split("@")[0]
    )
    # The campaign's own flag says what the user asked for when they launched it;
    # the policy says whether that is allowed right now. A pause or a spent daily
    # ceiling has to reach a campaign launched before it was set — a pause that
    # only governed autopilot would be a pause the user could not trust.
    auto_send = send_policy.evaluate(
        db, user, user.autopilot, intent=campaign.auto_send
    ).enabled
    initial_status = EmailStatus.QUEUED if auto_send else EmailStatus.DRAFT
    # ...and how many, which `evaluate` cannot say. The daily ceiling is per
    # message, and this loop is the only producer of outreach with no bound of
    # its own: `recruiter_ids` is whatever the discovery crawl turned up across
    # the target companies, and auto-apply has its own daily application budget.
    # Asking the yes/no question once for a hundred recruiters spent the ceiling
    # on the first of them and sent the other ninety-nine anyway — a user who
    # set the dial to 5 because that is how much unsupervised sending they were
    # willing to trust got the whole batch.
    #
    # None means no ceiling configured, which is the default.
    allowance = send_policy.remaining_allowance(db, user, user.autopilot) if auto_send else 0
    spent_allowance = False
    # Which mailbox this campaign's conversations belong to, decided once for the
    # whole batch. Stamped on every thread below so the sender, the poller and
    # the reputation ledger all agree later without having to guess — a Gmail
    # thread id means nothing outside the mailbox holding it.
    #
    # The campaign's own choice wins where the user made one; otherwise this is
    # the primary, exactly as it was before campaigns could name an address.
    sending_account = gmail_accounts.resolve_for_new_outreach(
        db, user, campaign=campaign
    )
    # How this user wants their outreach to read. Resolved once for the batch —
    # it is one row, and it cannot change mid-loop.
    personalization = Personalization.from_preference(user.autopilot)

    created: list[int] = []
    notes: list[str] = []

    # The batch's rows, read in two queries instead of two per contact.
    #
    # `recruiter_ids` is whatever the discovery crawl turned up across every
    # target company, so a launch against a dozen companies routinely arrives
    # here with a hundred ids or more. Each one used to cost a `db.get` (a real
    # SELECT — the run committed immediately before this call, which expires
    # every object the session already held) and a second SELECT asking whether
    # this campaign already had an application for it. Two hundred round trips
    # before a single email was composed, all of them answerable in two.
    #
    # The membership test is a snapshot rather than a live read, which widens a
    # race that was already there: nothing holds a lock between the check and the
    # insert, so two runs of one campaign overlapping could always reach the
    # insert for the same contact, and now they can do it for the whole run
    # rather than up to the moment the other one committed.
    #
    # What that race produces is a *crash*, not a duplicate email:
    # ``uq_application_campaign_recruiter`` covers (campaign_id, recruiter_id),
    # so the loser raises IntegrityError, `run_autopilot` catches it and the
    # campaign comes back FAILED with a raw database error as its reason. The
    # recruiter is never mailed twice. Left as it is because that race is closed
    # a level up rather than here: both doors into a run claim the row with
    # DISCOVERING before dispatching (see `routers/campaigns`), so a second
    # concurrent run is refused by `STARTABLE_STATUSES` before it reaches this
    # loop.
    by_id = _recruiters_by_id(db, user, recruiter_ids)
    already_in_campaign = set(
        db.scalars(
            select(Application.recruiter_id).where(
                Application.campaign_id == campaign.id
            )
        )
    )

    # The campaign's subject experiment, read once. ``ensure_variants`` is
    # idempotent and campaign-scoped: it generates the arms on first use and
    # hands the same rows back to every later caller. The loop was calling it per
    # message anyway, so each email past the first paid a SELECT (and a savepoint
    # around an insert it was never going to make) to re-read rows already in the
    # session. Resolved lazily rather than before the loop because the *first
    # eligible* recruiter's context is what seeds the generated text — hoisting
    # it above the filters would seed the experiment from a contact that may be
    # opted out, excluded or undeliverable.
    variants: list[SubjectVariant] | None = None

    for rid in recruiter_ids:
        recruiter = by_id.get(rid)
        if recruiter is None:
            notes.append(f"recruiter {rid}: not found")
            continue
        if recruiter.opted_out:
            notes.append(f"{recruiter.email}: opted out")
            continue
        if recruiter.is_excluded:
            # The user's own decision, not the recipient's — see
            # ``Recruiter.excluded_at``. Named distinctly in the notes so the
            # tracker never tells someone a contact "opted out" when in fact
            # they themselves set the contact aside.
            notes.append(f"{recruiter.email}: excluded by you")
            continue
        if bounce_service.is_suppressed(recruiter):
            # A permanently undeliverable address. Skipped here so no email is
            # even composed for it — distinct from opted_out, which is consent.
            notes.append(f"{recruiter.email}: undeliverable (hard bounce)")
            continue

        if recruiter.id in already_in_campaign:
            notes.append(f"{recruiter.email}: already in this campaign")
            continue

        rec_context = RecruiterContext.from_recruiter(recruiter)
        composed = compose_outreach(cand, rec_context, personalization)

        # Subject-line experiment: the campaign's variants are generated once,
        # on the first email, then one arm is assigned per message. The composed
        # subject stays the fallback when the experiment is off.
        subject = composed.subject
        variant_id: int | None = None
        if settings.subject_ab_enabled:
            if variants is None:
                variants = subject_ab_service.ensure_variants(
                    db, campaign, cand, rec_context
                )
            variant = subject_ab_service.assign_variant(variants)
            if variant is not None:
                subject, variant_id = variant.text, variant.id

        # What a receiving filter will make of the words. Scored per message
        # rather than per batch because the composer personalises each one, so
        # this is the only point at which the actual text exists. A risky
        # message is held for review, never dropped — same treatment a paused
        # account gets, and the note says which of the two it was.
        may_auto_send, content = spam_risk.screen(
            subject, composed.body, auto_send=initial_status is EmailStatus.QUEUED
        )
        email_status = initial_status
        if not may_auto_send and email_status is EmailStatus.QUEUED:
            email_status = EmailStatus.DRAFT
            notes.append(f"{recruiter.email}: held for review — {content.summary}")
            logger.info(
                "outreach held for review on content risk %d for recruiter %s",
                content.risk,
                recruiter.id,
            )

        # Checked after the content gate, so a message already held for review
        # does not spend an allowance it is not using.
        if email_status is EmailStatus.QUEUED and allowance is not None:
            if allowance <= 0:
                email_status = EmailStatus.DRAFT
                if not spent_allowance:
                    spent_allowance = True
                    # One note for the batch, not one per recruiter: the reason
                    # is the same for every message past the ceiling, and a
                    # hundred copies of it buries the rest of the notes.
                    notes.append(
                        "Daily auto-send allowance reached — the rest are "
                        "waiting in your review queue."
                    )
            else:
                allowance -= 1

        application = Application(
            user_id=user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
            status=ApplicationStatus.QUEUED,
        )
        db.add(application)
        db.flush()  # assign application.id

        thread = EmailThread(
            application_id=application.id,
            subject=subject,
            gmail_account_id=sending_account.id if sending_account else None,
        )
        db.add(thread)
        db.flush()

        db.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.SENT,
                status=email_status,
                to_address=recruiter.email,
                subject=subject,
                body_text=composed.body,
                subject_variant_id=variant_id,
                # Recorded at queue time, not send time: this is the only moment
                # that knows whether a human is going to read it first. Approving
                # from the review queue clears it back to False. Follows the
                # status rather than the policy, so a message the content check
                # pulled back is not still labelled as having been auto-sent.
                auto_sent=email_status is EmailStatus.QUEUED,
            )
        )
        thread.message_count = 1
        created.append(application.id)
        # Grown as we go, exactly as the per-row SELECT it replaced behaved: the
        # `db.flush` above made the new application visible to that query, so a
        # `recruiter_ids` list carrying the same id twice took the second one as
        # "already in this campaign". Dropping this line would make the duplicate
        # a second application, a second thread and a second email to one person.
        already_in_campaign.add(recruiter.id)

    campaign.emails_generated = (campaign.emails_generated or 0) + len(created)
    db.commit()
    return created, notes


def _paused_mid_run(db: Session, campaign: Campaign) -> bool:
    """Did the user press pause while this run was working?

    A run is not instant: discovery crawls one target company at a time and the
    composer writes an LLM-backed email per contact it found, so the window
    between "start" and "active" is minutes wide and sometimes much more. Pause
    is reachable throughout it — ``routers/campaigns.pause_campaign`` refuses
    only ``COMPLETED`` and an existing pause, so a campaign in ``DISCOVERING`` or
    ``GENERATING`` takes the new status happily.

    And then this function's caller overwrote it. ``campaign.status = ACTIVE``
    was unconditional, so the pause the user had just set was replaced by the
    worker a minute later, ``enqueue_campaign_sends`` handed the whole batch to
    the broker, and the send-time guard that would otherwise have parked every
    one of them read ``ACTIVE`` and let them through. The user pressed stop and
    the campaign sent anyway, with nothing anywhere recording that it had been
    asked not to.

    Read from the database rather than from the in-session attribute: the row
    was changed by the request thread, and this session has held its own copy
    since before that happened. ``expire`` forces the re-read without discarding
    the rest of the session's work.
    """
    db.expire(campaign, ["status"])
    return campaign.status == CampaignStatus.PAUSED


def _stopped(campaign_id: int, phase: str) -> dict:
    """The result of a run that found itself paused. Nothing is failed."""
    logger.info("campaign %s paused during %s; leaving it paused", campaign_id, phase)
    return {
        "campaign_id": campaign_id,
        "status": "paused",
        "phase": phase,
        # Surfaced through `routers/campaigns._launch`, which reads these to
        # explain an inline run. "Nothing happened" and "you paused it" look
        # identical from the tracker otherwise.
        "notes": ["Paused before anything was sent — resume to send what's queued."],
    }


def run_autopilot(db: Session, campaign: Campaign) -> dict:
    """Run a campaign end to end: find contacts, write emails, queue the sends.

    Each phase records its progress on the campaign row before starting, so the
    tracker can show what the pipeline is doing while it runs. A failure marks
    the campaign ``FAILED`` with the reason rather than leaving it stuck.
    """
    campaign_id = campaign.id
    user = db.get(User, campaign.user_id)
    if user is None:  # pragma: no cover - the FK makes this unreachable
        return {"campaign_id": campaign_id, "status": "orphaned"}

    # Before the first write, because the first write is the damage. See
    # :data:`app.models.campaign.STARTABLE_STATUSES` for what each exclusion
    # costs when it is missing; the short version is that this function used to
    # begin by stamping DISCOVERING over whatever it found, so a pause that
    # landed while the task was still sitting in the queue was erased by the
    # worker that eventually picked it up, and a redelivered ``start_campaign``
    # re-ran a finished campaign end to end.
    #
    # Read from the database rather than the in-session attribute, for
    # ``_paused_mid_run``'s reason: the row may well have been changed by a
    # request thread since this session loaded it.
    db.expire(campaign, ["status"])
    if campaign.status not in STARTABLE_STATUSES:
        if campaign.status is CampaignStatus.PAUSED:
            return _stopped(campaign_id, "start")
        logger.info(
            "campaign %s is %s; not starting a run over it",
            campaign_id,
            campaign.status.value,
        )
        return {
            "campaign_id": campaign_id,
            "status": "not_startable",
            "campaign_status": campaign.status.value,
        }

    campaign.status = CampaignStatus.DISCOVERING
    campaign.started_at = campaign.started_at or datetime.now(UTC)
    campaign.last_error = None
    # Cleared, unlike `started_at`, which is the first start and stays it. A row
    # arriving here can carry one — the containers are re-armed out of COMPLETED
    # by their own producers — and "completed at" on a campaign that is crawling
    # right now is a field the API serves and no reader can be expected to
    # discount.
    campaign.completed_at = None
    db.commit()

    try:
        resume = resolve_resume(db, user, campaign.resume_id)
        roles = campaign.target_roles or (resume.target_roles if resume else []) or []
        companies = expand_targets(
            list(campaign.target_companies or []),
            list(campaign.target_industries or []),
            roles,
        )
        if not companies:
            campaign.status = CampaignStatus.FAILED
            campaign.last_error = "No target companies or industries were provided"
            events.emit(
                events.CAMPAIGN_FAILED,
                logging.WARNING,
                campaign_id=campaign.id,
                user_id=campaign.user_id,
                reason="no targets",
            )
            db.commit()
            return {"campaign_id": campaign_id, "status": "failed", "reason": "no targets"}

        recruiter_ids, discovery_notes = discover_for_companies(db, user, companies)
        campaign.companies_processed = len(companies)
        campaign.contacts_found = len(recruiter_ids)
        db.commit()

        if not recruiter_ids:
            campaign.status = CampaignStatus.COMPLETED
            campaign.completed_at = datetime.now(UTC)
            campaign.last_error = "; ".join(discovery_notes[:5]) or "No contacts found"
            # Not routed through `maybe_complete_campaign`: this campaign never
            # queued a message, so the drain check would find nothing pending
            # and complete it for the wrong reason. A campaign that found
            # nobody is the most common non-failure a launch has, and it looked
            # identical in the log to one that mailed everybody.
            events.emit(
                events.CAMPAIGN_COMPLETED,
                campaign_id=campaign.id,
                user_id=campaign.user_id,
                companies_processed=campaign.companies_processed,
                contacts_found=0,
                reason="no contacts found",
            )
            db.commit()
            return {
                "campaign_id": campaign_id,
                "status": "completed",
                "contacts": 0,
                "notes": discovery_notes,
            }

        # Checked between phases, at each point the run has just committed and
        # is about to spend real money or real reputation on the next one.
        if _paused_mid_run(db, campaign):
            return _stopped(campaign_id, "discovery")

        campaign.status = CampaignStatus.GENERATING
        db.commit()
        created, generation_notes = generate_drafts_for_campaign(
            db, user, campaign, recruiter_ids
        )

        # The one that matters. Everything written above is recoverable — the
        # drafts stay QUEUED and go out when the campaign is resumed — but
        # flipping to ACTIVE here would erase the pause itself, and the
        # send-time guard has nothing left to read.
        if _paused_mid_run(db, campaign):
            return {**_stopped(campaign_id, "generation"), "emails": len(created)}

        campaign.status = CampaignStatus.ACTIVE
        db.commit()

        queued = 0
        policy_notes: list[str] = []
        policy = send_policy.evaluate(
            db, user, user.autopilot, intent=campaign.auto_send
        )
        if policy.enabled:
            # Local import: the API and tests must not require a live broker.
            from app.tasks.email_tasks import enqueue_campaign_sends

            queued, dispatch_error = enqueue_campaign_sends(db, campaign_id)
            if dispatch_error:
                # The emails are written and queued; only delivery is delayed.
                # Surfaced on the campaign rather than failing it.
                campaign.last_error = dispatch_error
                db.commit()
        elif campaign.auto_send and policy.reason:
            # The user asked for auto-send and got drafts. Say why, or the
            # campaign reads as broken — the emails are there and nothing moved.
            policy_notes.append(policy.reason)

        return {
            "campaign_id": campaign_id,
            "status": "active",
            "companies": len(companies),
            "contacts": len(recruiter_ids),
            "emails": len(created),
            "queued": queued,
            "notes": [*policy_notes, *discovery_notes, *generation_notes],
        }
    except Exception as exc:  # noqa: BLE001 - record the failure on the campaign
        logger.exception("autopilot failed for campaign %s", campaign_id)
        db.rollback()
        failed = db.get(Campaign, campaign_id)
        if failed is not None:
            # The reason is recorded either way; the *status* is not written
            # over a pause. This is the third window of the same failure
            # `_paused_mid_run` and `STARTABLE_STATUSES` between them close at
            # the start and at each phase boundary — and it was the one left
            # open. A run is minutes wide and pause is reachable throughout it,
            # so a crawl that dies on a network blip seconds after the user
            # presses stop erased the stop and reported a failure the user did
            # not cause. `_stopped` says "Nothing is failed" about the ordinary
            # path for a reason; the same is true when the run happens to die.
            #
            # Not merely cosmetic: FAILED is one of the two statuses the
            # campaign strip offers a restart on, so the pause was replaced by
            # an invitation to relaunch the batch the user had just stopped.
            if failed.status is not CampaignStatus.PAUSED:
                failed.status = CampaignStatus.FAILED
            failed.last_error = str(exc)[:500]
            # Reports the status actually written, not the one this branch is
            # named after: the pause above is exactly the case where those two
            # disagree, and an event that always said "failed" would put the
            # campaigns a user deliberately stopped into the failure count.
            events.emit(
                events.CAMPAIGN_FAILED,
                logging.WARNING,
                campaign_id=campaign_id,
                user_id=failed.user_id,
                status=failed.status.value,
                reason=type(exc).__name__,
            )
            db.commit()
        return {"campaign_id": campaign_id, "status": "failed", "reason": str(exc)[:200]}
