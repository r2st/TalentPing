"""The Recruiter Inbox API: listing, counts, preferences, and the ownership wall.

The service tests cover what the pipeline decides. These cover what the API
exposes — and, most importantly, what it refuses to expose: one user must not be
able to read, rematch, answer or dismiss another user's mail, and the refusal is
a 404 rather than a 403 so it doesn't confirm the row exists.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.email import Email, EmailStatus
from app.models.profile import Profile
from app.models.recruiter_email import (
    RecruiterEmail,
    RecruiterEmailKind,
    RecruiterEmailStatus,
    ReplyRoute,
)
from app.models.user import User

API = "/api/v1/recruiter-inbox"


def _detected(
    user_id: int,
    *,
    message_id: str = "m1",
    kind: RecruiterEmailKind = RecruiterEmailKind.RECRUITER_OUTREACH,
    status: RecruiterEmailStatus = RecruiterEmailStatus.FLAGGED,
    route: ReplyRoute | None = ReplyRoute.FLAG,
    sender: str = "alex@northwind.com",
    subject: str = "Senior Backend Engineer at Northwind",
    **extra,
) -> RecruiterEmail:
    return RecruiterEmail(
        user_id=user_id,
        gmail_message_id=message_id,
        gmail_thread_id=f"t-{message_id}",
        from_address=sender,
        from_name="Alex Recruiter",
        subject=subject,
        body_text="Would you be open to a chat about a backend role?",
        snippet="Would you be open to a chat about a backend role?",
        received_at=datetime.now(UTC),
        kind=kind,
        classification_confidence=0.9,
        classified_by="openrouter:test",
        extracted={"role_title": "Senior Backend Engineer", "asks": []},
        status=status,
        route=route,
        route_confidence=63.0,
        **extra,
    )


@pytest.fixture()
def seeded(db_session, current_user) -> list[RecruiterEmail]:
    rows = [
        _detected(current_user.id, message_id="m1"),
        _detected(
            current_user.id,
            message_id="m2",
            status=RecruiterEmailStatus.DRAFTED,
            route=ReplyRoute.DRAFT,
            sender="sam@globex.com",
            subject="Staff Engineer at Globex",
        ),
        _detected(
            current_user.id,
            message_id="m3",
            kind=RecruiterEmailKind.JOB_ALERT,
            status=RecruiterEmailStatus.CLASSIFIED,
            route=None,
            sender="jobalerts-noreply@linkedin.com",
            subject="New jobs for you",
        ),
    ]
    db_session.add_all(rows)
    db_session.commit()
    return rows


@pytest.fixture()
def other_user(db_session, client) -> User:
    """A second registered account, with one detected message of its own."""
    client.post(
        "/api/v1/auth/register",
        json={"email": "rival@example.com", "password": "supersecret123"},
    )
    user = db_session.query(User).filter_by(email="rival@example.com").one()
    db_session.add(_detected(user.id, message_id="theirs-1"))
    db_session.commit()
    return user


# --------------------------------------------------------------------------- #
# Listing                                                                      #
# --------------------------------------------------------------------------- #


def test_list_returns_rows_and_counts(auth_client, seeded):
    body = auth_client.get(API).json()

    assert body["counts"]["detected"] == 3
    assert body["counts"]["needs_you"] == 1
    assert body["counts"]["drafted"] == 1
    assert body["counts"]["not_recruiter"] == 1
    assert body["counts"]["by_kind"]["RECRUITER_OUTREACH"] == 2
    assert len(body["emails"]) == 3


def test_counts_are_computed_before_filters(auth_client, seeded):
    """The chips must keep saying what they would reveal, not what's left."""
    body = auth_client.get(API, params={"view": "needs_you"}).json()

    assert len(body["emails"]) == 1
    assert body["counts"]["detected"] == 3
    assert body["counts"]["drafted"] == 1


