"""Tests for production-hardening: rate limiting on write endpoints and
tightened Pydantic schema constraints.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from starlette.datastructures import Headers

from app.core.rate_limit import _windows, ip_rate_limit, rate_limit
from app.schemas.autopilot import AutopilotUpdate
from app.schemas.board import BoardMoveRequest
from app.schemas.campaign import CampaignCreate
from app.schemas.notification import NotificationPreferenceUpdate
from app.schemas.profile import ProfileCreate, ProfileUpdate


def _fake_request(ip: str) -> MagicMock:
    r = MagicMock()
    r.client.host = ip
    r.headers = Headers(raw=[])
    return r


# ------------------------------------------------------------------ #
# Rate limiting: per-user write limit                                #
# ------------------------------------------------------------------ #


class TestWriteRateLimit:
    """The per-user rate_limit factory used by board, profiles, follow-ups,
    notifications, review, and tracker write endpoints."""

    def setup_method(self):
        _windows.clear()

    def test_allows_under_limit(self):
        limiter = rate_limit(5, 60, scope="test-write")
        user = MagicMock(id=99)

        async def _run():
            for _ in range(5):
                await limiter(user)

        asyncio.run(_run())

    def test_blocks_over_limit(self):
        from fastapi import HTTPException

        limiter = rate_limit(2, 60, scope="test-write-block")
        user = MagicMock(id=100)

        async def _run():
            await limiter(user)
            await limiter(user)
            await limiter(user)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(_run())
        assert exc_info.value.status_code == 429

    def test_scopes_are_independent(self):
        limit_a = rate_limit(1, 60, scope="scope-a")
        limit_b = rate_limit(1, 60, scope="scope-b")
        user = MagicMock(id=101)

        async def _run():
            await limit_a(user)
            await limit_b(user)

        asyncio.run(_run())

    def test_users_are_independent(self):
        limiter = rate_limit(1, 60, scope="test-user-sep")
        user_a = MagicMock(id=200)
        user_b = MagicMock(id=201)

        async def _run():
            await limiter(user_a)
            await limiter(user_b)

        asyncio.run(_run())


# ------------------------------------------------------------------ #
# Rate limiting: per-IP tracking limit                               #
# ------------------------------------------------------------------ #


class TestTrackingRateLimit:
    """The ip_rate_limit factory used by the public tracking endpoints."""

    def setup_method(self):
        _windows.clear()

    def test_allows_under_limit(self):
        limiter = ip_rate_limit(5, 60, scope="test-tracking")
        request = _fake_request("10.0.0.1")
        response = MagicMock(spec=["headers"])
        response.headers = {}

        async def _run():
            for _ in range(5):
                await limiter(request, response)

        asyncio.run(_run())

    def test_blocks_over_limit(self):
        from fastapi import HTTPException

        limiter = ip_rate_limit(2, 60, scope="test-tracking-block")
        request = _fake_request("10.0.0.2")
        response = MagicMock(spec=["headers"])
        response.headers = {}

        async def _run():
            await limiter(request, response)
            await limiter(request, response)
            await limiter(request, response)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(_run())
        assert exc_info.value.status_code == 429

    def test_different_ips_independent(self):
        limiter = ip_rate_limit(1, 60, scope="test-tracking-ips")

        async def _run():
            for ip in ("10.0.0.3", "10.0.0.4"):
                request = _fake_request(ip)
                response = MagicMock(spec=["headers"])
                response.headers = {}
                await limiter(request, response)

        asyncio.run(_run())


# ------------------------------------------------------------------ #
# Rate limiting: callable-based settings                             #
# ------------------------------------------------------------------ #


class TestCallableSettings:
    """rate_limit and ip_rate_limit accept callables for max_requests and
    window_seconds, so the value is read at request time rather than frozen
    at import time."""

    def setup_method(self):
        _windows.clear()

    def test_callable_limits_are_evaluated_per_call(self):
        counter = {"max": 100}
        limiter = rate_limit(lambda: counter["max"], 60, scope="test-callable")
        user = MagicMock(id=300)

        async def _run():
            await limiter(user)
            counter["max"] = 1
            await limiter(user)

        with pytest.raises(Exception):
            asyncio.run(_run())


# ------------------------------------------------------------------ #
# Schema: BoardMoveRequest                                          #
# ------------------------------------------------------------------ #


class TestBoardMoveRequestSchema:
    def test_valid(self):
        m = BoardMoveRequest(stage="interview")
        assert m.stage == "interview"

    def test_empty_stage_rejected(self):
        with pytest.raises(ValidationError):
            BoardMoveRequest(stage="")

    def test_oversized_stage_rejected(self):
        with pytest.raises(ValidationError):
            BoardMoveRequest(stage="x" * 51)

    def test_note_max_length(self):
        m = BoardMoveRequest(stage="ok", note="a" * 255)
        assert len(m.note) == 255
        with pytest.raises(ValidationError):
            BoardMoveRequest(stage="ok", note="a" * 256)


# ------------------------------------------------------------------ #
# Schema: ProfileBase / ProfileCreate / ProfileUpdate list items     #
# ------------------------------------------------------------------ #


class TestProfileSchemaConstraints:
    def test_target_roles_item_max_length(self):
        with pytest.raises(ValidationError):
            ProfileCreate(name="test", target_roles=["x" * 201])

    def test_target_roles_item_min_length(self):
        with pytest.raises(ValidationError):
            ProfileCreate(name="test", target_roles=[""])

    def test_target_roles_valid(self):
        m = ProfileCreate(name="test", target_roles=["Backend Engineer"])
        assert m.target_roles == ["Backend Engineer"]

    def test_target_industries_item_bounds(self):
        m = ProfileCreate(name="test", target_industries=["Tech"])
        assert m.target_industries == ["Tech"]
        with pytest.raises(ValidationError):
            ProfileCreate(name="test", target_industries=["x" * 201])
        with pytest.raises(ValidationError):
            ProfileCreate(name="test", target_industries=[""])

    def test_skills_item_bounds(self):
        m = ProfileCreate(name="test", skills=["Python"])
        assert m.skills == ["Python"]
        with pytest.raises(ValidationError):
            ProfileCreate(name="test", skills=["x" * 201])

    def test_location_preferences_item_bounds(self):
        m = ProfileCreate(name="test", location_preferences=["NYC"])
        assert m.location_preferences == ["NYC"]
        with pytest.raises(ValidationError):
            ProfileCreate(name="test", location_preferences=["x" * 201])

    def test_update_target_roles_item_bounds(self):
        m = ProfileUpdate(target_roles=["Engineer"])
        assert m.target_roles == ["Engineer"]
        with pytest.raises(ValidationError):
            ProfileUpdate(target_roles=["x" * 201])
        with pytest.raises(ValidationError):
            ProfileUpdate(target_roles=[""])

    def test_list_max_length_enforced(self):
        with pytest.raises(ValidationError):
            ProfileCreate(name="test", target_roles=["ok"] * 13)


# ------------------------------------------------------------------ #
# Schema: CampaignCreate list items                                  #
# ------------------------------------------------------------------ #


class TestCampaignSchemaConstraints:
    def test_target_companies_item_max_length(self):
        with pytest.raises(ValidationError):
            CampaignCreate(
                target_companies=["x" * 201],
                target_industries=["Tech"],
            )

    def test_target_companies_item_min_length(self):
        with pytest.raises(ValidationError):
            CampaignCreate(
                target_companies=[""],
                target_industries=["Tech"],
            )

    def test_target_roles_item_bounds(self):
        m = CampaignCreate(
            target_companies=["Acme"],
            target_roles=["Engineer"],
        )
        assert m.target_roles == ["Engineer"]
        with pytest.raises(ValidationError):
            CampaignCreate(
                target_companies=["Acme"],
                target_roles=["x" * 201],
            )

    def test_target_industries_item_bounds(self):
        m = CampaignCreate(
            target_industries=["Finance"],
        )
        assert m.target_industries == ["Finance"]
        with pytest.raises(ValidationError):
            CampaignCreate(
                target_industries=["x" * 201],
            )

    def test_valid_campaign(self):
        m = CampaignCreate(
            target_companies=["Acme"],
            target_industries=["Tech"],
            target_roles=["Backend"],
        )
        assert m.target_companies == ["Acme"]


# ------------------------------------------------------------------ #
# Schema: AutopilotUpdate list items                                 #
# ------------------------------------------------------------------ #


class TestAutopilotSchemaConstraints:
    def test_target_roles_item_bounds(self):
        m = AutopilotUpdate(target_roles=["Engineer"])
        assert m.target_roles == ["Engineer"]
        with pytest.raises(ValidationError):
            AutopilotUpdate(target_roles=["x" * 201])
        with pytest.raises(ValidationError):
            AutopilotUpdate(target_roles=[""])

    def test_target_industries_item_bounds(self):
        m = AutopilotUpdate(target_industries=["Tech"])
        assert m.target_industries == ["Tech"]
        with pytest.raises(ValidationError):
            AutopilotUpdate(target_industries=["x" * 201])

    def test_locations_item_bounds(self):
        m = AutopilotUpdate(locations=["NYC"])
        assert m.locations == ["NYC"]
        with pytest.raises(ValidationError):
            AutopilotUpdate(locations=["x" * 201])

    def test_outreach_highlights_item_bounds(self):
        m = AutopilotUpdate(outreach_highlights=["Startup exp"])
        assert m.outreach_highlights == ["Startup exp"]
        with pytest.raises(ValidationError):
            AutopilotUpdate(outreach_highlights=["x" * 201])
        with pytest.raises(ValidationError):
            AutopilotUpdate(outreach_highlights=[""])

    def test_list_max_length_enforced(self):
        with pytest.raises(ValidationError):
            AutopilotUpdate(target_roles=["ok"] * 11)


# ------------------------------------------------------------------ #
# Schema: NotificationPreferenceUpdate                               #
# ------------------------------------------------------------------ #


class TestNotificationPreferenceSchema:
    def test_muted_kinds_max_length(self):
        with pytest.raises(ValidationError):
            NotificationPreferenceUpdate(muted_kinds=["a"] * 51)

    def test_muted_kinds_valid(self):
        from app.models.notification import NOTIFICATION_KINDS

        if NOTIFICATION_KINDS:
            kind = list(NOTIFICATION_KINDS)[0]
            m = NotificationPreferenceUpdate(muted_kinds=[kind])
            assert m.muted_kinds == [kind]

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            NotificationPreferenceUpdate(muted_kinds=["not_a_real_kind_xyz"])


# ------------------------------------------------------------------ #
# Config: new rate limit settings                                    #
# ------------------------------------------------------------------ #


class TestRateLimitConfigSettings:
    def test_write_rate_limit_defaults(self):
        from app.core.config import settings

        assert settings.write_rate_limit > 0
        assert settings.write_rate_window_seconds > 0

    def test_tracking_rate_limit_defaults(self):
        from app.core.config import settings

        assert settings.tracking_rate_limit > 0
        assert settings.tracking_rate_window_seconds > 0
