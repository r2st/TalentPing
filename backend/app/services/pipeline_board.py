"""The board: five columns over twelve statuses, and the rules for moving a card.

``ApplicationStatus`` has twelve members because it records what actually
happened to an outreach — queued, sent, nudged, replied, opted out. A board a
person can drag cards across cannot have twelve columns, so this module owns the
collapse down to the five stages the user asked to see, and — the harder half —
what a *drop* means when it runs the other way.

Going from a status to a column is a lookup. Going from a column to a status is
a decision, because most columns hold more than one status, and picking wrong
writes something false into the pipeline. Two rules decide it:

**The user is the authority on the recruiter; the product is the authority on
itself.** "They want to interview me" is something only the candidate can know,
so a drop into *Interview* is taken at face value. "We emailed them" is not: it
is a fact about our own outbox. Dropping a card into *Applied* therefore asks
the outbox rather than the user — an application with a sent email lands on
``OUTREACH_SENT``, one without lands back on ``QUEUED`` — so no drag can ever
make the pipeline claim an email that was never sent.

**Consent is not a stage.** ``UNSUBSCRIBED`` is the one status a drag may
neither set nor clear. Setting it would let a mis-drop suppress a contact who
never asked to be left alone; clearing it would re-arm the follow-up drip
against someone who did (see ``follow_up_service.TERMINAL_STATUSES``). A card in
that state is read-only on the board and says so.

Every move an endpoint makes goes through :func:`move_application`, and every
transition — automatic ones too — goes through :func:`record_change`, so the
history table is a complete account rather than a log of the manual half.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.application import Application, ApplicationStatus
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.status_event import ApplicationStatusEvent, StatusEventSource

logger = logging.getLogger(__name__)


class BoardMoveError(Exception):
    """A move the board refuses to make. The message is shown to the user."""


# key, label, the statuses that land in this column, and the status a drop
# writes. The drop target is the *weakest* claim that still belongs in the
# column: a card dragged to Screening becomes REPLIED rather than INTERESTED,
# because "we are talking" is what the gesture means and "they like me" is more
# than the user said.
#
# UNSUBSCRIBED appears in `statuses` (opted-out cards have to render somewhere)
# but is never a `drop_status` — see the module docstring.
BOARD_STAGES: list[tuple[str, str, set[ApplicationStatus], ApplicationStatus]] = [
    (
        "applied",
        "Applied",
        {ApplicationStatus.QUEUED, ApplicationStatus.OUTREACH_SENT, ApplicationStatus.FOLLOW_UP},
        # Overridden by the outbox check in `_applied_status`; never written raw.
        ApplicationStatus.OUTREACH_SENT,
    ),
    (
        "screening",
        "Screening",
        {ApplicationStatus.REPLIED, ApplicationStatus.INTERESTED},
        ApplicationStatus.REPLIED,
    ),
    (
        "interview",
        "Interview",
        {ApplicationStatus.SCHEDULING, ApplicationStatus.INTERVIEW_SCHEDULED},
        ApplicationStatus.INTERVIEW_SCHEDULED,
    ),
    ("offer", "Offer", {ApplicationStatus.OFFER}, ApplicationStatus.OFFER),
    (
        # Everything that ended without an offer. NO_RESPONSE and CLOSED are not
        # rejections and are not described as such — they keep their own badge on
        # the card — but they are over, and a column per ending would be three
        # columns the user never drags anything into.
        "rejected",
        "Rejected",
        {
            ApplicationStatus.NOT_INTERESTED,
            ApplicationStatus.UNSUBSCRIBED,
            ApplicationStatus.NO_RESPONSE,
            ApplicationStatus.CLOSED,
        },
        ApplicationStatus.NOT_INTERESTED,
    ),
]

_STAGE_BY_STATUS: dict[ApplicationStatus, str] = {
    status: key for key, _label, statuses, _drop in BOARD_STAGES for status in statuses
}
_DROP_STATUS: dict[str, ApplicationStatus] = {
    key: drop for key, _label, _statuses, drop in BOARD_STAGES
}

# The status a drag may neither set nor clear. One member today; a set because
# "which statuses are consent rather than progress" is the question being asked,
# and the answer grows the first time a second one exists.
LOCKED_STATUSES = frozenset({ApplicationStatus.UNSUBSCRIBED})

# How far along the open ladder each status sits. Only the statuses a
# conversation *passes through* are ranked; the four endings are deliberately
# absent, because they are outcomes rather than rungs — see :func:`advances`.
_PROGRESS: dict[ApplicationStatus, int] = {
    ApplicationStatus.QUEUED: 0,
    ApplicationStatus.OUTREACH_SENT: 1,
    ApplicationStatus.FOLLOW_UP: 2,
    ApplicationStatus.REPLIED: 3,
    ApplicationStatus.INTERESTED: 4,
    ApplicationStatus.SCHEDULING: 5,
    ApplicationStatus.INTERVIEW_SCHEDULED: 6,
    ApplicationStatus.OFFER: 7,
}

# The four ways an application ends. An ending may always be recorded, from
# anywhere on the ladder: a recruiter who withdraws after making an offer really
# has taken the application to NOT_INTERESTED, and refusing that would be a
# pipeline that can only ever go up.
ENDINGS = frozenset(
    {
        ApplicationStatus.NOT_INTERESTED,
        ApplicationStatus.NO_RESPONSE,
        ApplicationStatus.UNSUBSCRIBED,
        ApplicationStatus.CLOSED,
    }
)


def advances(current: ApplicationStatus, target: ApplicationStatus) -> bool:
    """May an *automatic* transition write *target* over *current*?

    The reply classifier reads one message at a time and maps its intent onto a
    status, which is right for a conversation moving forward and wrong for one
    already past the rung it names. A recruiter three messages into an offer who
    writes "great — still interested?" classifies as ``INTERESTED``; one asking
    to move Thursday's loop classifies as ``SCHEDULING``. Both were written
    straight onto the application, so the card fell from *Offer* back to
    *Screening*, the board moved it while the user watched, and the funnel — which
    counts ``INTERVIEWING_STATUSES`` off the live status — stopped reporting an
    offer the user actually had.

    Worse than a wrong label: ``INTERVIEW_SCHEDULED`` is in
    ``follow_up_service.TERMINAL_STATUSES`` and ``SCHEDULING`` is not, so the
    demotion also put a booked interview back in reach of the follow-up drip.

    So an automatic transition may move *up* the ladder or record an ending, and
    may do nothing else. Manual moves are unaffected and go on being taken at
    face value: the user is the authority on the recruiter, which is the rule
    this module already runs on — they can drag a card back themselves, and a
    classifier reading one sentence cannot.
    """
    if current in LOCKED_STATUSES:
        # Consent is not progress and is not re-opened by a later message. A
        # contact who opted out and then writes again classifies as INTERESTED
        # like anyone else, and lifting the card out of *Rejected* on that basis
        # would hide the opt-out from the board and unlock a card the drag rules
        # deliberately freeze.
        return False
    if target in ENDINGS or current in ENDINGS:
        # An ending may be recorded from anywhere, and a recruiter who said no
        # in March really can come back in May with a different role. Neither is
        # the demotion this guard is about.
        return True
    return _PROGRESS[target] > _PROGRESS[current]


def not_yet_replied(status: ApplicationStatus) -> bool:
    """True while the pipeline holds no reply against this application.

    Derived from :data:`_PROGRESS` rather than listed, because "which rungs come
    before a reply" is a question the ladder already answers and a second copy
    of it goes stale the first time a rung is added between them.

    The caller is ``inbox_tasks._apply_intent``, deciding whether an inbound
    message it could not map onto a specific status — ``QUESTION``, ``OTHER``,
    an unclassified null — should at least record ``REPLIED``. It used to ask
    ``status == OUTREACH_SENT``, which is only the *first* rung a reply can land
    on. A recruiter who answers after a nudge is answering an application
    already moved to ``FOLLOW_UP``, and "who is this? what role?" is exactly the
    reply the sequence provokes — so the commonest case the fallback exists for
    was the one it did not cover. The card stayed in *Applied*, and every rate
    computed off ``ENGAGED_STATUSES`` — the dashboard funnel, the campaign's
    response rate, the weekly digest's reply count, the fit calibrator's
    training label — read a real reply as silence. The campaign line was the
    plainest contradiction: 0% response rate beside a median time-to-first-
    response of a day and a half, because that figure is computed from the mail
    rather than from the status.

    The four endings are absent from ``_PROGRESS`` and so answer False: a
    conversation that closed is not re-opened by a later message, which is the
    rule :func:`advances` already keeps for ``LOCKED_STATUSES``.
    """
    rank = _PROGRESS.get(status)
    return rank is not None and rank < _PROGRESS[ApplicationStatus.REPLIED]


def stage_for(status: ApplicationStatus) -> str:
    """Which column a status renders in. Total over ``ApplicationStatus``."""
    # Falling back to "applied" would quietly file an unmapped future status
    # under a stage it does not belong to. Every member is covered above, and a
    # thirteenth should fail loudly here rather than be mis-drawn.
    return _STAGE_BY_STATUS[status]


def is_locked(status: ApplicationStatus) -> bool:
    """Whether the board must refuse to move a card in this status."""
    return status in LOCKED_STATUSES


def _has_sent_outreach(db: Session, application_id: int) -> bool:
    """Did an email for this application actually leave?

    The one question that decides what a drop into *Applied* is allowed to say.
    Asked of the outbox rather than the status, because the status is exactly
    what is in doubt.
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


