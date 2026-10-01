"""What happens when Google stops honouring a mailbox's grant.

A refresh token dies for three reasons: the candidate removed TalentPing at
myaccount.google.com/permissions, they changed their password, or the OAuth
client is still in "Testing" publishing status, where Google expires every
refresh token seven days after it is issued.

Whichever it was, google-auth reports it the same way — ``RefreshError`` with
``invalid_grant``, raised lazily from whichever API call happens to run first.
Before this was handled, that meant a fresh traceback on every beat tick while
the row went on claiming ``connected``: the setup page showed a healthy mailbox,
the resolvers kept handing it out as a sender, and nothing in the product ever
said the one thing that would fix it.

These tests pin the two halves of the fix: the error is *recognised* (and only
when it is actually terminal), and recognising it *changes the row*.
"""
from __future__ import annotations

import pytest

from app.models.gmail_account import GmailAccount
from app.services import crypto, gmail_accounts, gmail_service


class FakeRefreshError(Exception):
    """Stands in for google.auth.exceptions.RefreshError."""


@pytest.fixture()
def as_refresh_error(monkeypatch):
    """Make ``_execute`` treat FakeRefreshError the way it treats the real one."""
    monkeypatch.setattr(gmail_service, "RefreshError", FakeRefreshError)


class TestRecognisingADeadGrant:
    def test_invalid_grant_becomes_a_domain_error(self, as_refresh_error):
        def call():
            raise FakeRefreshError(
                "('invalid_grant: Token has been expired or revoked.', "
                "{'error': 'invalid_grant'})"
            )

        with pytest.raises(gmail_service.GmailAuthRevoked):
            gmail_service._execute(call)

    def test_a_transient_refresh_failure_is_left_alone(self, as_refresh_error):
        """Only ``invalid_grant`` is terminal.

        google-auth reports a 5xx from the token endpoint as the same exception
        type. Translating that to "revoked" would disconnect a working mailbox
        because Google had a bad minute.
        """

        def call():
            raise FakeRefreshError("Failed to refresh: 503 Service Unavailable")

        with pytest.raises(FakeRefreshError):
            gmail_service._execute(call)

    def test_a_successful_call_passes_its_value_through(self, as_refresh_error):
        assert gmail_service._execute(lambda: {"id": "abc"}) == {"id": "abc"}

    def test_retries_still_translate(self, as_refresh_error):
        """The retry wrapper runs calls through ``_execute``, not around it.

        Every retried call — sending, listing, fetching a message — would
        otherwise keep raising the raw error.
        """

        def call():
            raise FakeRefreshError("invalid_grant")

        with pytest.raises(gmail_service.GmailAuthRevoked):
            gmail_service._retry_transient(call)


class TestMarkingTheMailbox:
    def test_a_revoked_mailbox_stops_being_connected(
        self, db_session, current_user, connected_gmail
    ):
        gmail_accounts.mark_revoked(db_session, connected_gmail, "invalid_grant")
        db_session.commit()

        assert connected_gmail.status == "revoked"
        # The resolvers are what background work asks "which mailboxes are worth
        # doing work on"; a dead one must not come back.
        assert gmail_accounts.live_accounts(current_user) == []

    def test_the_stale_access_token_is_dropped(
        self, db_session, connected_gmail
    ):
        connected_gmail.access_token_encrypted = crypto.encrypt("stale-access")
        db_session.commit()

        gmail_accounts.mark_revoked(db_session, connected_gmail, "invalid_grant")

        assert connected_gmail.access_token_encrypted is None
        assert connected_gmail.token_expiry is None

    def test_the_refresh_token_is_kept(self, db_session, connected_gmail):
        """Reconnecting overwrites it; blanking it early only loses the audit trail.

        The callback refuses a reconnect that returns no refresh token *unless*
        one is already stored, so clearing it here would turn a recoverable
        reconnect into a dead end.
        """
        gmail_accounts.mark_revoked(db_session, connected_gmail, "invalid_grant")

        assert connected_gmail.refresh_token_encrypted

    def test_marking_twice_is_a_no_op(self, db_session, connected_gmail):
        gmail_accounts.mark_revoked(db_session, connected_gmail, "invalid_grant")
        gmail_accounts.mark_revoked(db_session, connected_gmail, "invalid_grant")

        assert connected_gmail.status == "revoked"

    def test_a_sibling_is_promoted_when_the_primary_dies(
        self, db_session, current_user, connected_gmail
    ):
        """Losing the sending identity should not cost the user the other mailbox."""
        second = GmailAccount(
            user_id=current_user.id,
            email="candidate.two@gmail.com",
            google_sub="test-sub-2",
            refresh_token_encrypted=crypto.encrypt("fake-refresh-token-2"),
            status="connected",
            is_primary=False,
        )
        db_session.add(second)
        db_session.commit()
        db_session.refresh(current_user)

        gmail_accounts.mark_revoked(db_session, connected_gmail, "invalid_grant")
        db_session.commit()
        db_session.refresh(current_user)

        assert second.is_primary is True
        assert connected_gmail.is_primary is False
        assert current_user.primary_gmail is second
        assert current_user.gmail_connected is True

    def test_the_last_mailbox_keeps_the_flag(
        self, db_session, current_user, connected_gmail
    ):
        """With nothing to promote, the row still names which one to reconnect."""
        gmail_accounts.mark_revoked(db_session, connected_gmail, "invalid_grant")
        db_session.commit()
        db_session.refresh(current_user)

        assert connected_gmail.is_primary is True
        assert current_user.gmail_connected is False
        assert current_user.primary_gmail is None


class TestWhatTheUserSees:
    def test_the_status_endpoint_reports_the_revoked_mailbox(
        self, auth_client, db_session, connected_gmail
    ):
        """Still listed, and listed as broken.

        Hiding it would read as "the mailbox is gone"; reporting it connected is
        what the bug was. The setup page keys its reconnect prompt off this.
        """
        gmail_accounts.mark_revoked(db_session, connected_gmail, "invalid_grant")
        db_session.commit()

        body = auth_client.get("/api/v1/gmail/status").json()

        assert body["connected"] is False
        assert [a["status"] for a in body["accounts"]] == ["revoked"]
        assert body["accounts"][0]["email"] == "candidate@gmail.com"

    def test_reconnecting_restores_the_mailbox(
        self, auth_client, db_session, connected_gmail, monkeypatch
    ):
        """The same Google account coming back through the callback revives the row.

        This is the whole recovery path: the callback matches on ``google_sub``,
        so a reconnect updates the existing row rather than colliding with it —
        which is what makes "Reconnect a mailbox" work at all.
        """
        gmail_accounts.mark_revoked(db_session, connected_gmail, "invalid_grant")
        db_session.commit()

        from app.services import google_oauth

        monkeypatch.setattr(
            google_oauth,
            "exchange_code",
            lambda code: google_oauth.ConnectedAccount(
                email="candidate@gmail.com",
                google_sub="test-sub-1",
                display_name="Jordan Candidate",
                refresh_token="fresh-refresh-token",
                access_token="fresh-access-token",
                expires_in=3600,
                scopes="openid https://www.googleapis.com/auth/gmail.send",
            ),
        )
        state = google_oauth.issue_state(user_id=connected_gmail.user_id)

        resp = auth_client.get(
            "/api/v1/gmail/callback", params={"code": "x", "state": state}
        )

        assert resp.status_code == 200
        db_session.expire_all()
        row = db_session.get(GmailAccount, connected_gmail.id)
        assert row.status == "connected"
        assert crypto.decrypt(row.refresh_token_encrypted) == "fresh-refresh-token"
