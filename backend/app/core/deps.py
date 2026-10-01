"""Shared FastAPI dependencies (current-user resolution)."""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.logging import bind_request_fields
from app.core.security import decode_access_token
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_v1_prefix}/auth/login")

_credentials_exc = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

# Distinguished from the above so a client can tell "this token is junk" from
# "this token was valid and has been deliberately cut off", and send the user to
# sign in again rather than to a support page. Still a 401, and still says
# nothing to a caller who did not already hold a token for this account.
_revoked_exc = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Session has been revoked; sign in again",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    """Resolve the authenticated user from the bearer token."""
    claims = decode_access_token(token)
    if claims is None:
        raise _credentials_exc
    try:
        user_id = int(claims.subject)
    except (TypeError, ValueError) as exc:
        raise _credentials_exc from exc
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _credentials_exc
    # The revocation check. A token signed under an older generation of this
    # account's credentials is refused for whatever remains of its lifetime —
    # without this, changing a password leaves every stolen token working for
    # up to `access_token_expire_minutes`, which is a day by default.
    if claims.token_version != user.token_version:
        raise _revoked_exc
    # Every authenticated request passes through here exactly once, which makes
    # it the one place that can put the caller on the request's log lines
    # without a route naming it. Bound after the refusals on purpose: a token
    # that failed to authenticate has not established whose it is, and stamping
    # the claimed subject onto the log of a rejected request would attribute a
    # forged token's activity to the account it was forged against.
    bind_request_fields(user_id=user.id)
    return user


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    """Resolve the caller and refuse anyone who is not an administrator.

    A 403, not a 404. Hiding the existence of ``/admin`` from a signed-in,
    non-admin user buys nothing — the frontend bundle names every route it can
    reach — and answering "not found" to someone who *is* an admin but whose
    role failed to load turns an authorization bug into a routing mystery.

    Layered on :func:`get_current_user` rather than re-deriving the token, so
    the revocation check and the inactive-account check apply here too. An
    administrator whose sessions were revoked must lose the admin surface at
    the same instant they lose everything else.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access is required",
        )
    return current_user