def _applied_status(db: Session, application: Application) -> ApplicationStatus:
    """What *Applied* means for this particular card.

    ``FOLLOW_UP`` is preserved rather than flattened to ``OUTREACH_SENT``: a card
    already in the follow-up sequence that is dragged within its own column has
    not changed, and rewriting it would lose which step the drip is on.
    """
    if application.status == ApplicationStatus.FOLLOW_UP:
        return ApplicationStatus.FOLLOW_UP
    if _has_sent_outreach(db, application.id):
        return ApplicationStatus.OUTREACH_SENT
    return ApplicationStatus.QUEUED


def record_change(
    db: Session,
    application: Application,
    to_status: ApplicationStatus,
    *,
    source: StatusEventSource,
    reason: str | None = None,
    from_status: ApplicationStatus | None = None,
) -> ApplicationStatusEvent:
    """Write the application's new status and the row that explains it.

    Callers pass ``from_status`` only when they have already assigned the new
    status themselves; otherwise the current value is read here, before the
    assignment, which is the ordering every caller gets wrong exactly once.
    """
    previous = from_status if from_status is not None else application.status
    application.status = to_status

    event = ApplicationStatusEvent(
        application_id=application.id,
        user_id=application.user_id,
        from_status=previous,
        to_status=to_status,
        source=source,
        reason=reason[:255] if reason else None,
    )
    db.add(event)
    return event