def test_search_matches_sender_and_subject(auth_client, seeded):
    assert len(auth_client.get(API, params={"q": "globex"}).json()["emails"]) == 1
    assert len(auth_client.get(API, params={"q": "northwind"}).json()["emails"]) == 1
    assert len(auth_client.get(API, params={"q": "nothing here"}).json()["emails"]) == 0


def test_kind_filter(auth_client, seeded):
    body = auth_client.get(API, params={"kind": "JOB_ALERT"}).json()
    assert len(body["emails"]) == 1
    assert body["emails"][0]["kind"] == "JOB_ALERT"


def test_dismissed_mail_disappears_from_the_list(auth_client, seeded):
    assert auth_client.post(f"{API}/{seeded[0].id}/dismiss").status_code == 204

    body = auth_client.get(API).json()
    assert body["counts"]["detected"] == 2
    assert all(row["id"] != seeded[0].id for row in body["emails"])


def test_detail_includes_the_body(auth_client, seeded):
    body = auth_client.get(f"{API}/{seeded[0].id}").json()

    assert body["body_text"]
    assert body["extracted"]["role_title"] == "Senior Backend Engineer"


def test_mark_read_clears_the_unread_count(auth_client, seeded):
    assert auth_client.get(API).json()["counts"]["unread"] == 3

    auth_client.post(f"{API}/{seeded[0].id}/read")

    assert auth_client.get(API).json()["counts"]["unread"] == 2


# --------------------------------------------------------------------------- #
# The ownership wall                                                           #
# --------------------------------------------------------------------------- #


def test_another_users_mail_is_invisible(auth_client, seeded, other_user, db_session):
    theirs = (
        db_session.query(RecruiterEmail).filter_by(user_id=other_user.id).one()
    )

    assert auth_client.get(f"{API}/{theirs.id}").status_code == 404
    assert auth_client.post(f"{API}/{theirs.id}/read").status_code == 404
    assert auth_client.post(f"{API}/{theirs.id}/dismiss").status_code == 404
    assert auth_client.post(f"{API}/{theirs.id}/rematch", json={}).status_code == 404
    assert (
        auth_client.post(f"{API}/{theirs.id}/generate-reply").status_code == 404
    )
    # And it never leaks into the list.
    ids = {row["id"] for row in auth_client.get(API).json()["emails"]}
    assert theirs.id not in ids


def test_rematch_rejects_a_profile_owned_by_someone_else(
    auth_client, db_session, seeded, other_user
):
    theirs = Profile(user_id=other_user.id, name="Their Profile")
    db_session.add(theirs)
    db_session.commit()

    resp = auth_client.post(
        f"{API}/{seeded[0].id}/rematch", json={"profile_id": theirs.id}
    )

    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Preferences                                                                  #
# --------------------------------------------------------------------------- #


def test_preferences_start_off(auth_client):
    """Watching starts off, so the feature is inert until asked for.

    ``auto_reply_enabled`` starts *on* (it mirrors outreach's ``auto_send``), but
    it cannot act while watching is off and the server switch is off, which is
    what the last two assertions pin down.
    """
    body = auth_client.get(f"{API}/preferences").json()

    assert body["enabled"] is False
    assert body["auto_reply_enabled"] is True
    assert body["server_enabled"] is False
    assert body["server_auto_enabled"] is False


def test_enabling_watching_then_auto_reply(auth_client):
    auth_client.patch(f"{API}/preferences", json={"enabled": True})
    body = auth_client.patch(
        f"{API}/preferences", json={"auto_reply_enabled": True}
    ).json()

    assert body["enabled"] is True
    assert body["auto_reply_enabled"] is True


def test_turning_watching_on_arms_auto_reply_by_default(auth_client):
    """One switch, both behaviours — the point of the default being on."""
    body = auth_client.patch(f"{API}/preferences", json={"enabled": True}).json()

    assert body["enabled"] is True
    assert body["auto_reply_enabled"] is True


