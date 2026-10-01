"""Resolve deployment credentials from the database, falling back to the environment.

A row in ``app_credentials`` overrides the environment for its key; no row means
the ``.env`` value stands. That ordering is the whole design — an install with an
empty table behaves exactly as it did before this module existed, and clearing an
override restores the deployed value rather than blanking it.

## Why this hydrates ``Settings`` instead of being a lookup function

Dozens of call sites read ``settings.google_client_id`` or
``settings.openrouter_api_key`` directly — in request handlers, in Celery tasks,
and inside synchronous helpers that build OAuth ``Credentials`` objects
(``google_oauth.exchange_code``, ``gmail_service._service``, the whole
``llm_router`` provider chain). Turning each into a database read would mean
threading a ``Session`` into every one of them, several of which have no session
in hand and no reason to grow one.

So resolution happens **once**, writing onto the ``settings`` singleton that all
of those call sites already read.

## ``_ENV_BASELINE`` is what makes clearing work

The first hydrate snapshots what the environment gave us, because by the second
hydrate the attribute no longer holds the environment's value — it holds
whatever the previous hydrate wrote. Without the snapshot, deleting an override
would "restore" to the value being backed out and leave it in place until the
next restart. That is a failure you would discover while trying to undo a bad
credential, which is the worst possible moment to discover it.

## Propagation is per-process, and it is reported rather than hidden

Hydration writes to one process's ``settings``. The API worker that served the
write has the new value immediately; the other workers and the Celery beat
process pick it up on their next :func:`app.tasks.credential_tasks.refresh_credentials`
tick or on restart. For OAuth client credentials that is the right trade — they
change about once a year, and the alternative is a database read on every send.

What it means in practice: for up to the refresh interval, an in-flight OAuth
callback on another worker can still be using the old client secret.
``GET /admin/credentials`` reports ``applied_at`` for the process that answered,
so an operator can watch the propagation instead of assuming it.

## Rotating the Google client disconnects every mailbox

A refresh token is issued *to* an OAuth client. A new client cannot refresh the
old client's grants, so every connected mailbox must be reconnected, and until
it is, each one fails with ``invalid_grant`` and is marked revoked (see
``gmail_service._execute``). That is correct behaviour that looks exactly like a
mass revocation incident, so the UI says so before the operator saves.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.app_credential import AppCredential
from app.services import crypto

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedCredential:
    """One credential an administrator may set from the dashboard."""

    key: str
    #: Attribute on ``Settings`` this value feeds. Also names the environment
    #: variable — pydantic-settings maps ``GOOGLE_CLIENT_ID`` to
    #: ``google_client_id`` — so the two never drift apart.
    setting: str
    label: str
    category: str
    #: Secret values are never returned in full, only as a masked hint. A client
    #: *id* is not a secret — it appears in the consent URL every user sees — so
    #: showing it whole is what lets an operator confirm which Google project is
    #: live without going to the server, which is the point of the screen.
    secret: bool = True
    help: str = ""


# The allowlist. A key absent from here cannot be written, which is the only
# thing standing between this feature and an arbitrary write primitive over the
# entire `Settings` object — `jwt_secret` and `token_encryption_key` are on that
# object too, and neither should ever be reachable from an HTTP handler.
MANAGED_CREDENTIALS: tuple[ManagedCredential, ...] = (
    ManagedCredential(
        key="google_client_id",
        setting="google_client_id",
        label="Google OAuth Client ID",
        category="Google",
        secret=False,
        help=(
            "From the Google Cloud project's OAuth 2.0 Client. Changing this "
            "invalidates every stored Gmail refresh token — each connected "
            "mailbox has to be reconnected before mail can be read or sent."
        ),
    ),
    ManagedCredential(
        key="google_client_secret",
        setting="google_client_secret",
        label="Google OAuth Client Secret",
        category="Google",
        help="Paired with the client ID above; both must come from the same project.",
    ),
    ManagedCredential(
        key="google_oauth_redirect_uri",
        setting="google_oauth_redirect_uri",
        label="Google OAuth Redirect URI",
        category="Google",
        secret=False,
        help=(
            "Must match a redirect URI registered on the OAuth client exactly, "
            "including scheme and trailing path."
        ),
    ),
    ManagedCredential(
        key="openrouter_api_key",
        setting="openrouter_api_key",
        label="OpenRouter API Key",
        category="AI providers",
        help="First provider in the chain.",
    ),
    ManagedCredential(
        key="gemini_api_key",
        setting="gemini_api_key",
        label="Gemini API Key",
        category="AI providers",
        help="Tried when OpenRouter is unavailable or rate limited.",
    ),
    ManagedCredential(
        key="groq_api_key",
        setting="groq_api_key",
        label="Groq API Key",
        category="AI providers",
    ),
    ManagedCredential(
        key="cerebras_api_key",
        setting="cerebras_api_key",
        label="Cerebras API Key",
        category="AI providers",
        help="Last provider in the chain; below this every feature uses its template.",
    ),
    ManagedCredential(
        key="serpapi_api_key",
        setting="serpapi_api_key",
        label="SerpAPI Key",
        category="Job discovery",
        help="Used for job search discovery when configured.",
    ),
)

BY_KEY: dict[str, ManagedCredential] = {c.key: c for c in MANAGED_CREDENTIALS}

# What the environment gave us, captured before anything overwrites it. See the
# module docstring — this is what "clear the override" restores to.
_ENV_BASELINE: dict[str, str] = {}

#: When *this process* last applied database values. Reported by the API so an
#: operator can see propagation across workers instead of guessing at it.
_last_applied: datetime | None = None


def _capture_env_baseline() -> None:
    """Snapshot the environment's values, once per process."""
    if _ENV_BASELINE:
        return
    for cred in MANAGED_CREDENTIALS:
        _ENV_BASELINE[cred.key] = getattr(settings, cred.setting, "") or ""


