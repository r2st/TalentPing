"""Google OAuth 2.0 helper for connecting a candidate's Gmail account.

Drives the authorization-code flow that yields a long-lived refresh token, so
TalentPing can send outreach *as the candidate* — the mail inherits their
domain's SPF/DKIM/DMARC alignment, which is what keeps cold outreach out of
spam.

Scopes requested:
    openid / email / profile  — to identify which account connected
    gmail.send                — send outreach on the candidate's behalf
    gmail.readonly            — poll threads for recruiter replies

``state`` is a signed, time-limited token (encrypted with the same Fernet key as
stored credentials) so the public callback can be validated — and linked back to
the user who started the flow — without any server-side session storage.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.services import crypto

logger = logging.getLogger(__name__)

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# What we ask for is not what we get. Google's consent screen lets a user untick
# individual permissions, and an OAuth client can be registered with a narrower
# set than the request names — in which case Google issues the grant it is
# willing to issue and says so only in the token response's ``scope`` field.
#
# These two are the product. Without ``gmail.send`` no outreach leaves and no
# reply is answered; without ``gmail.readonly`` no reply is ever read. The
# identity scopes are deliberately not here: they have already proved themselves
# by the time we look, because the userinfo call that names the account is what
# the exchange does next.
REQUIRED_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)

# Google's own words on the consent screen, near enough that someone re-reading
# it can find the checkbox they missed.
_SCOPE_LABELS = {
    "https://www.googleapis.com/auth/gmail.send": "send email on your behalf",
    "https://www.googleapis.com/auth/gmail.readonly": "read your email",
}


def missing_scopes(granted: str | None) -> list[str]:
    """Which required scopes this grant does *not* carry.

    *granted* is the space-separated ``scope`` from the token response. An empty
    or absent one yields every required scope rather than none: a grant that
    cannot be shown to include a permission has not been shown to include it,
    and the alternative reading is what let a mailbox that could not send be
    stored as connected.
    """
    held = set((granted or "").split())
    return [scope for scope in REQUIRED_SCOPES if scope not in held]


def describe_scopes(scopes: list[str]) -> str:
    """Name some scopes the way the consent screen does, for an error page."""
    labels = [_SCOPE_LABELS.get(scope, scope) for scope in scopes]
    if len(labels) <= 1:
        return "".join(labels)
    return f"{', '.join(labels[:-1])} and {labels[-1]}"

_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"
_REVOKE_URI = "https://oauth2.googleapis.com/revoke"

# Maximum age of an OAuth ``state`` token, in seconds.
_STATE_TTL = 600


class OAuthConfigError(RuntimeError):
    """Raised when Google OAuth env vars are not configured."""


class OAuthError(RuntimeError):
    """Raised when the OAuth exchange fails."""


@dataclass
class ConnectedAccount:
    """Result of a successful OAuth exchange."""

    email: str
    google_sub: str
    display_name: str | None
    refresh_token: str | None
    access_token: str
    expires_in: int
    scopes: str


def credential_scopes_for(stored_scopes: str | None) -> list[str]:
    """Scopes to request when refreshing a connected account's access token.

    Google rejects a refresh with ``invalid_scope`` if *any* requested scope was
    not part of the original grant, so we replay exactly what the account
    granted at connect time (recorded on the row) rather than the current
    :data:`SCOPES` superset — which may have grown since.
    """
    if stored_scopes and stored_scopes.strip():
        return stored_scopes.split()
    return list(SCOPES)


def is_configured() -> bool:
    """True when both the Google client credentials and a token key are set."""
    return bool(
        settings.google_client_id
        and settings.google_client_secret
        and crypto.is_configured()
    )


def _require_config() -> None:
    if not (settings.google_client_id and settings.google_client_secret):
        raise OAuthConfigError(
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET (see docs/GOOGLE-OAUTH-SETUP.md)."
        )


def issue_state(user_id: int | str | None = None) -> str:
    """Create a signed, time-limited state token for CSRF protection.

    When *user_id* is provided it is embedded in the (encrypted) state so the
    public OAuth callback can link the connected account to the user who started
    the flow — without trusting any client-supplied value.
    """
    payload: dict[str, object] = {"n": uuid.uuid4().hex, "t": int(time.time())}
    if user_id is not None:
        payload["u"] = str(user_id)
    return crypto.encrypt(json.dumps(payload))


def read_state(state: str) -> dict | None:
    """Decode and validate a state token, returning its payload or ``None``.

    Returns ``None`` when the token is missing, tampered with, or expired.

    Three very different things produce that ``None``, and the caller has to
    treat all three the same way — reject the callback — but an operator does
    not:

    * a forged or tampered token, which is the CSRF attempt this exists to stop;
    * an expired one, which is a user who left the consent screen open;
    * **a decrypt that cannot work at all**, because ``TOKEN_ENCRYPTION_KEY``
      is not the key the token was sealed with.

    The third is a deployment-wide outage — every mailbox connection fails,
    forever, for everybody — and it presented as exactly the same silent
    ``None`` as a stray bad request. So it is logged, at a level that matches
    what it means, while the return value stays identical: nothing about the
    behaviour of this function changes, only whether it says anything on the
    way out.

    The token itself is never logged. It is a CSRF secret, and it carries the
    user id of whoever started the flow.
    """
    if not state:
        return None
    try:
        payload = json.loads(crypto.decrypt(state))
    except Exception as exc:  # noqa: BLE001 - any failure is a rejected callback
        # WARNING, not exception(): a forged token is a normal thing for a
        # public endpoint to receive and its traceback is noise. The type is
        # what separates the two cases — a key mismatch fails the same way on
        # every callback, so a run of identical warnings here is the signature
        # of a rotated key rather than of an attacker.
        logger.warning(
            "oauth state could not be read (%s); if every callback is failing, "
            "check TOKEN_ENCRYPTION_KEY",
            type(exc).__name__,
        )
        return None
    if (time.time() - payload.get("t", 0)) > _STATE_TTL:
        logger.info("oauth state expired before the callback arrived")
        return None
    return payload


def build_authorization_url(state: str) -> str:
    """Build the Google consent-screen URL to redirect the user to."""
    _require_config()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        # offline + consent guarantees we receive a refresh_token even when
        # re-connecting an already-authorized account. select_account forces
        # Google's account chooser even when the browser already has a signed-in
        # session — without it, "Add another mailbox" silently re-consents
        # whichever account is already connected instead of offering a new one.
        "access_type": "offline",
        "prompt": "select_account consent",
        "include_granted_scopes": "true",
    }
    return f"{_AUTH_URI}?{urlencode(params)}"


def exchange_code(code: str) -> ConnectedAccount:
    """Exchange an authorization code for tokens and the user's identity."""
    _require_config()
    with httpx.Client(timeout=15) as client:
        token_resp = client.post(
            _TOKEN_URI,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            logger.error("Token exchange failed: %s", token_resp.text)
            raise OAuthError(f"Token exchange failed: {token_resp.status_code}")
        tok = token_resp.json()

        access_token = tok["access_token"]
        userinfo_resp = client.get(
            _USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"}
        )
        if userinfo_resp.status_code != 200:
            logger.error("Userinfo fetch failed: %s", userinfo_resp.text)
            raise OAuthError("Could not fetch Google account profile")
        info = userinfo_resp.json()

    email = info.get("email")
    sub = info.get("sub")
    if not email or not sub:
        raise OAuthError("Google did not return an email/sub for the account")

    return ConnectedAccount(
        email=email,
        google_sub=sub,
        display_name=info.get("name"),
        refresh_token=tok.get("refresh_token"),
        access_token=access_token,
        expires_in=int(tok.get("expires_in", 3600)),
        # No default. Assuming we were granted everything we asked for is the
        # assumption that has to be checked, not the one to fall back on — and
        # ``credential_scopes_for`` still treats an empty string as "replay the
        # superset", so nothing downstream behaves differently for it.
        scopes=tok.get("scope", ""),
    )


# There is deliberately no ``refresh_access_token`` here.
#
# There was one — a hand-rolled POST to the token endpoint — and nothing had
# called it since ``gmail_service`` started building ``google.oauth2.Credentials``
# and letting google-auth mint tokens lazily. Leaving it was worse than
# unhelpful, because it collapsed the one distinction that matters on this
# endpoint: it raised the same ``OAuthError`` whether Google answered
# ``invalid_grant`` (the grant is gone, the mailbox has to be connected again)
# or 503 (try later). ``gmail_service._is_invalid_grant`` keeps those apart, and
# marking a healthy mailbox revoked because Google had a bad minute is exactly
# the failure that check exists to prevent. Refresh belongs behind
# ``_execute``; anything that needs a token should go through it.


def revoke_token(token: str) -> bool:
    """Best-effort revocation of a token at Google. Returns True on success."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                _REVOKE_URI,
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001 - revocation is best-effort
        # The type, not only the message: a network timeout here is routine and
        # a `TypeError` is a bug in the caller, and stringified they are two
        # sentences that read alike. The traceback stays off — this fires on
        # ordinary disconnects and is not a fault of ours.
        logger.warning(
            "Token revocation failed: %s: %s", type(exc).__name__, exc
        )
        return False