def test_auto_reply_can_be_declined_while_watching(auth_client):
    """Opting out of unreviewed replies must not cost you inbox watching."""
    auth_client.patch(f"{API}/preferences", json={"enabled": True})
    body = auth_client.patch(
        f"{API}/preferences", json={"auto_reply_enabled": False}
    ).json()

    assert body["enabled"] is True
    assert body["auto_reply_enabled"] is False


def test_auto_reply_cannot_be_armed_without_watching(auth_client):
    """Arming the inner switch alone would be a trap for later."""
    body = auth_client.patch(
        f"{API}/preferences", json={"auto_reply_enabled": True}
    ).json()

    assert body["enabled"] is False
    assert body["auto_reply_enabled"] is False


def test_turning_watching_off_disarms_auto_reply(auth_client):
    auth_client.patch(f"{API}/preferences", json={"enabled": True})
    auth_client.patch(f"{API}/preferences", json={"auto_reply_enabled": True})

    body = auth_client.patch(f"{API}/preferences", json={"enabled": False}).json()

    assert body["enabled"] is False
    assert body["auto_reply_enabled"] is False


# --------------------------------------------------------------------------- #
# Scanning                                                                     #
# --------------------------------------------------------------------------- #


def test_scan_requires_the_server_switch(auth_client, connected_gmail):
    resp = auth_client.post(f"{API}/scan")

    assert resp.status_code == 409
    assert "not enabled" in resp.json()["detail"]


def test_scan_requires_a_connected_mailbox(auth_client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "recruiter_reply_enabled", True)

    resp = auth_client.post(f"{API}/scan")

    assert resp.status_code == 409
    assert "Gmail" in resp.json()["detail"]


def test_inline_scan_reports_real_counts(
    auth_client, db_session, current_user, connected_gmail, monkeypatch
):
    """CELERY_ENABLED is false in tests, so the button runs the scan inline."""
    from app.core.config import settings
    from app.services import gmail_service, inbound_scanner

    from tests.test_recruiter_reply import gmail_message

    monkeypatch.setattr(settings, "recruiter_reply_enabled", True)
    mailbox = {"m9": gmail_message("m9")}
    monkeypatch.setattr(
        inbound_scanner.gmail_service,
        "list_messages",
        lambda a, q, *, max_results=50: [
            {"id": m["id"], "threadId": m["threadId"]} for m in mailbox.values()
        ],
    )
    monkeypatch.setattr(
        inbound_scanner.gmail_service, "get_message", lambda a, mid: mailbox[mid]
    )
    assert gmail_service is not None  # imported for the patch target's sake

    body = auth_client.post(f"{API}/scan").json()

    assert body["dispatched"] is False
    assert body["detected"] == 1
    assert db_session.query(RecruiterEmail).filter_by(
        gmail_message_id="m9"
    ).count() == 1


# --------------------------------------------------------------------------- #
# Generating a reply for a flagged message                                     #
# --------------------------------------------------------------------------- #


def test_generate_reply_on_a_flagged_message_produces_a_draft(
    auth_client, db_session, current_user, resume, seeded
):
    """The escape hatch: we weren't confident, the user disagrees."""
    resp = auth_client.post(f"{API}/{seeded[0].id}/generate-reply")

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply_email_id"] is not None
    assert body["reply_body"]

    reply = db_session.get(Email, body["reply_email_id"])
    # Always a draft, whatever the original confidence was.
    assert reply.status is EmailStatus.DRAFT


def test_generate_reply_is_refused_twice(auth_client, resume, seeded):
    auth_client.post(f"{API}/{seeded[0].id}/generate-reply")

    resp = auth_client.post(f"{API}/{seeded[0].id}/generate-reply")

    assert resp.status_code == 409


def test_generate_reply_is_refused_for_a_job_alert(auth_client, resume, seeded):
    resp = auth_client.post(f"{API}/{seeded[2].id}/generate-reply")

    assert resp.status_code == 409
    assert "recruiter" in resp.json()["detail"]


