"""Gmail connection endpoints — step 1 of the wizard, one click.

    GET    /gmail/authorize  -> { authorization_url }   (auth required)
    GET    /gmail/callback   -> upserts the account, closes the popup (public)
    GET    /gmail/status     -> connection status + connected accounts
    PATCH  /gmail/accounts/{id}       -> move the sending identity here
    DELETE /gmail/accounts/{id}       -> disconnect (revokes the grant at Google)
    POST   /gmail/accounts/{id}/watch -> instant replies for this mailbox
    DELETE /gmail/accounts/{id}/watch -> back to polling this mailbox

A user may connect several mailboxes, so everything past ``/accounts/`` names
the one it acts on. ``POST/DELETE /gmail/watch`` survive as primary-only aliases
for older clients.

The callback is public by necessity — Google redirects the browser there with no
Authorization header. It is safe because the signed, time-limited ``state``
carries the id of the user who started the flow.
"""
from __future__ import annotations

import html
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pii import mask_email
from app.core.rate_limit import ip_rate_limit
from app.models.gmail_account import GmailAccount
from app.models.user import User
from app.schemas.gmail import (
    AuthorizeResponse,
    GmailAccountOut,
    GmailAccountUpdate,
    GmailStatus,
    GmailWatchOut,
)
from app.services import (
    crypto,
    gmail_accounts,
    gmail_push,
    google_oauth,
    reputation_service,
)
from app.tasks import inbox_tasks

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/gmail", tags=["gmail"])

# The OAuth redirect is unauthenticated — Google sends the browser here with no
# session of ours attached — so a limit keyed on the caller's address is the
# only kind it can carry.
#
# Its real gate is the signed `state`, and that gate holds: `read_state` rejects
# anything it did not sign, before a database row is read or a request leaves
# for Google. What it does not bound is *replay* inside the state's own
# lifetime. One captured redirect, looped, is a token exchange against Google
# per iteration from an endpoint anyone can reach, and Google's own rate
# limiting is then the only thing standing between that loop and the
# deployment's OAuth client being throttled for every user.
#
# Set well above real use: a person connecting a mailbox completes this once,
# and an administrator connecting several does it a handful of times in a
# sitting.
_callback_limit = ip_rate_limit(30, 300, scope="gmail-oauth-callback")


# What Google's redirect ``error`` codes actually mean for this app, in words a
# candidate can act on.
#
# Every one of these is a *console* problem, not a code problem: the scopes
# TalentPing needs (``gmail.send``, ``gmail.readonly``) are classed sensitive and
# restricted, so until the OAuth client is published and verified Google only
# lets allowlisted test users through — and it enforces that at its own screen,
# before the browser is ever redirected here. The generic "Google returned an
# error: access_denied" this replaced sent people looking for a bug in the app.
#
# See docs/GOOGLE-OAUTH-SETUP.md § "This app is blocked" for the fix.
_OAUTH_ERROR_HELP = {
    "access_denied": (
        "Google blocked the connection. This usually means the consent screen "
        "was dismissed, or the app hasn't completed Google's OAuth verification "
        "for the Gmail permissions it needs. If the app is still in Testing mode, "
        "only addresses on the test-user list may connect. Ask the administrator "
        "to add your Google account as a test user in the Google Cloud Console, "
        "or to complete the OAuth verification process."
    ),
    "admin_policy_enforced": (
        "Your Google Workspace administrator blocks third-party apps from "
        "accessing Gmail. They need to allow this app's client ID before it can "
        "connect."
    ),
    "org_internal": (
        "This OAuth client only accepts accounts inside its own Google "
        "Workspace organisation. A personal Gmail address cannot connect until "
        "the client's user type is set to External."
    ),
    "disallowed_useragent": (
        "Google refused the browser this popup opened in. Try connecting from a "
        "normal browser window rather than an in-app or embedded one."
    ),
}


def _oauth_error_message(error: str) -> str:
    """Turn Google's redirect error code into something worth reading."""
    return _OAUTH_ERROR_HELP.get(
        error, f"Google returned an error: {error}"
    )


