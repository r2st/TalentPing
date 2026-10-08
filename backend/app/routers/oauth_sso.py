"""OAuth SSO login endpoints: Google, GitHub, Microsoft.

Separate from the Gmail OAuth in ``gmail.py`` which is for sending email on
behalf of the user. This module handles *login* via social providers.
"""
from __future__ import annotations

import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import ip_rate_limit
from app.core.security import create_access_token
from app.models.user import ROLE_USER, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

oauth = OAuth()

if settings.sso_google_client_id:
    oauth.register(
        name="google",
        client_id=settings.sso_google_client_id,
        client_secret=settings.sso_google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

if settings.github_client_id:
    oauth.register(
        name="github",
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        authorize_url="https://github.com/login/oauth/authorize",
        access_token_url="https://github.com/login/oauth/access_token",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "user:email"},
    )

if settings.microsoft_client_id:
    oauth.register(
        name="microsoft",
        client_id=settings.microsoft_client_id,
        client_secret=settings.microsoft_client_secret,
        server_metadata_url="https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

FRONTEND_URL = settings.frontend_url

_oauth_initiate_limit = ip_rate_limit(30, 60, scope="sso_initiate")
_oauth_callback_limit = ip_rate_limit(30, 60, scope="sso_callback")


def _callback_url(provider: str) -> str:
    return f"{settings.oauth_redirect_base}{settings.api_v1_prefix}/auth/sso/{provider}/callback"


def _find_or_create_oauth_user(
    db: Session,
    *,
    provider: str,
    oauth_id: str,
    email: str,
    name: str | None,
    email_verified: bool = False,
) -> User | None:
    user = db.scalar(
        select(User).where(User.oauth_provider == provider, User.oauth_id == oauth_id)
    )
    if user:
        if not user.is_active:
            return None
        return user

    user = db.scalar(select(User).where(User.email == email))
    if user:
        if not user.is_active:
            return None
        if not email_verified:
            logger.warning(
                "SSO link refused: provider did not verify email",
                extra={"provider": provider, "user_id": user.id},
            )
            return None
        if not user.oauth_provider:
            user.oauth_provider = provider
            user.oauth_id = oauth_id
            db.commit()
            db.refresh(user)
        return user

    user = User(
        email=email,
        full_name=name,
        hashed_password="",
        role=ROLE_USER,
        oauth_provider=provider,
        oauth_id=oauth_id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("SSO account created", extra={"provider": provider, "user_id": user.id})
    return user


def _complete_oauth_login(user: User | None, provider: str) -> RedirectResponse:
    if user is None:
        return RedirectResponse(f"{FRONTEND_URL}/?error=account_deactivated")
    access_token = create_access_token(user.id, token_version=user.token_version)
    return RedirectResponse(f"{FRONTEND_URL}/auth/callback#token={access_token}")


# ── Google ────────────────────────────────────────────────

@router.get(
    "/sso/google",
    summary="Initiate Google SSO login",
    dependencies=[Depends(_oauth_initiate_limit)],
)
async def google_login(request: Request):
    if not settings.sso_google_client_id:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Google SSO not configured")
    redirect_uri = _callback_url("google")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get(
    "/sso/google/callback",
    summary="Handle Google SSO callback",
    dependencies=[Depends(_oauth_callback_limit)],
)
async def google_callback(request: Request, db: Session = Depends(get_db)):
    if not settings.sso_google_client_id:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED)
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        logger.exception("Google SSO callback failed")
        return RedirectResponse(f"{FRONTEND_URL}/?error=google_auth_failed")

    userinfo = token.get("userinfo", {})
    email = userinfo.get("email")
    if not email:
        return RedirectResponse(f"{FRONTEND_URL}/?error=no_email")

    user = _find_or_create_oauth_user(
        db,
        provider="google",
        oauth_id=userinfo.get("sub", ""),
        email=email.lower(),
        name=userinfo.get("name"),
        email_verified=bool(userinfo.get("email_verified")),
    )
    return _complete_oauth_login(user, "google")


# ── GitHub ────────────────────────────────────────────────

@router.get(
    "/sso/github",
    summary="Initiate GitHub SSO login",
    dependencies=[Depends(_oauth_initiate_limit)],
)
async def github_login(request: Request):
    if not settings.github_client_id:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="GitHub SSO not configured")
    redirect_uri = _callback_url("github")
    return await oauth.github.authorize_redirect(request, redirect_uri)


@router.get(
    "/sso/github/callback",
    summary="Handle GitHub SSO callback",
    dependencies=[Depends(_oauth_callback_limit)],
)
async def github_callback(request: Request, db: Session = Depends(get_db)):
    if not settings.github_client_id:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED)
    try:
        token = await oauth.github.authorize_access_token(request)
    except Exception:
        logger.exception("GitHub SSO callback failed")
        return RedirectResponse(f"{FRONTEND_URL}/?error=github_auth_failed")

    try:
        resp = await oauth.github.get("user", token=token)
        profile = resp.json()
    except Exception:
        logger.exception("GitHub user profile fetch failed")
        return RedirectResponse(f"{FRONTEND_URL}/?error=github_auth_failed")

    email = profile.get("email")
    if not email:
        try:
            emails_resp = await oauth.github.get("user/emails", token=token)
            for e in emails_resp.json():
                if e.get("primary") and e.get("verified"):
                    email = e["email"]
                    break
        except Exception:
            logger.exception("GitHub user emails fetch failed")
            return RedirectResponse(f"{FRONTEND_URL}/?error=github_auth_failed")

    if not email:
        return RedirectResponse(f"{FRONTEND_URL}/?error=no_email")

    user = _find_or_create_oauth_user(
        db,
        provider="github",
        oauth_id=str(profile.get("id", "")),
        email=email.lower(),
        name=profile.get("name") or profile.get("login"),
        email_verified=True,
    )
    return _complete_oauth_login(user, "github")


# ── Microsoft ─────────────────────────────────────────────

@router.get(
    "/sso/microsoft",
    summary="Initiate Microsoft SSO login",
    dependencies=[Depends(_oauth_initiate_limit)],
)
async def microsoft_login(request: Request):
    if not settings.microsoft_client_id:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Microsoft SSO not configured")
    redirect_uri = _callback_url("microsoft")
    return await oauth.microsoft.authorize_redirect(request, redirect_uri)


@router.get(
    "/sso/microsoft/callback",
    summary="Handle Microsoft SSO callback",
    dependencies=[Depends(_oauth_callback_limit)],
)
async def microsoft_callback(request: Request, db: Session = Depends(get_db)):
    if not settings.microsoft_client_id:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED)
    try:
        token = await oauth.microsoft.authorize_access_token(request)
    except Exception:
        logger.exception("Microsoft SSO callback failed")
        return RedirectResponse(f"{FRONTEND_URL}/?error=microsoft_auth_failed")

    userinfo = token.get("userinfo", {})
    email = userinfo.get("email") or userinfo.get("preferred_username")
    if not email:
        return RedirectResponse(f"{FRONTEND_URL}/?error=no_email")

    user = _find_or_create_oauth_user(
        db,
        provider="microsoft",
        oauth_id=userinfo.get("sub", ""),
        email=email.lower(),
        name=userinfo.get("name"),
        email_verified=bool(userinfo.get("email_verified")),
    )
    return _complete_oauth_login(user, "microsoft")
