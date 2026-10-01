"""Gmail OAuth: state signing, the callback upsert, and status/disconnect."""
from __future__ import annotations

import json
import time

import pytest

from app.models.gmail_account import GmailAccount
from app.services import crypto, google_oauth
from app.services.google_oauth import ConnectedAccount


@pytest.fixture()
def oauth_configured(monkeypatch):
    """Pretend the server has Google credentials configured."""
    monkeypatch.setattr(google_oauth.settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(google_oauth.settings, "google_client_secret", "test-secret")
    monkeypatch.setattr(
        google_oauth.settings,
        "google_oauth_redirect_uri",
        "http://testserver/api/v1/gmail/callback",
    )


class TestCrypto:
    def test_round_trip(self):
        assert crypto.decrypt(crypto.encrypt("refresh-token")) == "refresh-token"

    def test_tampered_ciphertext_is_rejected(self):
        token = crypto.encrypt("secret")
        with pytest.raises(crypto.TokenCryptoError):
            crypto.decrypt(token[:-4] + "AAAA")


class TestState:
    def test_state_round_trips_the_user_id(self):
        payload = google_oauth.read_state(google_oauth.issue_state(user_id=42))
        assert payload is not None and payload["u"] == "42"

    def test_each_state_is_unique(self):
        assert google_oauth.issue_state(1) != google_oauth.issue_state(1)

    def test_tampered_state_is_rejected(self):
        assert google_oauth.read_state("not-a-real-state") is None
        assert google_oauth.read_state("") is None

    def test_expired_state_is_rejected(self):
        stale = crypto.encrypt(json.dumps({"n": "x", "t": int(time.time()) - 3600}))
        assert google_oauth.read_state(stale) is None

    def test_authorization_url_carries_the_right_params(self, oauth_configured):
        url = google_oauth.build_authorization_url("state-token")
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        # offline + consent is what guarantees a refresh token on reconnect;
        # select_account forces the chooser so a second mailbox can be added.
        assert "access_type=offline" in url
        assert "prompt=select_account+consent" in url
        assert "gmail.send" in url and "gmail.readonly" in url
        assert "state=state-token" in url

    def test_authorization_url_requires_configuration(self, monkeypatch):
        monkeypatch.setattr(google_oauth.settings, "google_client_id", "")
        with pytest.raises(google_oauth.OAuthConfigError):
            google_oauth.build_authorization_url("s")


class TestCredentialScopes:
    def test_recorded_scopes_are_replayed_verbatim(self):
        """Refreshing with scopes the grant never had fails at Google."""
        stored = "openid https://www.googleapis.com/auth/gmail.send"
        assert google_oauth.credential_scopes_for(stored) == stored.split()

    def test_legacy_rows_fall_back_to_the_full_set(self):
        assert google_oauth.credential_scopes_for(None) == google_oauth.SCOPES
        assert google_oauth.credential_scopes_for("  ") == google_oauth.SCOPES


class TestAuthorizeEndpoint:
    def test_returns_a_consent_url(self, auth_client, oauth_configured):
        resp = auth_client.get("/api/v1/gmail/authorize")
        assert resp.status_code == 200
        body = resp.json()
        assert body["authorization_url"].startswith("https://accounts.google.com/")
        assert google_oauth.read_state(body["state"]) is not None

    def test_503_when_the_server_has_no_credentials(self, auth_client, monkeypatch):
        monkeypatch.setattr(google_oauth.settings, "google_client_id", "")
        assert auth_client.get("/api/v1/gmail/authorize").status_code == 503

    def test_requires_auth(self, client):
        assert client.get("/api/v1/gmail/authorize").status_code == 401


class TestCallback:
    def _exchange(self, monkeypatch, *, refresh_token="refresh-abc", sub="sub-1"):
        monkeypatch.setattr(
            google_oauth,
            "exchange_code",
            lambda code: ConnectedAccount(
                email="candidate@gmail.com",
                google_sub=sub,
                display_name="Jordan Candidate",
                refresh_token=refresh_token,
                access_token="access-abc",
                expires_in=3600,
                scopes="openid https://www.googleapis.com/auth/gmail.send",
            ),
        )

    def test_successful_callback_stores_an_encrypted_token(
        self, auth_client, db_session, current_user, monkeypatch, oauth_configured
    ):
        self._exchange(monkeypatch)
        state = google_oauth.issue_state(user_id=current_user.id)

        resp = auth_client.get(f"/api/v1/gmail/callback?code=abc&state={state}")
        assert resp.status_code == 200
        assert "Connected candidate@gmail.com" in resp.text

        account = db_session.query(GmailAccount).one()
        assert account.user_id == current_user.id
        assert account.status == "connected" and account.is_primary is True
        # The refresh token must never be at rest in plaintext.
        assert account.refresh_token_encrypted != "refresh-abc"
        assert crypto.decrypt(account.refresh_token_encrypted) == "refresh-abc"

    def test_reconnect_updates_the_existing_row(
        self, auth_client, db_session, current_user, monkeypatch, oauth_configured
    ):
        state = google_oauth.issue_state(user_id=current_user.id)
        self._exchange(monkeypatch)
        auth_client.get(f"/api/v1/gmail/callback?code=abc&state={state}")

        self._exchange(monkeypatch, refresh_token="refresh-xyz")
        state2 = google_oauth.issue_state(user_id=current_user.id)
        auth_client.get(f"/api/v1/gmail/callback?code=def&state={state2}")

        account = db_session.query(GmailAccount).one()
        assert crypto.decrypt(account.refresh_token_encrypted) == "refresh-xyz"

    def test_a_withheld_refresh_token_keeps_the_stored_one(
        self, auth_client, db_session, current_user, monkeypatch, oauth_configured
    ):
        state = google_oauth.issue_state(user_id=current_user.id)
        self._exchange(monkeypatch)
        auth_client.get(f"/api/v1/gmail/callback?code=abc&state={state}")

        self._exchange(monkeypatch, refresh_token=None)
        state2 = google_oauth.issue_state(user_id=current_user.id)
        auth_client.get(f"/api/v1/gmail/callback?code=def&state={state2}")

        account = db_session.query(GmailAccount).one()
        assert crypto.decrypt(account.refresh_token_encrypted) == "refresh-abc"

    def test_first_connect_without_a_refresh_token_asks_the_user_to_retry(
        self, auth_client, db_session, current_user, monkeypatch, oauth_configured
    ):
        self._exchange(monkeypatch, refresh_token=None)
        state = google_oauth.issue_state(user_id=current_user.id)

        resp = auth_client.get(f"/api/v1/gmail/callback?code=abc&state={state}")
        assert "did not return a refresh token" in resp.text
        assert db_session.query(GmailAccount).count() == 0

    def test_google_error_is_shown_not_raised(self, client):
        resp = client.get("/api/v1/gmail/callback?error=access_denied")
        assert resp.status_code == 200
        assert "access_denied" in resp.text

    def test_missing_or_forged_state_is_refused(self, client):
        resp = client.get("/api/v1/gmail/callback?code=abc&state=forged")
        assert "Invalid or expired" in resp.text

    def test_an_account_owned_by_another_user_is_refused(
        self, auth_client, db_session, current_user, monkeypatch, oauth_configured
    ):
        """The same Gmail must not be hijacked into a second TalentPing account."""
        from app.models.user import User

        other = User(
            email="other@example.com", hashed_password="x", full_name="Other"
        )
        db_session.add(other)
        db_session.commit()
        db_session.add(
            GmailAccount(
                user_id=other.id,
                email="candidate@gmail.com",
                google_sub="sub-1",
                refresh_token_encrypted=crypto.encrypt("theirs"),
                status="connected",
            )
        )
        db_session.commit()

        self._exchange(monkeypatch, sub="sub-1")
        state = google_oauth.issue_state(user_id=current_user.id)
        resp = auth_client.get(f"/api/v1/gmail/callback?code=abc&state={state}")

        assert "already connected to another user" in resp.text
        assert db_session.query(GmailAccount).count() == 1


class TestStatusAndDisconnect:
    def test_status_reports_disconnected_by_default(self, auth_client):
        body = auth_client.get("/api/v1/gmail/status").json()
        assert body["connected"] is False and body["accounts"] == []

    def test_status_lists_a_connected_account(self, auth_client, connected_gmail):
        body = auth_client.get("/api/v1/gmail/status").json()
        assert body["connected"] is True
        assert body["accounts"][0]["email"] == "candidate@gmail.com"

    def test_disconnect_removes_the_account_and_revokes_the_grant(
        self, auth_client, db_session, connected_gmail, monkeypatch
    ):
        revoked: list[str] = []
        monkeypatch.setattr(
            "app.routers.gmail.google_oauth.revoke_token",
            lambda token: revoked.append(token) or True,
        )
        resp = auth_client.delete(f"/api/v1/gmail/accounts/{connected_gmail.id}")
        assert resp.status_code == 204
        assert revoked == ["fake-refresh-token"]
        assert db_session.query(GmailAccount).count() == 0

    def test_disconnecting_the_primary_promotes_another_account(
        self, auth_client, db_session, current_user, connected_gmail, monkeypatch
    ):
        monkeypatch.setattr(
            "app.routers.gmail.google_oauth.revoke_token", lambda token: True
        )
        spare = GmailAccount(
            user_id=current_user.id,
            email="spare@gmail.com",
            google_sub="sub-2",
            refresh_token_encrypted=crypto.encrypt("spare-token"),
            status="connected",
            is_primary=False,
        )
        db_session.add(spare)
        db_session.commit()

        auth_client.delete(f"/api/v1/gmail/accounts/{connected_gmail.id}")
        db_session.refresh(spare)
        assert spare.is_primary is True

    def test_cannot_disconnect_someone_elses_account(self, auth_client, db_session):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.commit()
        account = GmailAccount(
            user_id=other.id,
            email="other@gmail.com",
            google_sub="sub-9",
            refresh_token_encrypted=crypto.encrypt("theirs"),
        )
        db_session.add(account)
        db_session.commit()

        assert auth_client.delete(f"/api/v1/gmail/accounts/{account.id}").status_code == 404