def _callback_html(ok: bool, message: str) -> str:
    """The page Google redirects the browser to when consent is done.

    It has to serve two arrivals that look nothing alike, and serving only the
    first is why this flow could not be completed from a phone at all.

    **A popup that has an opener** — the desktop case. It posts the outcome back
    to the app and closes itself, which is what the Setup page's ``message``
    listener is waiting for.

    **A tab with no opener** — every phone. ``window.open`` on a browser that
    ignores window features gives a plain tab, and inside an installed PWA
    (``display: standalone`` in the manifest) it launches a *separate browser
    app*: ``window.opener`` is null there, ``postMessage`` reaches nobody, and
    ``window.close()`` silently refuses on a window that script did not open. So
    the connection would succeed at Google, be stored here, and the app would sit
    on "Waiting for Google…" until it timed out into an error about the mailbox
    not being connected — while the mailbox *was* connected. Reported as "I am
    unable to connect from my mobile app".

    With no opener the browser is sent back to the app instead, carrying the
    outcome in the query string, and :func:`Setup` reads it on arrival.

    The link is not a fallback for style. ``window.close()`` failing leaves the
    user on a dead-end page with no route back into the app — on a phone there
    is no other tab to switch to — so the destination is always on screen and
    always tappable, whatever the script does.
    """
    origin = settings.frontend_url.rstrip("/")
    outcome = "connected" if ok else "error"
    safe = html.escape(message)
    # Built here rather than in the template so the escaping is unambiguous:
    # this string goes into both an HTML attribute and a JS string literal.
    back = f"{origin}/setup?gmail={outcome}"
    if not ok:
        back += f"&reason={quote(message[:300])}"
    # Two encodings of one URL, and they are not interchangeable. The attribute
    # needs HTML escaping, which turns `&` into `&amp;` — correct in markup and
    # wrong inside a JS string, where it would be sent literally and the second
    # query parameter would arrive named `amp;reason`. ``json.dumps`` gives the
    # JS literal, quotes included.
    safe_back = html.escape(back, quote=True)
    js_back = json.dumps(back)
    js_origin = json.dumps(origin)
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Gmail connection</title>
  </head>
  <body style="font-family:system-ui,sans-serif;margin:0;padding:2rem 1.25rem;
               text-align:center;color:#111">
    <p style="font-size:1.05rem;line-height:1.5;max-width:34rem;margin:0 auto 1.5rem">{safe}</p>
    <p style="margin:0">
      <a href="{safe_back}"
         style="display:inline-block;min-height:44px;line-height:44px;padding:0 1.5rem;
                border-radius:10px;background:#f0b429;color:#0a0a0b;font-weight:600;
                text-decoration:none">Back to AutoApply</a>
    </p>
    <script>
      (function () {{
        var payload = {{ source: "doaide-gmail-oauth", status: "{outcome}" }};
        var opener = null;
        try {{ opener = window.opener; }} catch (e) {{}}
        if (opener) {{
          try {{ opener.postMessage(payload, {js_origin}); }} catch (e) {{}}
          setTimeout(function () {{ window.close(); }}, 1200);
          return;
        }}
        // No opener: this is a phone, or an installed PWA that launched a
        // separate browser. Go back to the app rather than asking someone to
        // close a window that will not close.
        window.location.replace({js_back});
      }})();
    </script>
  </body>