def move_application(
    db: Session,
    application: Application,
    stage: str,
    *,
    note: str | None = None,
) -> ApplicationStatusEvent | None:
    """Move a card to ``stage``. Returns None when nothing changed.

    Raises :class:`BoardMoveError` for a stage that does not exist and for a card
    the board is not allowed to move. A no-op move — dropping a card back in the
    column it came from, which is most of what a real drag session produces —
    writes no history row rather than a page of identical ones.
    """
    if stage not in _DROP_STATUS:
        raise BoardMoveError(f"Unknown stage {stage!r}")

    if is_locked(application.status):
        raise BoardMoveError(
            "This contact opted out of email. Their stage is locked so the "
            "follow-up sequence can never be restarted against them."
        )

    # A card dropped back in the column it came from has not moved, and the
    # column is all the gesture says. Comparing against `_DROP_STATUS` instead
    # only caught the cards already sitting on their column's drop status, so
    # every other member of a multi-status column was rewritten by a drag that
    # meant nothing — in both directions:
    #
    #   INTERESTED  -> screening  became REPLIED, discarding "they like me"
    #   NO_RESPONSE -> rejected   became NOT_INTERESTED, calling a silence a
    #                             rejection the column header explicitly says
    #                             it is not
    #   SCHEDULING  -> interview  became INTERVIEW_SCHEDULED, which is worse
    #                             than losing detail: it claims a booked
    #                             interview the user never said they had, and
    #                             it is in `follow_up_service.TERMINAL_STATUSES`
    #                             — so a drag that moved nothing silently
    #                             cancelled the rest of the follow-up sequence.
    #
    # `_applied_status` already had this rule for FOLLOW_UP; it belongs to every
    # column, not that one.
    if stage_for(application.status) == stage:
        return None

    target = (
        _applied_status(db, application)
        if stage == "applied"
        else _DROP_STATUS[stage]
    )
    if target in LOCKED_STATUSES:  # pragma: no cover - no stage drops into one
        raise BoardMoveError("That stage cannot be set by hand.")

    if target == application.status:
        return None

    previous = application.status
    event = record_change(
        db,
        application,
        target,
        source=StatusEventSource.MANUAL,
        reason=note or f"moved to {stage} on the board",
    )
    logger.info(
        "board move application=%s %s -> %s", application.id, previous.value, target.value
    )
    return event


__all__ = [
    "BOARD_STAGES",
    "ENDINGS",
    "LOCKED_STATUSES",
    "BoardMoveError",
    "advances",
    "is_locked",
    "move_application",
    "record_change",
    "stage_for",
]
