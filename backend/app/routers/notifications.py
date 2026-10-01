"""The notification centre.

    GET    /notifications              -> a page of them, with the unread count
    GET    /notifications/unread       -> just the count (the nav polls this)
    POST   /notifications/{id}/read    -> mark one read
    POST   /notifications/read-all     -> mark every unread one read
    DELETE /notifications/{id}         -> dismiss one
    DELETE /notifications              -> clear the list
    GET    /notifications/preferences  -> which kinds are on
    PUT    /notifications/preferences  -> change that

Nothing here creates a notification. Writing is the sweep's job
(:mod:`app.services.notification_sweep`) and the emitters' — a route that could
mint one would be a route that could mint one for somebody else.

The unread count is a separate, deliberately tiny endpoint because it is polled
on every route in the app. ``GET /notifications`` loads rows; this one loads an
integer, and the difference is a full page of JSON every thirty seconds for the
entire life of every session.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import MAX_DB_INT, set_page_headers
from app.models.notification import NOTIFICATION_KINDS
from app.models.user import User
from app.schemas.notification import (
    DismissAllResult,
    MarkAllReadResult,
    NotificationList,
    NotificationOut,
    NotificationPreferenceOut,
    NotificationPreferenceUpdate,
    UnreadCount,
)
from app.services import notifications, usage_events

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _preference_out(pref) -> NotificationPreferenceOut:
    return NotificationPreferenceOut(
        enabled=pref.enabled,
        muted_kinds=list(pref.muted_kinds or []),
        available_kinds=list(NOTIFICATION_KINDS),
    )


@router.get("", response_model=NotificationList)
def list_notifications(
    response: Response,
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=notifications.MAX_LIST),
    offset: int = Query(default=0, ge=0, le=MAX_DB_INT),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NotificationList:
    rows = notifications.list_for(
        db, user.id, unread_only=unread_only, limit=limit, offset=offset
    )
    matched = notifications.total_count(db, user.id, unread_only=unread_only)
    set_page_headers(response, total=matched, returned=len(rows), offset=offset)
    return NotificationList(
        items=[NotificationOut.model_validate(r) for r in rows],
        unread=notifications.unread_count(db, user.id),
        # The body's ``total`` stays the *unfiltered* count, which is what the
        # bell renders and what every existing client reads. ``X-Total-Count``
        # is the filtered one — the two are different questions and now have
        # different names.
        total=notifications.total_count(db, user.id),
    )


@router.get("/unread", response_model=UnreadCount)
def unread(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> UnreadCount:
    return UnreadCount(unread=notifications.unread_count(db, user.id))


@router.get("/preferences", response_model=NotificationPreferenceOut)
def get_preferences(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> NotificationPreferenceOut:
    return _preference_out(notifications.get_preference(db, user))


@router.put("/preferences", response_model=NotificationPreferenceOut)
def update_preferences(
    payload: NotificationPreferenceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NotificationPreferenceOut:
    """Change the master switch, the muted list, or both.

    ``exclude_unset`` rather than ``exclude_none``: a client sending
    ``muted_kinds: []`` means "mute nothing", which is a real and different
    instruction from not mentioning the field at all.
    """
    pref = notifications.get_preference(db, user)
    fields = payload.model_dump(exclude_unset=True)
    for name, value in fields.items():
        if value is not None:
            setattr(pref, name, value)
    # One event name for every settings surface in the product, with `scope`
    # saying which screen. A name per screen would make "how many people change
    # settings at all" a sum somebody has to remember to compute, and would grow
    # the vocabulary by one every time a preferences form is added — which is
    # the cost that stops anyone instrumenting the next one.
    usage_events.record(
        db,
        "settings.changed",
        user_id=user.id,
        scope="notifications",
        sections=sorted(fields),
    )
    db.commit()
    db.refresh(pref)
    return _preference_out(pref)


@router.post("/read-all", response_model=MarkAllReadResult)
def read_all(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> MarkAllReadResult:
    return MarkAllReadResult(marked=notifications.mark_all_read(db, user.id))


@router.post("/{notification_id}/read", response_model=NotificationOut)
def read_one(
    notification_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NotificationOut:
    row = notifications.mark_read(db, user.id, notification_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    return NotificationOut.model_validate(row)


@router.delete("", response_model=DismissAllResult)
def dismiss_all(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> DismissAllResult:
    return DismissAllResult(dismissed=notifications.dismiss_all(db, user.id))


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
def dismiss(
    notification_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    if not notifications.dismiss(db, user.id, notification_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