</html>"""


@router.get("/authorize", response_model=AuthorizeResponse)
def authorize(user: User = Depends(get_current_user)) -> AuthorizeResponse:
    """Return the Google consent-screen URL for connecting a Gmail account.

    The caller's id is embedded in the signed ``state`` so the public callback
    can link the connected account back to them.
    """
    if not google_oauth.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Gmail connection is not configured on this server "
                "(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / TOKEN_ENCRYPTION_KEY)."
            ),
        )
    state = google_oauth.issue_state(user_id=user.id)
    return AuthorizeResponse(
        authorization_url=google_oauth.build_authorization_url(state), state=state
    )


@router.get(
    "/callback",
    response_class=HTMLResponse,
    dependencies=[Depends(_callback_limit)],
)
def oauth_callback(
    db: Session = Depends(get_db),
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    """Handle Google's redirect: exchange the code and store the connection."""
    if error:
        # Logged at warning because the actionable ones are all deployment
        # configuration, and nothing else records that a user tried and bounced.
        logger.warning("gmail oauth: Google refused the grant (%s)", error)
        return HTMLResponse(_callback_html(False, _oauth_error_message(error)))

    payload = google_oauth.read_state(state or "")
    if not code or payload is None:
        return HTMLResponse(
            _callback_html(False, "Invalid or expired authorization request.")
        )

    try:
        user_id = int(payload.get("u", ""))
    except (TypeError, ValueError):
        return HTMLResponse(
            _callback_html(False, "Could not identify the connecting account.")
        )

    user = db.get(User, user_id)
    if user is None:
        return HTMLResponse(_callback_html(False, "Unknown user."))

    try:
        connected = google_oauth.exchange_code(code)
    except google_oauth.OAuthError as exc:
        logger.error("OAuth exchange failed for user %s: %s", user_id, exc)
        return HTMLResponse(_callback_html(False, "Could not complete the connection."))

    # What Google granted, not what we asked for. Checked before a single field
    # is written, so a narrowed re-consent cannot half-overwrite a mailbox that
    # was working — and so a grant that cannot send is never stored as
    # "connected", which is the state this whole screen exists to report.
    #
    # Two ways to arrive here, and the copy has to cover both: the user unticked
    # a permission on the consent screen, or the deployment's OAuth client is
    # registered without the scope, in which case the checkbox was never offered
    # and reconnecting will fail identically until an administrator fixes it.
    missing = google_oauth.missing_scopes(connected.scopes)
    if missing:
        logger.error(
            "gmail oauth: %s granted a scope set missing %s",
            connected.email,
            ", ".join(missing),
        )
        return HTMLResponse(
            _callback_html(
                False,
                f"AutoApply wasn't given permission to "
                f"{google_oauth.describe_scopes(missing)}, so this mailbox "
                "can't be used. Connect again and leave every permission "
                "ticked. If the consent screen never offered it, this server's "
                "Google client is missing the scope — an administrator has to "
                "add it.",
            )
        )

    account = db.scalar(
        select(GmailAccount).where(GmailAccount.google_sub == connected.google_sub)
    )
    if account is not None and account.user_id != user.id:
        return HTMLResponse(
            _callback_html(
                False, "That Gmail account is already connected to another user."
            )
        )

    if account is None:
        account = GmailAccount(
            user_id=user.id,
            email=connected.email,
            google_sub=connected.google_sub,
            # First account connected becomes the sender.
            is_primary=not any(a.status == "connected" for a in user.gmail_accounts),
            refresh_token_encrypted="",
        )
        db.add(account)

    # ``prompt=consent`` should always return a refresh token; keep the previous
    # one if Google ever omits it so a reconnect can't blank out the grant.
    if connected.refresh_token:
        account.refresh_token_encrypted = crypto.encrypt(connected.refresh_token)
        # Stamped with the token, not with the row. A re-consent lands on the
        # existing row, so `created_at` is the age of the *mailbox* and this is
        # the age of the *grant* — and on a deployment whose consent screen is
        # unpublished, only the second one predicts when sending stops.
        account.granted_at = datetime.now(UTC)
    elif not account.refresh_token_encrypted:
        db.rollback()
        return HTMLResponse(
            _callback_html(
                False,
                "Google did not return a refresh token. Remove DoAide AutoApply at "
                "myaccount.google.com/permissions, then connect again.",
            )
        )

    # Carry the ramp across a reconnection. Matching on ``google_sub`` above
    # keeps a re-consent on the existing row, but a mailbox that was *removed*
    # and added back arrives here as a new row with a null warm-up clock — and
    # so does every mailbox on a deployment whose Google OAuth client is
    # replaced, which is not a rare event. The address's reputation with the
    # receiving servers did not reset, so neither should the allowance: without
    # this, a mailbox with weeks of history drops to 5/day and the queue behind
    # it stops draining. Only ever ages the clock backwards, and only on sends
    # this address really made — see ``reputation_service.adopt_send_history``.
    first_sent_at, sent_count = gmail_accounts.observed_send_history(
        db, user.id, connected.email
    )
    if reputation_service.adopt_send_history(account, first_sent_at, sent_count):
        logger.info(
            "gmail oauth: %s adopted %s prior send(s) since %s for the warm-up ramp",
            connected.email,
            sent_count,
            first_sent_at,
        )

    account.email = connected.email
    account.display_name = connected.display_name or user.full_name
    account.access_token_encrypted = crypto.encrypt(connected.access_token)
    account.token_expiry = datetime.now(UTC) + timedelta(
        seconds=connected.expires_in
    )
    account.scopes = connected.scopes
    account.status = "connected"

    # The first mailbox back from the dead takes the sending identity, because
    # the flag can otherwise be left on a row that is still revoked.
    # ``mark_revoked`` puts it there deliberately when it has nothing live to
    # promote to — it is what names the mailbox to fix first — and re-consenting
    # to a *different* address does not move it. The user is then sending from a
    # mailbox nothing marks as the sender: with two mailboxes and one alive, the
    # "sends from" chip appears on neither, and ``primary_gmail`` is answering
    # from its positional fallback rather than from a choice anybody made.
    #
    # Deliberately conditioned on there being no other live mailbox at all,
    # rather than on no live mailbox holding the flag. Both readings fix the
    # dead-row case; only this one cannot reach a state where a mailbox that is
    # currently sending loses the identity to one just reconnected.
    if not any(a.status == "connected" for a in user.gmail_accounts if a is not account):
        for sibling in user.gmail_accounts:
            if sibling is not account:
                sibling.is_primary = False
        account.is_primary = True

    db.commit()

    return HTMLResponse(_callback_html(True, f"Connected {connected.email}."))


