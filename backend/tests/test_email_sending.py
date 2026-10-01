"""Tests for the email sending pipeline: send_outreach_email task,
reputation gate, bounce suppression, and Gmail retry logic.

These tests exercise the most consequential code path in the product —
the one that sends email in the user's name.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.recruiter import Recruiter
from app.models.user import User
from app.services import gmail_service


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(email="test@example.com", hashed_password="x", full_name="Test User")
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture()
def campaign(db_session: Session, user: User) -> Campaign:
    c = Campaign(user_id=user.id, name="Test Campaign", status=CampaignStatus.ACTIVE)
    db_session.add(c)
    db_session.flush()
    return c


@pytest.fixture()
def recruiter(db_session: Session, user: User) -> Recruiter:
    r = Recruiter(user_id=user.id, email="recruiter@acme.com", name="Jane Doe")
    db_session.add(r)
    db_session.flush()
    return r


@pytest.fixture()
def application(db_session: Session, user: User, campaign: Campaign, recruiter: Recruiter) -> Application:
    a = Application(
        user_id=user.id,
        campaign_id=campaign.id,
        recruiter_id=recruiter.id,
        status=ApplicationStatus.QUEUED,
    )
    db_session.add(a)
    db_session.flush()
    return a


@pytest.fixture()
def thread(db_session: Session, application: Application) -> EmailThread:
    t = EmailThread(application_id=application.id)
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture()
def queued_email(db_session: Session, thread: EmailThread) -> Email:
    e = Email(
        thread_id=thread.id,
        direction=EmailDirection.SENT,
        status=EmailStatus.QUEUED,
        to_address="recruiter@acme.com",
        subject="Re: AI Engineer Role",
        body_text="Thank you for reaching out.",
    )
    db_session.add(e)
    db_session.flush()
    return e


# --------------------------------------------------------------------------- #
# send_outreach_email                                                          #
# --------------------------------------------------------------------------- #


class TestSendOutreachEmail:
    """Tests for the Celery send_outreach_email task.

    ``send_outreach_email`` opens its own ``SessionLocal()`` rather than taking
    a session as an argument (it runs from Celery, not a request), so every
    test here points that at the shared in-memory ``db_session`` the same way
    ``test_bounce_handling.py`` does.
    """

    def test_skips_non_queued_email(self, db_session, monkeypatch, queued_email):
        """Emails not in QUEUED status are skipped."""
        from app.tasks import email_tasks

        monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
        queued_email.status = EmailStatus.DRAFT
        db_session.commit()
        result = email_tasks.send_outreach_email(queued_email.id)
        assert result["status"] == "skipped"

    def test_skips_missing_email(self, db_session, monkeypatch):
        """A missing email id returns 'skipped'."""
        from app.tasks import email_tasks

        monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
        result = email_tasks.send_outreach_email(99999)
        assert result["status"] == "skipped"

    def test_paused_campaign_leaves_queued(self, db_session, monkeypatch, queued_email, campaign):
        """Emails in paused campaigns stay QUEUED."""
        from app.tasks import email_tasks

        monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
        campaign.status = CampaignStatus.PAUSED
        db_session.commit()
        result = email_tasks.send_outreach_email(queued_email.id)
        assert result["status"] == "paused"
        db_session.expire_all()
        assert db_session.get(Email, queued_email.id).status == EmailStatus.QUEUED

    def test_bounced_address_fails_send(self, db_session, monkeypatch, queued_email, recruiter):
        """Suppressed (hard-bounced) addresses are not sent to."""
        from app.tasks import email_tasks

        email_id = queued_email.id
        monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
        with patch("app.tasks.email_tasks.bounce_service.is_suppressed", return_value=True):
            result = email_tasks.send_outreach_email(email_id)
        assert result["status"] == "failed"
        assert "hard-bounced" in result["reason"]
        db_session.expire_all()
        assert db_session.get(Email, email_id).status == EmailStatus.FAILED

    def test_gmail_not_configured_fails(self, db_session, monkeypatch, queued_email, user):
        """Gmail not configured results in FAILED status."""
        from app.tasks import email_tasks

        monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
        with (
            patch("app.tasks.email_tasks.bounce_service.is_suppressed", return_value=False),
            patch("app.tasks.email_tasks.gmail_accounts.resolve_for_thread") as mock_resolve,
            patch("app.tasks.email_tasks.reputation_service.evaluate") as mock_rep,
            patch("app.tasks.email_tasks.email_attachments.files_for_email") as mock_files,
            patch(
                "app.tasks.email_tasks.gmail_service.send_email",
                side_effect=gmail_service.GmailNotConfigured("No Gmail"),
            ),
        ):
            mock_account = MagicMock()
            mock_account.id = 1
            mock_account.email = "test@example.com"
            mock_resolve.return_value = mock_account
            mock_rep.return_value = MagicMock(allowed=True)
            mock_files.return_value = MagicMock(
                attachments=[], resume_filename=None, cover_letter_id=None
            )
            result = email_tasks.send_outreach_email(queued_email.id)

        assert result["status"] == "failed"
        assert "No Gmail" in result["reason"]


# --------------------------------------------------------------------------- #
# Gmail retry logic                                                            #
# --------------------------------------------------------------------------- #


class TestGmailRetry:
    """Tests for the _retry_transient helper in gmail_service."""

    def test_retries_on_500(self):
        """Transient 500 errors trigger retries."""
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                exc = gmail_service.HttpError.__new__(gmail_service.HttpError)
                exc.resp = MagicMock(status=500)
                raise exc
            return {"id": "msg123"}

        with patch("app.services.gmail_service.time.sleep"):
            result = gmail_service._retry_transient(flaky, max_retries=3, base_delay=0.01)
        assert result == {"id": "msg123"}
        assert call_count == 3

    def test_does_not_retry_on_400(self):
        """Non-transient errors (400) are raised immediately."""

        def bad_request():
            exc = gmail_service.HttpError.__new__(gmail_service.HttpError)
            exc.resp = MagicMock(status=400)
            raise exc

        with pytest.raises(gmail_service.HttpError):
            gmail_service._retry_transient(bad_request, max_retries=3)

    def test_retries_on_429(self):
        """Rate limit (429) errors trigger retries."""
        call_count = 0

        def rate_limited():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                exc = gmail_service.HttpError.__new__(gmail_service.HttpError)
                exc.resp = MagicMock(status=429)
                raise exc
            return {"id": "msg456"}

        with patch("app.services.gmail_service.time.sleep"):
            result = gmail_service._retry_transient(rate_limited, max_retries=3, base_delay=0.01)
        assert result == {"id": "msg456"}
        assert call_count == 2


# --------------------------------------------------------------------------- #
# Rate limiting                                                                #
# --------------------------------------------------------------------------- #


class TestRateLimit:
    """Tests for the per-user rate limiter.

    The dependency takes the authenticated ``User`` via ``Depends(get_current_user)``
    rather than reading ``request.state`` (nothing in this app ever populates
    that), so tests call the inner check with a ``User`` directly — the same
    shape FastAPI would inject in a real request.
    """

    def test_allows_under_limit(self):
        """Requests under the limit pass through."""
        import asyncio

        from app.core.rate_limit import _windows, rate_limit

        _windows.clear()
        limiter = rate_limit(5, 60)
        user = MagicMock(id=1)

        async def _run():
            for _ in range(5):
                await limiter(user)

        asyncio.run(_run())

    def test_blocks_over_limit(self):
        """Requests over the limit get 429."""
        import asyncio

        from fastapi import HTTPException

        from app.core.rate_limit import _windows, rate_limit

        _windows.clear()
        limiter = rate_limit(2, 60)
        user = MagicMock(id=2)

        async def _run():
            await limiter(user)
            await limiter(user)
            await limiter(user)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(_run())
        assert exc_info.value.status_code == 429


# --------------------------------------------------------------------------- #
# Secret validation                                                            #
# --------------------------------------------------------------------------- #


class TestSecretValidation:
    """Tests for startup secret validation in config."""

    def test_jwt_default_rejected_in_production(self):
        """Default JWT secret is rejected when ENVIRONMENT != development."""
        import os

        from pydantic import ValidationError

        from app.core.config import Settings

        with patch.dict(os.environ, {"ENVIRONMENT": "production", "JWT_SECRET": "change-me-to-a-long-random-string"}):
            with pytest.raises(ValidationError, match="JWT_SECRET must be set"):
                Settings()

    def test_jwt_default_allowed_in_development(self):
        """Default JWT secret is allowed in development."""
        import os

        from app.core.config import Settings

        with patch.dict(os.environ, {"ENVIRONMENT": "development"}):
            s = Settings(jwt_secret="change-me-to-a-long-random-string")
            assert s.jwt_secret == "change-me-to-a-long-random-string"
