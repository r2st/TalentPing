"""Authentication routes: register, login, password change, session revocation,
downloading the account, and closing it.

The last two are newer than the rest and are the two halves of one obligation.
``DELETE /auth/me`` is the only irreversible thing here — see
:mod:`app.services.account_deletion` for what it erases. ``GET /auth/me/export``
is what makes closing an account something other than losing everything; see
:mod:`app.services.account_export`, and ``docs/PII-INVENTORY.md`` for why both
had to exist.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.emails import normalize_address
from app.core.rate_limit import ip_rate_limit, rate_limit
from app.core.security import (
    create_access_token,
    hash_password,
    spend_verify_cost,
    verify_password,
)
from app.models.user import ROLE_ADMIN, User
from app.schemas.auth import (
    AccountDeletion,
    PasswordChange,
    Token,
    UserCreate,
    UserOut,
)
from app.services import account_deletion, account_export, usage_events

router = APIRouter(prefix="/auth", tags=["auth"])

# Keyed on the caller's address, not on a user: these are the two routes in the
# app with nobody authenticated yet, which is exactly why the per-user limiter
# could never cover them. Separate scopes so a burst of failed logins cannot
# consume the registration budget and lock out signup.
#
# Passed as callables rather than values. This module is imported once, at
# startup, so reading `settings.login_rate_limit` here would bake in whatever
# the setting was at import and ignore every later change to it.
_login_limit = ip_rate_limit(
    lambda: settings.login_rate_limit,
    lambda: settings.login_rate_window_seconds,
    scope="auth:login",
)
_register_limit = ip_rate_limit(
    lambda: settings.register_rate_limit,
    lambda: settings.register_rate_window_seconds,
    scope="auth:register",
)
# Per user, not per address: the caller here is authenticated, and it is the
# account that needs guarding — a stolen token can otherwise grind the current
# password out of this endpoint one guess at a time.
_password_change_limit = rate_limit(
    lambda: settings.password_change_rate_limit,
    lambda: settings.password_change_rate_window_seconds,
    scope="auth:password",
)
# Its own scope, and a tight one. An export is the most expensive read in the
# product — every row this account owns, in one request — and it is a read
# nobody performs twice in a minute for a reason. Three an hour is generous for
# the human use ("download my data") and useless as a way to make one account's
# tables everybody's problem.
_export_limit = rate_limit(3, 3600, scope="auth:export")


def user_for_address(db: Session, address: str | None) -> User | None:
    """The account for *address*, ignoring case.

    New rows are lower-cased by ``UserCreate``, so the exact match is the whole
    story for anyone who signed up after that landed — and it is the branch that
    uses ``uq_users_email``. The fold is for the rows written before it: an
    account stored as ``Jane@acme.com`` has to keep answering to the address its
    owner types, whichever way they type it, or securing this bug would lock out
    exactly the users it was hurting.

    Ordered by ``id`` so that when two legacy rows differ only in case, the
    answer is stable rather than whatever the planner returned first — and it is
    the older account, which is the one with the history on it.
    """
    normalized = normalize_address(address)
    if not normalized:
        return None
    exact = db.scalar(select(User).where(User.email == normalized))
    if exact is not None:
        return exact
    return db.scalars(
        select(User).where(func.lower(User.email) == normalized).order_by(User.id)
    ).first()


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_register_limit)],
)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    if user_for_address(db, payload.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two signups for one address landing together: the check above is a
        # read, `uq_users_email` is what actually decides it, and the loser used
        # to surface as a 500 on the one endpoint a new user meets first. The
        # answer is the same one the read would have given a moment later.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from None
    db.refresh(user)
    return user


@router.post(
    "/login", response_model=Token, dependencies=[Depends(_login_limit)]
)
def login(
    form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
) -> Token:
    # OAuth2PasswordRequestForm uses ``username``; we treat it as the email.
    # Matched without regard to case: the form is free text with no `EmailStr`
    # in front of it, phone keyboards capitalise the first letter, and an
    # address is not case-sensitive to any human who owns one. An exact-match
    # lookup answered `Jane@acme.com` typed as `jane@acme.com` with the same
    # 401 as a wrong password — indistinguishable from the user's side, and
    # nothing on the server recorded that the address had in fact been found.
    user = user_for_address(db, form.username)
    if user is None:
        # Pay the hash anyway. Returning here the instant the lookup missed made
        # the 401 for an unregistered address arrive a bcrypt-verify sooner than
        # the 401 for a registered one — an account-existence oracle measurable
        # from anywhere, on an endpoint whose whole job is to give nothing away.
        # See ``security.spend_verify_cost``.
        spend_verify_cost()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    _apply_admin_bootstrap(db, user)
    token = create_access_token(user.id, token_version=user.token_version)
    return Token(access_token=token)


def _apply_admin_bootstrap(db: Session, user: User) -> None:
    """Promote an ``ADMIN_EMAILS`` address to administrator, once, at sign-in.

    Deliberately one-directional. Promotion happens here so the first admin on a
    deployment costs an environment variable rather than an operator running SQL
    against production — which is the same "SSH onto the box to change a value"
    loop the credentials screen exists to close, moved one step earlier.

    Demotion does *not* happen here, and that asymmetry is the point. If this
    reconciled both ways, a typo in ``ADMIN_EMAILS`` — or a deploy that simply
    forgot to carry it — would strip the last administrator at their next login
    and lock the deployment out of the screen that fixes it. Removing an admin
    is therefore an explicit write through the admin API, where it is authorized
    and attributable.
    """
    if user.is_admin or user.email.lower() not in settings.bootstrap_admin_emails:
        return
    user.role = ROLE_ADMIN
    db.commit()
    db.refresh(user)


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.post(
    "/password",
    response_model=Token,
    dependencies=[Depends(_password_change_limit)],
)
def change_password(
    payload: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Token:
    """Change the password and end every session that predates the change.

    The session invalidation is the point. Changing a password is what someone
    does *after* they think their account was reached — a laptop left open, a
    token pasted somewhere it shouldn't have been — and a change that leaves
    the attacker's existing token working for another day is barely a change at
    all. Bumping ``token_version`` refuses every token minted under the old
    password from the next request onward.

    That includes the caller's own token, which is why a fresh one is returned:
    the client swaps it in and stays signed in on this device only. A 204 here
    would log the user out of the very session they used to secure the account.
    """
    if not verify_password(payload.current_password, current_user.hashed_password):
        # Deliberately not the same message as a bad login. The caller has
        # already proved they hold a token for this account, so there is
        # nothing left to disclose by naming which field was wrong — and a
        # generic error here reads as "the change failed", which sends people
        # to support instead of to their password manager.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )
    if verify_password(payload.new_password, current_user.hashed_password):
        # Not merely pedantic: this would otherwise revoke every session on the
        # account without changing the credential that leaked, which is the
        # worst of both outcomes.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from the current one",
        )

    current_user.hashed_password = hash_password(payload.new_password)
    current_user.token_version += 1
    db.commit()
    db.refresh(current_user)

    return Token(
        access_token=create_access_token(
            current_user.id, token_version=current_user.token_version
        )
    )


@router.post(
    "/logout-all",
    response_model=Token,
    dependencies=[Depends(_password_change_limit)],
)
def logout_all(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Token:
    """Revoke every existing session, keeping only the caller signed in.

    The same mechanism as a password change, minus the password. Someone who
    still knows their password but has lost a device needs to cut the device
    off, and until this existed the only way to do that was to change a
    password that was never compromised.
    """
    current_user.token_version += 1
    db.commit()
    db.refresh(current_user)

    return Token(
        access_token=create_access_token(
            current_user.id, token_version=current_user.token_version
        )
    )


@router.get("/me/export", dependencies=[Depends(_export_limit)])
def export_me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Download everything on this account, as one JSON document.

    The counterpart to ``DELETE /auth/me``, and the reason that endpoint is a
    door rather than a cliff. Until this existed, the only complete copy of a
    candidate's resumes, parsed career history, intents, verdicts and
    correspondence was the one on this deployment — readable a screen at a time
    through the UI, and only for as long as the account stayed open.

    **No password prompt, deliberately**, which is the one place this and
    deletion part company. Both are reached with a token, and the question each
    asks is different: deletion re-authenticates because the scenario worth
    defending against is a stolen token *destroying* an account, and that cannot
    be walked back. A read cannot. Everything in this file is already reachable
    with the same token through the ordinary endpoints — this one is a
    convenience over reads the caller can already perform, so a password gate
    would buy no protection while standing between a user and their own data at
    the moment they are most likely to be locked out of remembering it.

    What it does not carry is named in the document itself: the credentials in
    :data:`~app.services.account_export.REDACTED_COLUMNS`, and the file bytes,
    which are described with the path that returns them.

    Streamed. See :func:`~app.services.account_export.stream_export` — an
    account with a few thousand emails is tens of megabytes of body text, and
    assembling that whole in memory is how a rarely-used feature becomes an
    outage.
    """
    filename = account_export.export_filename(current_user)
    # Recorded before the stream starts, and committed here: the generator runs
    # after the response has begun, by which point a write of ours would be
    # racing the session this request is about to close.
    usage_events.record(
        db, "account.exported", user_id=current_user.id, commit=True
    )
    return StreamingResponse(
        account_export.stream_export(db, current_user),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The document is this account's, and no cache — shared, browser, or
            # otherwise — has any business holding a second copy of it.
            "Cache-Control": "no-store",
        },
    )


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_password_change_limit)],
)
def delete_me(
    payload: AccountDeletion,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Close the account and erase everything belonging to it.

    The gap this fills was not subtle. Every *part* of an account could be
    deleted — a resume, a mailbox, a contact and its whole correspondence — and
    the account itself could not, so a user asking to leave had to be told no,
    and their resumes, their parsed career history and every message they had
    exchanged stayed on this deployment indefinitely. That is the state a
    product is in when it can only be joined.

    Re-authentication is required, and is the reason this is not simply a
    ``DELETE`` with an empty body: the caller already holds a token, and the
    scenario worth defending against is that the token is not theirs. A
    password prompt is the difference between "someone stole a session" and
    "someone destroyed an account".

    What goes is documented in :mod:`app.services.account_deletion` — the
    short version is everything with the user's id on it, plus the Google
    grants revoked upstream first, minus usage counters which survive with the
    user detached. 204, because there is nothing left to return.
    """
    if not verify_password(payload.password, current_user.hashed_password):
        # The same message and the same cost as a wrong password on the change
        # endpoint. Deletion must not be the cheap oracle that tells an
        # attacker holding a token whether a guessed password is right.
        spend_verify_cost()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Password is incorrect",
        )
    account_deletion.delete_account(db, current_user)