def _account_out(account: GmailAccount) -> GmailAccountOut:
    """One mailbox with its own push, warm-up and grant-expiry state attached.

    None of the computed fields can come off the ORM row: ``push_healthy`` is a
    judgement about a lapsed subscription, ``warmup`` is a projection of the
    ramp, and the grant clock combines a deployment fact with the issue date.
    All three are cheap and none touches the database.
    """
    out = GmailAccountOut.model_validate(account)
    out.push_healthy = gmail_push.push_is_healthy(account.watch)
    out.warmup = reputation_service.warmup_progress(account)

    expiry = gmail_accounts.grant_expiry(account)
    if expiry is not None:
        out.grant_expires_at = expiry.expires_at
        out.grant_expires_in_days = expiry.days_left
        out.grant_expiring = expiry.expiring
        out.grant_expiry_reason = expiry.reason
    return out


@router.get("/status", response_model=GmailStatus)
def gmail_status(user: User = Depends(get_current_user)) -> GmailStatus:
    """Whether the user can send yet, and from which addresses."""
    primary = user.primary_gmail
    watch = primary.watch if primary is not None else None
    return GmailStatus(
        configured=google_oauth.is_configured(),
        connected=user.gmail_connected,
        accounts=[_account_out(a) for a in user.gmail_accounts],
        push_configured=gmail_push.is_configured(),
        push_healthy=gmail_push.push_is_healthy(watch),
        watch=GmailWatchOut.model_validate(watch) if watch is not None else None,
    )


