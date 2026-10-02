"""Public open/click tracking endpoints.

    GET /t/o/{token}.gif  -> 1x1 pixel, records an open
    GET /t/c/{token}?u=…  -> 302 to the decoded target, records a click

Unauthenticated by necessity: a recruiter's mail client has no session with this
product. Four rules make that safe, and all four are load-bearing:

* **An unknown token is not an error.** Both endpoints return their normal
  response and write nothing. A 404 would turn the endpoint into an oracle for
  guessing valid tokens.
* **The redirect target is validated twice** before it goes into a ``Location``
  header: it must be http(s), *and* it must be one of the links the message
  that token belongs to actually carried. The first rules out ``javascript:``;
  the second is what stops the route being an open redirect on this product's
  own domain. Anything failing either bounces to the app's own frontend.
* **``user_id`` comes from the email row**, never from the request.
* **Writes are counter increments only.** There is no shape of request here that
  reaches another user's data.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import ip_rate_limit
from app.services import email_tracking

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/t", tags=["tracking"])

_tracking_limit = ip_rate_limit(
    lambda: settings.tracking_rate_limit,
    lambda: settings.tracking_rate_window_seconds,
    scope="tracking",
)

# Mail clients cache aggressively; without this a second read never reports.
_NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _pixel() -> Response:
    return Response(
        content=email_tracking.TRANSPARENT_GIF,
        media_type="image/gif",
        headers=_NO_STORE,
    )


@router.get("/o/{token}.gif", dependencies=[Depends(_tracking_limit)])
def track_open(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Record an open and return the pixel. Always returns the pixel."""
    if not settings.email_tracking_enabled:
        return _pixel()

    email = email_tracking.email_for_token(db, token)
    if email is None:
        return _pixel()

    try:
        email_tracking.record_open(
            db,
            email,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        db.commit()
    except Exception:  # noqa: BLE001 - a failed stat must never break the pixel
        logger.exception("failed to record open for email %s", email.id)
        db.rollback()
    return _pixel()


@router.get("/c/{token}", dependencies=[Depends(_tracking_limit)])
def track_click(
    token: str,
    request: Request,
    u: str = Query(default="", description="urlsafe-base64 target"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Record a click and redirect to the decoded target.

    The destination is checked twice, and both checks have to pass before
    anything reaches a ``Location`` header: it must be an http(s) URL, and it
    must be a link the message named by *token* actually carried. The second is
    what keeps this from being an open redirect on the product's own domain —
    see :func:`app.services.email_tracking.is_tracked_target`.

    Neither check is conditional on ``email_tracking_enabled``. That setting
    governs whether a click is *recorded*; turning it off must not turn the
    route into a redirector that follows anything it is handed.
    """
    target = email_tracking.decode_target(u)
    if not email_tracking.is_safe_target(target):
        # Refused rather than followed: this parameter is attacker-reachable,
        # and the frontend is the one destination that is always safe.
        logger.info("tracking: refusing unsafe click target")
        return RedirectResponse(url=settings.frontend_url, status_code=302)

    email = email_tracking.email_for_token(db, token)
    if email is None or not email_tracking.is_tracked_target(email, target):
        # An unknown token has no message to check the target against, so there
        # is nothing here that makes this URL ours to send anyone to. Same
        # answer as an unrecognised target, and the same silence: bouncing to
        # the frontend rather than erroring keeps the endpoint from reporting
        # which tokens exist.
        logger.info("tracking: refusing a click target this message never carried")
        return RedirectResponse(url=settings.frontend_url, status_code=302)

    if settings.email_tracking_enabled:
        try:
            email_tracking.record_click(
                db,
                email,
                target,  # type: ignore[arg-type]  # is_safe_target rules out None
                ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
            db.commit()
        except Exception:  # noqa: BLE001 - never strand the recipient
            logger.exception("failed to record click for email %s", email.id)
            db.rollback()

    return RedirectResponse(url=target, status_code=302, headers=_NO_STORE)
