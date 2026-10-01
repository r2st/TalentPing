"""The pipeline board's writes.

    PATCH /board/applications/{id}          -> move a card to another column
    GET   /board/applications/{id}/history  -> every stage it has held

The board's *read* is ``GET /dashboard``: the cards are the same rows the table
draws, carrying a ``board_stage`` the server computed, so the two views of the
page can never disagree about where an application sits. Only the move needs its
own endpoint.

Rules about which moves are allowed live in :mod:`app.services.pipeline_board`,
not here — the same rules have to hold whether a status changes because someone
dragged a card or because a reply arrived.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.application import Application
from app.models.status_event import ApplicationStatusEvent
from app.models.user import User
from app.schemas.board import (
    BoardMoveOut,
    BoardMoveRequest,
    StatusEventOut,
    StatusHistoryOut,
)
from app.services import pipeline_board as board

router = APIRouter(tags=["board"])


def _application_or_404(db: Session, user: User, application_id: int) -> Application:
    application = db.scalar(
        select(Application).where(
            Application.id == application_id, Application.user_id == user.id
        )
    )
    if application is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    return application


def _event_out(event: ApplicationStatusEvent) -> StatusEventOut:
    return StatusEventOut(
        at=event.created_at,
        from_status=event.from_status,
        to_status=event.to_status,
        source=event.source,
        reason=event.reason,
    )


@router.patch("/board/applications/{application_id}", response_model=BoardMoveOut)
def move_card(
    application_id: int,
    payload: BoardMoveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BoardMoveOut:
    """Drop a card into a column.

    A refused move is a 409 with the reason in the detail, and the UI puts the
    card back. The two refusals are an unknown column and a contact who opted
    out of email, whose stage is deliberately not the user's to change.
    """
    application = _application_or_404(db, user, application_id)

    try:
        event = board.move_application(
            db, application, payload.stage, note=payload.note
        )
    except board.BoardMoveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    db.commit()
    db.refresh(application)

    return BoardMoveOut(
        application_id=application.id,
        status=application.status,
        board_stage=board.stage_for(application.status),
        locked=board.is_locked(application.status),
        moved=event is not None,
        event=_event_out(event) if event is not None else None,
    )


@router.get(
    "/board/applications/{application_id}/history", response_model=StatusHistoryOut
)
def status_history(
    application_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StatusHistoryOut:
    """Every stage this application has held, oldest first.

    Applications created before the history table existed have no rows and
    return an empty list rather than a reconstruction: the transitions happened,
    but nothing recorded when, and inventing timestamps for them would make the
    one view whose job is to be trustworthy the least trustworthy on the page.
    """
    _application_or_404(db, user, application_id)

    events = db.scalars(
        select(ApplicationStatusEvent)
        .where(ApplicationStatusEvent.application_id == application_id)
        .order_by(
            ApplicationStatusEvent.created_at.asc(), ApplicationStatusEvent.id.asc()
        )
    ).all()

    return StatusHistoryOut(
        application_id=application_id, events=[_event_out(e) for e in events]
    )
