"""Authentication primitives: password hashing and JWT access tokens."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, NamedTuple

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings


class TokenClaims(NamedTuple):
    """The parts of a validated access token the app actually acts on."""

    subject: str
    token_version: int

# bcrypt operates on at most 72 bytes; longer inputs must be truncated first.
_BCRYPT_MAX_BYTES = 72


def _prepare(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Return a bcrypt hash for a plaintext password.

    The work factor is read at call time rather than captured at import, so a
    change to it takes effect on the next hash. bcrypt stores the cost inside
    the hash, so raising the setting does not invalidate existing passwords —
    they keep verifying at the cost they were written with.
    """
    salt = bcrypt.gensalt(settings.bcrypt_rounds)
    return bcrypt.hashpw(_prepare(password), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(_prepare(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


@lru_cache(maxsize=4)
def _decoy_hash(rounds: int) -> str:
    """A hash of a value nobody holds, at the work factor real hashes use.

    Keyed on *rounds* so that changing ``BCRYPT_ROUNDS`` re-derives it rather
    than leaving a decoy that costs a different amount from the real thing —
    which would reintroduce the very difference it exists to erase. Cached
    because deriving it per call would double the cost of every failed login.
    """
    salt = bcrypt.gensalt(rounds)
    return bcrypt.hashpw(_prepare(secrets.token_urlsafe(32)), salt).decode("utf-8")


def spend_verify_cost() -> None:
    """Do a password check's worth of work, for an account that does not exist.

    bcrypt is deliberately slow — that is the whole point of it — which makes
    *skipping* it loud. ``/auth/login`` answered an unknown address the moment
    the row lookup missed and a known one a hundred-odd milliseconds later,
    once the hash had been computed, and both got the same 401. The status code
    says nothing and the clock says everything: the difference is far larger
    than network jitter, so anyone with a word list could sort it into
    registered and unregistered addresses at the speed of the rate limit.

    That matters more here than on a typical product. The addresses worth
    testing are the ones this application is built around — a candidate's
    personal mailbox, a recruiter's work address — and "does this person use
    TalentPing" is itself the disclosure, before any password is guessed.

    So a miss pays the same cost as a hit. The rate limiter on the route bounds
    how often that can be spent; this bounds what is learned each time.
    """
    verify_password("decoy", _decoy_hash(settings.bcrypt_rounds))


def create_access_token(
    subject: str | int,
    expires_minutes: int | None = None,
    *,
    token_version: int = 0,
) -> str:
    """Create a signed JWT whose ``sub`` claim is the user id.

    ``ver`` pins the token to a generation of the account's credentials. It is
    what lets a password change end sessions that already exist: the token
    stays cryptographically valid, but no longer matches the user row, so it is
    refused. See ``User.token_version``.
    """
    expire = datetime.now(UTC) + timedelta(
        minutes=expires_minutes or settings.access_token_expire_minutes
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "exp": expire,
        "type": "access",
        "ver": token_version,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> TokenClaims | None:
    """Return the claims of a valid token, or ``None`` if it is not one."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("type") != "access":
        return None
    subject = payload.get("sub")
    if subject is None:
        return None

    # Absent on tokens minted before revocation existed; those belong to
    # generation 0, which is what every existing user row says.
    version = payload.get("ver", 0)
    # `bool` is an `int` in Python but `true` in JSON, and a token claiming
    # `"ver": true` would otherwise compare equal to generation 1. A claim that
    # is not a plain integer is not a claim we know how to check, so the token
    # is refused rather than interpreted.
    if isinstance(version, bool) or not isinstance(version, int):
        return None

    return TokenClaims(subject=str(subject), token_version=version)