def test_rematch_can_force_a_profile(
    auth_client, db_session, current_user, resume, seeded
):
    chosen = Profile(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Platform Engineer",
        target_roles=["Platform Engineer"],
        skills=["kubernetes", "terraform"],
        is_active=True,
    )
    db_session.add(chosen)
    db_session.commit()

    body = auth_client.post(
        f"{API}/{seeded[0].id}/rematch", json={"profile_id": chosen.id}
    ).json()

    assert body["matched_profile_id"] == chosen.id
    assert body["matched_profile_name"] == "Platform Engineer"
    assert body["match_score"] is not None


def test_dismiss_discards_an_unsent_draft(
    auth_client, db_session, current_user, resume, seeded
):
    detail = auth_client.post(f"{API}/{seeded[0].id}/generate-reply").json()
    email_id = detail["reply_email_id"]

    auth_client.post(f"{API}/{seeded[0].id}/dismiss")

    assert db_session.get(Email, email_id) is None


# --------------------------------------------------------------------------- #
# What the drafted reply says it will carry                                    #
# --------------------------------------------------------------------------- #


def test_a_drafted_reply_names_the_resume_it_will_send(
    auth_client, db_session, current_user, resume, seeded
):
    """The user's complaint, exactly: a reply was drafted and no attachment shown.

    Resolution happens at send time by design, so the draft carried no filename
    anywhere the UI could reach — leaving "the resume is queued" and "there is no
    resume" looking identical on screen.
    """
    auth_client.post(f"{API}/{seeded[0].id}/generate-reply")

    body = auth_client.get(f"{API}/{seeded[0].id}").json()

    assert body["reply_attachments"], "a drafted reply should name its resume"
    assert body["reply_attachments"][0].endswith(".pdf")
    assert body["reply_attachment_note"] is None


def test_a_draft_with_no_resume_says_why_rather_than_showing_nothing(
    auth_client, db_session, current_user, resume, seeded, monkeypatch
):
    from app.services import resume_pdf

    auth_client.post(f"{API}/{seeded[0].id}/generate-reply")
    monkeypatch.setattr(resume_pdf, "is_available", lambda: False)

    body = auth_client.get(f"{API}/{seeded[0].id}").json()

    assert body["reply_attachments"] == []
    assert "PDF rendering is unavailable" in body["reply_attachment_note"]


def test_a_message_with_no_reply_has_nothing_to_attach(auth_client, seeded):
    body = auth_client.get(f"{API}/{seeded[0].id}").json()

    assert body["reply_attachments"] == []
    assert body["reply_attachment_note"] is None


def test_the_detail_exposes_where_the_reply_is_addressed(
    auth_client, db_session, current_user
):
    """The From is who wrote; the Reply-To is who gets answered. The UI needs both."""
    row = _detected(
        current_user.id,
        message_id="m-platform",
        sender="noreply@gem.example.com",
        reply_to_address="alex@northwind.com",
    )
    db_session.add(row)
    db_session.commit()

    body = auth_client.get(f"{API}/{row.id}").json()

    assert body["from_address"] == "noreply@gem.example.com"
    assert body["reply_to_address"] == "alex@northwind.com"


def test_the_scan_result_carries_the_query_it_ran(
    auth_client, db_session, current_user, connected_gmail, monkeypatch
):
    """"Why is that email missing?" is answered by the query more than the filters."""
    from app.core.config import settings
    from app.services import inbound_scanner

    monkeypatch.setattr(settings, "recruiter_reply_enabled", True)
    monkeypatch.setattr(
        inbound_scanner.gmail_service, "list_messages", lambda a, q, *, max_results=50: []
    )

    body = auth_client.post(f"{API}/scan").json()

    assert body["query"] == inbound_scanner.build_query()
    assert "in:inbox" not in body["query"]