@router.patch("/accounts/{account_id}", response_model=GmailAccountOut)
def update_account(
    account_id: int,
    payload: GmailAccountUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GmailAccountOut:
    """Move the sending identity to this mailbox.

    Until this existed the only way to change which address outreach argued from
    was to disconnect the one holding the flag — which revoked a working grant
    and threw away its warm-up history to change a preference.

    ``is_primary: false`` is accepted and does nothing: a user with mailboxes has
    exactly one primary, so demoting one without naming its replacement has no
    coherent result. Naming the replacement is the whole operation.
    """
    if payload.is_primary:
        try:
            account = gmail_accounts.set_primary(db, user, account_id)
        except LookupError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Gmail account not found"
            ) from None
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from None
        db.commit()
        db.refresh(account)
        return _account_out(account)

    account = db.get(GmailAccount, account_id)
    if account is None or account.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gmail account not found"
        )
    return _account_out(account)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(
    account_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Disconnect an account and best-effort revoke the grant at Google."""
    account = db.get(GmailAccount, account_id)
    if account is None or account.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gmail account not found"
        )
    # Removing the user's last sending identity while autopilot is running would
    # disarm the product silently: the campaigns stay ACTIVE, the beat keeps
    # ticking, and every send fails for a reason nothing surfaces. An error the
    # user reads now beats outreach that stopped without saying so.
    #
    # A mailbox Google has stopped honouring is not a sending identity, so
    # removing it disarms nothing that was still armed — which is why the guard
    # asks about *this* mailbox first. Keyed only on what the user had left, it
    # fired hardest exactly where it was least true: with every mailbox revoked
    # there are no live others, so each dead row refused to go, and the refusal
    # said "this is your only connected mailbox" about a mailbox that was not
    # connected. Reconnecting is what fixes a dead grant, but a user who wanted
    # the row gone first — a mailbox they no longer own, an address they will
    # not consent from again — had no way to remove it at all, and the × on it
    # looked broken.
    if account.status == gmail_accounts.CONNECTED:
        others = [
            a
            for a in gmail_accounts.live_accounts(user)
            if a.id != account.id
        ]
        if not others and user.autopilot is not None and user.autopilot.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This is your only connected mailbox and autopilot is running. "
                    "Connect another mailbox, or turn autopilot off first."
                ),
            )
    # An undecryptable token means there is nothing to revoke — still drop the row.
    with suppress(crypto.TokenCryptoError):
        google_oauth.revoke_token(crypto.decrypt(account.refresh_token_encrypted))
    was_primary = account.is_primary
    db.delete(account)
    db.flush()

    if was_primary:
        # Promote whatever is left so the user keeps a sending identity.
        replacement = db.scalar(
            select(GmailAccount)
            .where(GmailAccount.user_id == user.id, GmailAccount.status == "connected")
            .order_by(GmailAccount.id)
        )
        if replacement is not None:
            replacement.is_primary = True
    db.commit()


# --------------------------------------------------------------------------- #
# Push notifications                                                           #
# --------------------------------------------------------------------------- #


@router.post("/webhook", status_code=status.HTTP_204_NO_CONTENT)
def pubsub_webhook(
    payload: dict,
    db: Session = Depends(get_db),
    token: str | None = Query(default=None),
) -> Response:
    """Receive a Gmail change notification from Cloud Pub/Sub.

    Public by necessity — Pub/Sub carries no user session — so the shared
    ``token`` query parameter is the only gate, and the handler is deliberately
    incurious beyond it: decode, resolve the mailbox, enqueue, acknowledge.

    **Always 204, even on a bad request.** Pub/Sub retries with backoff on any
    non-2xx, so returning an error for a payload that will never parse turns
    one malformed message into an indefinite retry loop. A rejected
    notification is logged instead; the mailbox it would have covered is still
    polled on the normal schedule, so nothing is lost.
    """
    if not gmail_push.token_is_valid(token):
        logger.warning("gmail webhook: rejected a request with a bad token")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    notification = gmail_push.decode_notification(payload)
    if notification is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    account = gmail_push.account_for_address(db, notification.email_address)
    if account is None:
        logger.info(
            "gmail webhook: no connected account for %s", notification.email_address
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    watch = account.watch
    if watch is None:
        logger.info(
            "gmail webhook: %s has no watch row",
            mask_email(notification.email_address),
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    gmail_push.record_notification(db, watch, notification.history_id)
    db.commit()

    # The fetch is the slow part and Pub/Sub wants a prompt ack, so it goes to a
    # worker. `.delay` falls back to inline execution when no broker is up, which
    # is what keeps this testable and a single-process deploy working.
    inbox_tasks.ingest_push_notification.delay(account.id, notification.history_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _owned_connected(db: Session, user: User, account_id: int) -> GmailAccount:
    """A mailbox of this user's that push can actually be registered on."""
    account = db.get(GmailAccount, account_id)
    if account is None or account.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gmail account not found"
        )
    if account.status != "connected":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{account.email} needs reconnecting first",
        )
    return account


@router.post("/accounts/{account_id}/watch", response_model=GmailWatchOut)
def register_account_watch(
    account_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GmailWatchOut:
    """Turn on push for one named mailbox.

    Push is one subscription per mailbox — the schema has said so since
    ``GmailWatch.gmail_account_id`` was made unique — so a single switch for the
    user was only ever the primary's switch wearing the user's name. A second
    mailbox had no way to get instant replies at all.

    Returns the watch row whether or not registration succeeded: a failed watch
    is a real state the UI should show ("still polling"), not an error that hides
    why replies are arriving slowly.
    """
    account = _owned_connected(db, user, account_id)
    watch = gmail_push.start_watch(db, account)
    db.commit()
    db.refresh(watch)
    return GmailWatchOut.model_validate(watch)


@router.delete("/accounts/{account_id}/watch", status_code=status.HTTP_204_NO_CONTENT)
def unregister_account_watch(
    account_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Turn push off for one mailbox and go back to polling it."""
    account = db.get(GmailAccount, account_id)
    if account is None or account.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gmail account not found"
        )
    gmail_push.stop_watch(db, account)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# The two aliases below act on the primary. Kept because an older client still
# calls them and a deploy should not break push mid-flight; new callers should
# name the mailbox.


@router.post("/watch", response_model=GmailWatchOut)
def register_watch(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GmailWatchOut:
    """Turn on push for the caller's primary mailbox."""
    account = user.primary_gmail
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect a Gmail account before enabling push",
        )
    watch = gmail_push.start_watch(db, account)
    db.commit()
    db.refresh(watch)
    return GmailWatchOut.model_validate(watch)


@router.delete("/watch", status_code=status.HTTP_204_NO_CONTENT)
def unregister_watch(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Turn push off and go back to polling."""
    account = user.primary_gmail
    if account is not None:
        gmail_push.stop_watch(db, account)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