def env_value(key: str) -> str:
    """The environment's value for *key*, whatever is currently applied."""
    _capture_env_baseline()
    return _ENV_BASELINE.get(key, "")


def mask(value: str) -> str:
    """A hint that identifies a credential without disclosing it.

    Short values are replaced outright rather than partially shown: revealing
    four characters of a six-character secret is disclosure, not masking.
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{'•' * 8}{value[-4:]}"


def load_overrides(db: Session) -> dict[str, str]:
    """Every stored override, decrypted.

    A row whose ciphertext will not decrypt is skipped rather than raised on.
    The Fernet key having been rotated must not take the process down, and the
    environment value is a working fallback — but it is logged at ERROR, because
    the credential the operator intended is not the one in use.
    """
    out: dict[str, str] = {}
    for row in db.scalars(select(AppCredential)).all():
        if row.key not in BY_KEY:
            # A key retired from the registry. Left in the table — deleting an
            # operator's stored secret is not this function's call — and ignored.
            continue
        try:
            out[row.key] = crypto.decrypt(row.value_encrypted)
        except Exception:  # noqa: BLE001 - a bad row must not dark out the process
            logger.error(
                "Stored credential %s could not be decrypted — falling back to "
                "the environment value. Was TOKEN_ENCRYPTION_KEY rotated?",
                row.key,
            )
    return out


def apply_overrides(overrides: dict[str, str]) -> None:
    """Write *overrides* onto the ``settings`` singleton, restoring the rest."""
    global _last_applied
    _capture_env_baseline()
    for cred in MANAGED_CREDENTIALS:
        value = overrides.get(cred.key)
        # An override present but empty is not an override. Someone who wants
        # the environment value back deletes the row, and a blank string saved
        # by accident must not dark out a working provider key.
        if not value:
            value = _ENV_BASELINE.get(cred.key, "")
        setattr(settings, cred.setting, value)
    _last_applied = datetime.now(UTC)


def hydrate(db: Session) -> dict[str, str]:
    """Load overrides and apply them. Called at startup and on a beat tick."""
    overrides = load_overrides(db)
    apply_overrides(overrides)
    if overrides:
        logger.info(
            "Applied %d credential override(s) from the database: %s",
            len(overrides),
            ", ".join(sorted(overrides)),
        )
    return overrides


def last_applied() -> datetime | None:
    """When this process last applied database values, if ever."""
    return _last_applied


def set_credential(
    db: Session, key: str, value: str, updated_by: str | None = None
) -> AppCredential:
    """Store (or replace) one override and apply it to this process.

    Raises ``KeyError`` for a key outside the registry, and ``ValueError`` for
    an empty value — "set it to nothing" is ambiguous between *clear the
    override* and *there is no credential*, and the caller means the former,
    which is :func:`clear_credential`.
    """
    if key not in BY_KEY:
        raise KeyError(key)
    value = (value or "").strip()
    if not value:
        raise ValueError("value must not be empty")

    row = db.scalar(select(AppCredential).where(AppCredential.key == key))
    if row is None:
        row = AppCredential(key=key)
        db.add(row)
    row.value_encrypted = crypto.encrypt(value)
    row.updated_by = updated_by
    db.commit()
    db.refresh(row)

    hydrate(db)
    # Never the value, not even at DEBUG: logs are shipped and retained.
    logger.info("Credential %s updated by %s", key, updated_by or "unknown")
    return row


def clear_credential(db: Session, key: str, cleared_by: str | None = None) -> bool:
    """Drop the override for *key*, restoring whatever the environment deployed."""
    if key not in BY_KEY:
        raise KeyError(key)
    result = db.execute(delete(AppCredential).where(AppCredential.key == key))
    db.commit()
    hydrate(db)
    removed = bool(result.rowcount)
    if removed:
        logger.info("Credential %s cleared by %s", key, cleared_by or "unknown")
    return removed


def reset_baseline_for_tests() -> None:
    """Forget the snapshot and the applied stamp.

    Exists because ``_ENV_BASELINE`` is a per-process cache that is correct to
    take exactly once in a real worker and wrong to carry between tests, each of
    which sets up its own environment.
    """
    _ENV_BASELINE.clear()
    global _last_applied
    _last_applied = None


__all__ = [
    "BY_KEY",
    "MANAGED_CREDENTIALS",
    "ManagedCredential",
    "apply_overrides",
    "clear_credential",
    "env_value",
    "hydrate",
    "last_applied",
    "load_overrides",
    "mask",
    "reset_baseline_for_tests",
    "set_credential",
]
