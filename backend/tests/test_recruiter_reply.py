"""Inbound recruiter mail: scanning, classifying, matching, routing, replying.

The outbound pipeline has one safety story (don't email the wrong person); this
one has two, because it *reads a whole mailbox* and can *answer a stranger*. The
tests are organised around the second: every band of the routing table, every
fallback that must downgrade rather than send, and the boundary that keeps the
two ingestion paths from handling the same message twice.

Nothing here touches a network. Gmail is stubbed at the ``gmail_service`` module
boundary, and the model chain is unconfigured by ``conftest`` (no API key), so
every LLM call takes its deterministic fallback unless a test says otherwise.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from email import message_from_bytes

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.application import Application
from app.models.campaign import Campaign, CampaignStatus
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.profile import Profile
from app.models.recruiter import Recruiter
from app.models.resume import Resume
from app.models.recruiter_email import (
    RecruiterEmail,
    RecruiterEmailKind,
    RecruiterEmailStatus,
    RecruiterReplyPreference,
    ReplyRoute,
)
from app.services import (
    gmail_service,
    inbound_matcher,
    inbound_reply,
    inbound_scanner,
    recruiter_classifier,
    recruiter_reply_service,
    reply_routing,
    resume_pdf,
)
from app.tasks import email_tasks, recruiter_reply_tasks

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

RECRUITER_BODY = """\
Hi Jordan,

I came across your profile and wanted to reach out about a Senior Backend
Engineer role at Northwind Payments. It's a Python and FastAPI shop running on
AWS, and the team owns payment infrastructure end to end.

Would you be open to a short chat this month? Also, are you open to hybrid in
San Francisco?

Best,
Alex Recruiter
Technical Recruiter, Northwind Payments
"""

JOB_ALERT_BODY = """\
New jobs for you this week

Senior Backend Engineer at Acme
Staff Engineer at Globex
See all 47 matches on LinkedIn.
"""

ATS_BODY = """\
Thank you for applying to Northwind Payments.

We have received your application for Senior Backend Engineer and will be in
touch if there is a match.
"""


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def gmail_message(
    message_id: str,
    *,
    thread_id: str | None = None,
    sender: str = "alex@northwind.com",
    sender_name: str = "Alex Recruiter",
    subject: str = "Senior Backend Engineer at Northwind",
    body: str = RECRUITER_BODY,
    when: datetime | None = None,
    reply_to: str | None = None,
) -> dict:
    """A Gmail message resource, shaped the way the API really returns them."""
    when = when or datetime.now(UTC)
    headers = [
        {"name": "From", "value": f"{sender_name} <{sender}>"},
        {"name": "To", "value": "candidate@gmail.com"},
        {"name": "Subject", "value": subject},
    ]
    if reply_to:
        headers.append({"name": "Reply-To", "value": reply_to})
    return {
        "id": message_id,
        "threadId": thread_id or f"t-{message_id}",
        "internalDate": str(int(when.timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": _b64(body)},
        },
    }


# The `stub_gmail`, `profiles` and `watched` fixtures live in conftest — every
# suite that exercises inbound mail needs the same three.


# --------------------------------------------------------------------------- #
# Scanning                                                                     #
# --------------------------------------------------------------------------- #


def test_scan_detects_new_mail(db_session, current_user, connected_gmail, stub_gmail):
    stub_gmail["m1"] = gmail_message("m1")

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.from_address == "alex@northwind.com"
    assert message.from_name == "Alex Recruiter"
    assert "Northwind Payments" in message.body_text


def test_scan_is_idempotent(db_session, current_user, connected_gmail, stub_gmail):
    """The same message twice is one row — what makes the scan safe to replay."""
    stub_gmail["m1"] = gmail_message("m1")

    recruiter_reply_service.record_scan(db_session, current_user, connected_gmail)
    db_session.commit()
    result, created = recruiter_reply_service.record_scan(
        db_session, current_user, connected_gmail
    )
    db_session.commit()

    assert created == []
    assert result.skipped_known == 1
    assert db_session.query(RecruiterEmail).count() == 1


def test_scan_skips_our_own_threads(
    db_session, current_user, connected_gmail, stub_gmail
):
    """A message on a thread we started belongs to poll_thread, not here.

    This is the boundary between the two ingestion paths. Without it the product
    classifies its own outreach as inbound recruiter mail and replies to itself.
    """
    campaign = Campaign(user_id=current_user.id, name="Outbound")
    db_session.add(campaign)
    db_session.flush()
    recruiter = Recruiter(user_id=current_user.id, email="alex@northwind.com")
    db_session.add(recruiter)
    db_session.flush()
    application = Application(
        user_id=current_user.id, campaign_id=campaign.id, recruiter_id=recruiter.id
    )
    db_session.add(application)
    db_session.flush()
    db_session.add(
        EmailThread(application_id=application.id, gmail_thread_id="ours-1")
    )
    db_session.commit()

    stub_gmail["m1"] = gmail_message("m1", thread_id="ours-1")

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert result.messages == []
    assert result.skipped_own_thread == 1


def test_scan_skips_mail_from_the_user(
    db_session, current_user, connected_gmail, stub_gmail
):
    """Including from a connected mailbox that isn't the login address."""
    stub_gmail["m1"] = gmail_message("m1", sender="candidate@gmail.com")

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert result.messages == []
    assert result.skipped_from_self == 1


# --------------------------------------------------------------------------- #
# Reaching the mail that was being missed                                      #
# --------------------------------------------------------------------------- #


def test_the_query_reads_past_the_inbox_and_every_category():
    """The four filters that were each losing real opportunities are gone.

    Every one of these was removed for the same reason: it decided a message was
    not worth reading on evidence that has nothing to do with whether a recruiter
    wrote it. A Gmail filter that archives mail, a tab Gmail guessed, a phone
    that opened the message first.
    """
    query = inbound_scanner.build_query()

    assert "in:inbox" not in query
    assert "is:unread" not in query
    assert "category:social" not in query
    assert "category:promotions" not in query
    # Still bounded, and still never reading the folders that aren't mail.
    assert f"newer_than:{settings.recruiter_scan_window_days}d" in query
    assert "-in:spam" in query
    assert "-in:trash" in query


def test_the_query_can_be_narrowed_back_to_the_inbox(monkeypatch):
    monkeypatch.setattr(settings, "recruiter_scan_include_archived", False)
    assert "in:inbox" in inbound_scanner.build_query()


def test_a_deployment_can_override_the_query_entirely(monkeypatch):
    monkeypatch.setattr(settings, "recruiter_scan_query", "label:recruiters")
    assert inbound_scanner.build_query() == "label:recruiters"


def test_the_listing_pages_instead_of_truncating_at_gmails_ceiling(monkeypatch):
    """Gmail caps one page at 500 and says nothing about it.

    The scanner asked for more than that and believed it got what it asked for.
    Because Gmail returns newest-first, the truncation fell on the *old* end of
    the window: those ids were never listed, so they were never skipped, never
    deferred and never counted — just gone. They are exactly the messages a user
    notices missing.
    """
    pages = [
        {
            "messages": [{"id": f"a{i}", "threadId": f"t{i}"} for i in range(500)],
            "nextPageToken": "page-2",
        },
        {"messages": [{"id": f"b{i}", "threadId": f"u{i}"} for i in range(40)]},
    ]
    seen: list[dict] = []

    class _Messages:
        def list(self, **kwargs):
            seen.append(kwargs)
            return self

        def execute(self):
            return pages[len(seen) - 1]

    class _Users:
        def messages(self):
            return _Messages()

    monkeypatch.setattr(
        gmail_service, "_service", lambda account: type("S", (), {"users": lambda s: _Users()})()
    )

    result = gmail_service.list_messages(object(), "q", max_results=600)

    assert len(result) == 540
    # Never asks for more than one page can hold, and carries the cursor.
    assert seen[0]["maxResults"] == gmail_service.LIST_PAGE_MAX
    assert seen[0]["pageToken"] is None
    assert seen[1]["pageToken"] == "page-2"


def test_a_lookalike_domain_is_not_mistaken_for_the_user(
    db_session, current_user, connected_gmail, stub_gmail
):
    """"bob@acme.com" is a substring of "bob@acme.com.mx" and not the same person.

    The self-check was a substring match, so a recruiter at a domain that merely
    started with the user's own was silently discarded as the user's own mail —
    a loss with no counter and no log line to explain it.
    """
    stub_gmail["m1"] = gmail_message("m1", sender="candidate@gmail.com.mx")

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert result.skipped_from_self == 0
    assert [m.from_address for m in result.messages] == ["candidate@gmail.com.mx"]


def test_the_scan_reports_the_query_and_everything_it_listed(
    db_session, current_user, connected_gmail, stub_gmail
):
    """Every message leaves through exactly one counter, and the search is on the row.

    "Why is that email missing?" is answered by the query far more often than by
    any filter, and a scan that reports only what it found cannot answer it.
    """
    stub_gmail["m1"] = gmail_message("m1")
    stub_gmail["m2"] = gmail_message("m2", sender="candidate@gmail.com")

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)
    reported = result.as_dict()

    assert reported["listed"] == 2
    assert reported["detected"] == 1
    assert reported["skipped_from_self"] == 1
    assert reported["query"] == inbound_scanner.build_query()
    # The listing is fully accounted for: nothing left silently.
    accounted = reported["detected"] + sum(
        reported[key] for key in reported if key.startswith("skipped_")
    ) + reported["deferred"]
    assert accounted == reported["listed"]


# --------------------------------------------------------------------------- #
# Reply-To: the human behind a no-reply sender                                  #
# --------------------------------------------------------------------------- #


def test_a_reply_to_header_is_captured(
    db_session, current_user, connected_gmail, stub_gmail
):
    stub_gmail["m1"] = gmail_message(
        "m1", sender="noreply@gem.example.com", reply_to="alex@northwind.com"
    )

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert result.messages[0].from_address == "noreply@gem.example.com"
    assert result.messages[0].reply_to_address == "alex@northwind.com"


def test_a_reply_to_that_merely_repeats_the_sender_is_not_stored(
    db_session, current_user, connected_gmail, stub_gmail
):
    """The common case stores nothing, so `reply_address` falls back to the From."""
    stub_gmail["m1"] = gmail_message("m1", reply_to="Alex <alex@northwind.com>")

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert result.messages[0].reply_to_address is None


def test_a_no_reply_sender_with_a_human_reply_to_is_still_read(monkeypatch):
    """Gem, Loxo, Bullhorn and every in-house mail merge send this way.

    The pre-filter condemned the message on an address the recruiter never chose.
    A Reply-To pointing somewhere answerable means a person is behind it, so the
    message goes on to be read properly instead of being filed as noise.
    """
    from app.services.openrouter_client import OpenRouterError

    monkeypatch.setattr(
        recruiter_classifier,
        "chat_completion_detailed",
        lambda *a, **k: (_ for _ in ()).throw(OpenRouterError("429")),
    )

    condemned = recruiter_classifier.classify(
        from_address="noreply@gem.example.com",
        subject="Senior Backend Engineer",
        body=RECRUITER_BODY,
    )
    assert condemned.kind is RecruiterEmailKind.NOT_RECRUITER

    rescued = recruiter_classifier.classify(
        from_address="noreply@gem.example.com",
        subject="Senior Backend Engineer",
        body=RECRUITER_BODY,
        reply_to="alex@northwind.com",
    )
    assert rescued.kind is RecruiterEmailKind.RECRUITER_OUTREACH


def test_a_no_reply_reply_to_is_still_a_no_reply(monkeypatch):
    """The escape hatch needs a *human* on the other end, not another robot."""
    result = recruiter_classifier.classify(
        from_address="noreply@gem.example.com",
        subject="Anything",
        body="Some text",
        reply_to="donotreply@gem.example.com",
    )
    assert result.kind is RecruiterEmailKind.NOT_RECRUITER


def test_the_reply_is_addressed_to_the_human_not_the_robot(
    db_session, current_user, connected_gmail, stub_gmail, profiles, watched
):
    """A reply sent to a `noreply@` is worse than no reply — it reports as answered."""
    stub_gmail["m1"] = gmail_message(
        "m1", sender="noreply@gem.example.com", reply_to="alex@northwind.com"
    )

    _, created = recruiter_reply_service.record_scan(
        db_session, current_user, connected_gmail
    )
    db_session.commit()
    recruiter_reply_service.process(db_session, created[0])
    db_session.commit()

    row = created[0]
    assert row.reply_address == "alex@northwind.com"
    reply = db_session.get(Email, row.reply_email_id)
    assert reply.to_address == "alex@northwind.com"
    # And the contact is filed under the address that reaches a person, not
    # under the platform's shared sending address.
    recruiter = db_session.query(Recruiter).one()
    assert recruiter.email == "alex@northwind.com"


def test_the_rule_fallback_survives_a_platform_preheader(monkeypatch):
    """"View this email in your browser" is not evidence of anything.

    It sat in the marketing hint list and short-circuited before a single
    recruiter phrase was counted, so an agency recruiter whose platform stamped
    that line at the top was filed as NOT_RECRUITER — the same shape of bug as
    the unsubscribe footer, and just as expensive.
    """
    from app.services.openrouter_client import OpenRouterError

    monkeypatch.setattr(
        recruiter_classifier,
        "chat_completion_detailed",
        lambda *a, **k: (_ for _ in ()).throw(OpenRouterError("429")),
    )

    result = recruiter_classifier.classify(
        from_address="alex@northwind.com",
        subject="Senior Backend Engineer at Northwind",
        body="View this email in your browser\n\n" + RECRUITER_BODY,
    )

    assert result.kind is RecruiterEmailKind.RECRUITER_OUTREACH


def _bounce_notice(message_id: str = "m1"):
    return gmail_message(
        message_id,
        sender="mailer-daemon@googlemail.com",
        sender_name="Mail Delivery Subsystem",
        subject="Delivery Status Notification (Failure)",
        body="Address not found. Your message wasn't delivered.",
    )


def test_scan_reports_a_bounce_without_booking_it(
    db_session, current_user, connected_gmail, stub_gmail
):
    """The scanner reports; the caller books. ``scan`` promises not to write."""
    stub_gmail["m1"] = _bounce_notice()
    before = connected_gmail.bounce_count or 0

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert result.messages == []
    assert result.skipped_bounce == 1
    assert [m.gmail_message_id for m in result.hard_bounces] == ["m1"]
    assert (connected_gmail.bounce_count or 0) == before


def test_record_scan_books_a_bounce_once_however_often_it_rescans(
    db_session, current_user, connected_gmail, stub_gmail
):
    """The outage this fixes.

    The bounce branch returned before the message was stored, so the notice
    never entered ``known_ids`` and every scan over the same rolling window
    booked it again. At a scan a minute one mailbox reached 4074 bounces on 20
    sends — a 20370% rate — and re-paused itself forever, which stopped all
    outbound mail. Re-scanning must be a no-op.
    """
    stub_gmail["m1"] = _bounce_notice()
    before = connected_gmail.bounce_count or 0

    for _ in range(5):
        recruiter_reply_service.record_scan(db_session, current_user, connected_gmail)

    assert (connected_gmail.bounce_count or 0) == before + 1
    # And it never becomes an opportunity: terminal, so `process` skips it.
    row = db_session.scalar(
        select(RecruiterEmail).where(RecruiterEmail.gmail_message_id == "m1")
    )
    assert row is not None
    assert row.status is RecruiterEmailStatus.CLASSIFIED
    assert row.kind is RecruiterEmailKind.NOT_RECRUITER


def test_scan_skips_opt_outs(db_session, current_user, connected_gmail, stub_gmail):
    stub_gmail["m1"] = gmail_message(
        "m1", subject="Unsubscribe", body="Please remove me from your list."
    )

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert result.messages == []
    assert result.skipped_opt_out == 1


def test_a_recruiter_footer_is_not_an_opt_out(
    db_session, current_user, connected_gmail, stub_gmail
):
    """The bug that made the whole feature silent.

    Agency recruiters mail from platforms that append an unsubscribe footer to
    every message. The opt-out filter matched the word anywhere in the body, so
    it discarded those emails before anything read them — in production, 56 of
    every 57 messages examined, for a week, with `detected: 0` every scan.
    """
    stub_gmail["m1"] = gmail_message(
        "m1",
        subject="Senior Backend Engineer at Northwind",
        body=(
            "Hi,\n\nI came across your profile and wanted to reach out about a "
            "Senior Backend Engineer role in San Francisco.\n\n"
            "Best,\nAlex Recruiter\n\n"
            "---\n"
            "You are receiving this email because you opted in on our site.\n"
            "To unsubscribe from these emails, click here: "
            "https://mail.example.com/unsubscribe?id=123\n"
        ),
    )

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert result.skipped_opt_out == 0
    assert [m.gmail_message_id for m in result.messages] == ["m1"]


@pytest.mark.parametrize(
    "subject,body",
    [
        ("Unsubscribe", "Please remove me from your list."),
        ("Re: your note", "stop emailing me"),
        ("Re: your note", "Please take me off your distribution list."),
        ("unsubscribe", ""),
        ("Re: opportunity", "Do not contact me again."),
    ],
)
def test_a_real_opt_out_is_still_honoured(
    db_session, current_user, connected_gmail, stub_gmail, subject, body
):
    """Narrowing the filter must not stop it recognising an actual request."""
    stub_gmail["m1"] = gmail_message("m1", subject=subject, body=body)

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert result.messages == []
    assert result.skipped_opt_out == 1


def test_scan_reads_mail_the_candidate_has_already_opened(
    db_session, current_user, connected_gmail, stub_gmail, monkeypatch
):
    """`is:unread` made an opened email invisible forever.

    A candidate who reads a recruiter's note on their phone before the beat tick
    is exactly the candidate who needs it answered, and the old query could never
    see it again. Re-reading is free: `known_ids` is what makes a rescan a no-op.
    """
    seen: dict[str, str] = {}

    real_list = inbound_scanner.gmail_service.list_messages

    def _capture(account, query, *, max_results=50):
        seen["query"] = query
        return real_list(account, query, max_results=max_results)

    monkeypatch.setattr(inbound_scanner.gmail_service, "list_messages", _capture)
    inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert "is:unread" not in seen["query"]
    # Promotions is where agency mail platforms land; excluding it dropped a
    # whole class of real opportunity.
    assert "-category:promotions" not in seen["query"]


def test_scan_reports_what_it_deferred(
    db_session, current_user, connected_gmail, stub_gmail
):
    """A capped scan says so; it never implies the whole mailbox was covered."""
    for i in range(5):
        stub_gmail[f"m{i}"] = gmail_message(f"m{i}")

    result = inbound_scanner.scan(db_session, current_user, connected_gmail, limit=2)

    assert len(result.messages) == 2
    assert result.deferred == 3


def test_scan_survives_an_unconfigured_mailbox(
    db_session, current_user, connected_gmail, monkeypatch
):
    def _boom(account, query, *, max_results=50):
        raise gmail_service.GmailNotConfigured("no credentials")

    monkeypatch.setattr(inbound_scanner.gmail_service, "list_messages", _boom)

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert result.messages == []
    assert "no credentials" in (result.error or "")


def test_parse_from_handles_a_bare_address():
    assert inbound_scanner.parse_from("alex@northwind.com") == (
        None,
        "alex@northwind.com",
    )
    assert inbound_scanner.parse_from('"Alex R" <Alex@Northwind.com>') == (
        "Alex R",
        "alex@northwind.com",
    )


def test_received_at_uses_gmail_time_not_poll_time(
    db_session, current_user, connected_gmail, stub_gmail
):
    """Old mail noticed today still sorts as old."""
    long_ago = datetime.now(UTC) - timedelta(days=5)
    stub_gmail["m1"] = gmail_message("m1", when=long_ago)

    result = inbound_scanner.scan(db_session, current_user, connected_gmail)

    assert abs((result.messages[0].received_at - long_ago).total_seconds()) < 2


# --------------------------------------------------------------------------- #
# Classification                                                               #
# --------------------------------------------------------------------------- #


def test_job_alert_is_caught_without_a_model_call(monkeypatch):
    """The pre-filter is most of the volume and must cost nothing."""
    called = False

    def _never(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("the classifier should not have called a model")

    monkeypatch.setattr(recruiter_classifier, "chat_completion_detailed", _never)

    result = recruiter_classifier.classify(
        from_address="jobalerts-noreply@linkedin.com",
        subject="New jobs for you",
        body=JOB_ALERT_BODY,
    )

    assert result.kind is RecruiterEmailKind.JOB_ALERT
    assert result.classified_by == "rules"
    assert called is False


def test_ats_acknowledgement_is_caught_without_a_model_call(monkeypatch):
    monkeypatch.setattr(
        recruiter_classifier,
        "chat_completion_detailed",
        lambda *a, **k: pytest.fail("should not reach the model"),
    )

    result = recruiter_classifier.classify(
        from_address="no-reply@greenhouse.io",
        subject="Thank you for applying",
        body=ATS_BODY,
    )

    assert result.kind is RecruiterEmailKind.ATS_AUTOMATED


def test_llm_classification_extracts_the_opportunity(monkeypatch):
    from app.services import llm_router

    def _fake(messages, **kwargs):
        return llm_router.Completion(
            text=(
                '{"kind": "RECRUITER_OUTREACH", "confidence": 0.95, '
                '"role_title": "Senior Backend Engineer", '
                '"company": "Northwind Payments", "location": "San Francisco", '
                '"remote": false, "salary_text": null, "seniority": "senior", '
                '"asks": ["Would you be open to a short chat this month?"], '
                '"reason": "A recruiter writing personally about a role."}'
            ),
            provider="openrouter",
            model="openai/gpt-oss-20b:free",
        )

    monkeypatch.setattr(recruiter_classifier, "chat_completion_detailed", _fake)

    result = recruiter_classifier.classify(
        from_address="alex@northwind.com",
        subject="Senior Backend Engineer at Northwind",
        body=RECRUITER_BODY,
    )

    assert result.kind is RecruiterEmailKind.RECRUITER_OUTREACH
    assert result.confidence == pytest.approx(0.95)
    assert result.role_title == "Senior Backend Engineer"
    assert result.company == "Northwind Payments"
    assert result.asks
    assert result.classified_by == "openrouter:openai/gpt-oss-20b:free"


def test_malformed_model_output_falls_back_and_never_actions(monkeypatch):
    from app.services import llm_router

    monkeypatch.setattr(
        recruiter_classifier,
        "chat_completion_detailed",
        lambda *a, **k: llm_router.Completion(
            text="I think this is probably a recruiter?",
            provider="openrouter",
            model="m",
        ),
    )

    result = recruiter_classifier.classify(
        from_address="someone@example.com",
        subject="Hello",
        body="A short note about nothing in particular.",
    )

    assert result.kind is RecruiterEmailKind.UNKNOWN
    assert result.is_actionable is False


def test_no_provider_configured_uses_rules(monkeypatch):
    """conftest leaves the chain keyless, which is the degraded path in prod too."""
    from app.services.openrouter_client import OpenRouterError

    def _fail(*args, **kwargs):
        raise OpenRouterError("no providers configured")

    monkeypatch.setattr(recruiter_classifier, "chat_completion_detailed", _fail)

    result = recruiter_classifier.classify(
        from_address="alex@northwind.com",
        subject="A role",
        body=RECRUITER_BODY,
    )

    assert result.classified_by == "rules"
    # The rule path may recognise outreach, but never confidently enough to
    # clear the auto-reply bar on its own.
    assert result.confidence < 0.9


def test_the_rule_fallback_identifies_outreach_from_a_single_phrase(monkeypatch):
    """With every provider rate-limited, this function *is* the classifier.

    It used to demand two strong phrases and return UNKNOWN otherwise, so a
    perfectly ordinary recruiter email was recorded as unreadable and never
    answered for as long as the outage lasted.
    """
    from app.services.openrouter_client import OpenRouterError

    monkeypatch.setattr(
        recruiter_classifier,
        "chat_completion_detailed",
        lambda *a, **k: (_ for _ in ()).throw(OpenRouterError("429 Too Many Requests")),
    )

    result = recruiter_classifier.classify(
        from_address="alex@northwind.com",
        subject="Backend role",
        body="Hi — would you be interested in hearing about a position we have?",
    )

    assert result.kind is RecruiterEmailKind.RECRUITER_OUTREACH
    assert result.is_actionable
    assert result.classified_by == "rules"


def test_the_rule_fallback_can_clear_the_auto_bar_at_a_strong_match(monkeypatch):
    """The keyword fallback can now auto-reply, but only at a strong match.

    Before 2026-07-29 the fallback's confidence ceiling (0.6) sat below the
    auto threshold at *any* match score, so an LLM outage meant every message
    fell to DRAFT — arithmetically impossible to auto-reply even to an
    obvious recruiter with a perfect match. The ceiling was raised to 0.8
    specifically so a confident keyword match times a strong profile fit can
    still clear the bar (0.8 * 100 = 80 >= the 65 auto threshold) when no LLM
    is available to confirm it. See reply_routing's module docstring.
    """
    from app.services.openrouter_client import OpenRouterError

    monkeypatch.setattr(
        recruiter_classifier,
        "chat_completion_detailed",
        lambda *a, **k: (_ for _ in ()).throw(OpenRouterError("429")),
    )

    result = recruiter_classifier.classify(
        from_address="alex@northwind.com",
        subject="A role",
        body=RECRUITER_BODY,
    )

    decision = reply_routing.decide(
        result.kind, result.confidence, 100.0, has_profile=True, auto_enabled=True
    )
    assert decision.route is ReplyRoute.AUTO


def test_the_rule_fallback_still_rejects_bulk_marketing(monkeypatch):
    from app.services.openrouter_client import OpenRouterError

    monkeypatch.setattr(
        recruiter_classifier,
        "chat_completion_detailed",
        lambda *a, **k: (_ for _ in ()).throw(OpenRouterError("429")),
    )

    result = recruiter_classifier.classify(
        from_address="deals@shop.example.com",
        subject="An opportunity you can't miss — 40% off",
        body="View this email in your browser. Shop now, sale ends Friday!",
    )

    assert result.kind is RecruiterEmailKind.NOT_RECRUITER
    assert result.is_actionable is False


def test_unknown_classification_can_never_auto_reply():
    """UNKNOWN never sends unreviewed, even at a perfect match — DRAFT at best."""
    decision = reply_routing.decide(
        RecruiterEmailKind.UNKNOWN, 1.0, 100.0, has_profile=True, auto_enabled=True
    )
    assert decision.route is ReplyRoute.DRAFT


def test_unknown_classification_below_the_match_floor_still_flags():
    """A weak or absent profile match on an unclassifiable message still flags."""
    decision = reply_routing.decide(
        RecruiterEmailKind.UNKNOWN, 1.0, 40.0, has_profile=True, auto_enabled=True
    )
    assert decision.route is ReplyRoute.FLAG


# --------------------------------------------------------------------------- #
# Matching                                                                     #
# --------------------------------------------------------------------------- #


def _classification(**overrides) -> recruiter_classifier.Classification:
    base = dict(
        kind=RecruiterEmailKind.RECRUITER_OUTREACH,
        confidence=0.95,
        role_title="Senior Backend Engineer",
        company="Northwind Payments",
        location="San Francisco",
        remote=False,
        seniority="senior",
    )
    base.update(overrides)
    return recruiter_classifier.Classification(**base)


def test_match_picks_the_right_profile(db_session, current_user, profiles):
    match = inbound_matcher.match(
        db_session, current_user, _classification(), body=RECRUITER_BODY
    )

    assert match is not None
    assert match.profile_id == profiles[0].id
    assert match.label == "Backend Engineer"
    assert match.score > 0


def test_match_is_deterministic(db_session, current_user, profiles):
    """Same input, same number — the invariant the whole scoring story rests on."""
    first = inbound_matcher.match(
        db_session, current_user, _classification(), body=RECRUITER_BODY
    )
    second = inbound_matcher.match(
        db_session, current_user, _classification(), body=RECRUITER_BODY
    )

    assert first.score == second.score
    assert first.profile_id == second.profile_id


def test_inactive_profiles_are_never_matched(db_session, current_user, profiles):
    profiles[0].is_active = False
    db_session.commit()

    match = inbound_matcher.match(
        db_session, current_user, _classification(), body=RECRUITER_BODY
    )

    assert match.profile_id == profiles[1].id


def test_user_with_no_profiles_still_scores(db_session, current_user, resume):
    """The pre-profile fallback: preferences + default resume as one target."""
    match = inbound_matcher.match(
        db_session, current_user, _classification(), body=RECRUITER_BODY
    )

    assert match is not None
    assert match.profile_id is None
    assert match.score > 0


def test_location_gate_vetoes_a_city_the_profile_does_not_list(
    db_session, current_user, profiles
):
    profiles[0].location_preferences = ["Berlin"]
    profiles[1].is_active = False
    db_session.commit()

    match = inbound_matcher.match(
        db_session,
        current_user,
        _classification(location="Singapore", remote=False),
        body=RECRUITER_BODY,
    )

    assert match.location_ok is False
    assert "Singapore" in (match.location_note or "")


def test_remote_roles_always_pass_the_location_gate(
    db_session, current_user, profiles
):
    profiles[0].location_preferences = ["Berlin"]
    db_session.commit()

    match = inbound_matcher.match(
        db_session,
        current_user,
        _classification(location="Singapore", remote=True),
        body=RECRUITER_BODY,
    )

    assert match.location_ok is True


# --------------------------------------------------------------------------- #
# Routing — the table in the design doc, band by band                          #
# --------------------------------------------------------------------------- #


def test_high_confidence_and_high_match_auto_replies():
    decision = reply_routing.decide(
        RecruiterEmailKind.RECRUITER_OUTREACH,
        0.95,
        95.0,
        has_profile=True,
        auto_enabled=True,
    )
    assert decision.route is ReplyRoute.AUTO
    assert decision.confidence == pytest.approx(90.25)


def test_auto_band_downgrades_to_draft_when_auto_is_off():
    decision = reply_routing.decide(
        RecruiterEmailKind.RECRUITER_OUTREACH,
        0.95,
        95.0,
        has_profile=True,
        auto_enabled=False,
    )
    assert decision.route is ReplyRoute.DRAFT


def test_auto_band_downgrades_to_draft_on_a_location_veto():
    decision = reply_routing.decide(
        RecruiterEmailKind.RECRUITER_OUTREACH,
        0.95,
        95.0,
        has_profile=True,
        location_ok=False,
        auto_enabled=True,
    )
    assert decision.route is ReplyRoute.DRAFT
    assert "location" in decision.reason.lower()


def test_middle_band_drafts(monkeypatch):
    monkeypatch.setattr(settings, "recruiter_reply_auto_threshold", 90.0)
    decision = reply_routing.decide(
        RecruiterEmailKind.RECRUITER_OUTREACH,
        0.95,
        80.0,
        has_profile=True,
        auto_enabled=True,
    )
    assert decision.route is ReplyRoute.DRAFT
    assert decision.confidence == pytest.approx(76.0)


def test_confident_read_of_a_bad_fit_still_drafts():
    """A weak fit is something to ask about, not a reason to say nothing.

    The old behaviour flagged this — 0.95 × 40 = 38, under a 70 draft floor — and
    that is the bug that made the whole feature silent: with every LLM provider
    rate-limited the classifier is capped at 0.6, so *nothing* could clear 70 at
    any match score, and every message in the mailbox was flagged unanswered.
    A real recruiter who wrote about a real job now always gets a draft.
    """
    decision = reply_routing.decide(
        RecruiterEmailKind.RECRUITER_OUTREACH,
        0.95,
        40.0,
        has_profile=True,
        auto_enabled=True,
    )
    assert decision.confidence == pytest.approx(38.0)
    assert decision.route is ReplyRoute.DRAFT


def test_multiplying_still_keeps_a_bad_fit_out_of_the_auto_band():
    """The product of the two signals is still what gates an *unread* send.

    Averaging would put a confident reading of a bad fit at 67.5 — over the 65
    auto bar — and send it without the candidate seeing it. Multiplying gives 38.
    """
    decision = reply_routing.decide(
        RecruiterEmailKind.RECRUITER_OUTREACH,
        0.95,
        40.0,
        has_profile=True,
        auto_enabled=True,
    )
    assert decision.confidence == pytest.approx(38.0)
    assert decision.route is not ReplyRoute.AUTO


def test_a_deployment_can_restore_the_silent_floor(monkeypatch):
    """The flag band is still reachable, just off by default."""
    monkeypatch.setattr(settings, "recruiter_reply_draft_threshold", 70.0)
    decision = reply_routing.decide(
        RecruiterEmailKind.RECRUITER_OUTREACH,
        0.95,
        40.0,
        has_profile=True,
        auto_enabled=True,
    )
    assert decision.route is ReplyRoute.FLAG


def test_no_profile_flags_with_a_reason():
    decision = reply_routing.decide(
        RecruiterEmailKind.RECRUITER_OUTREACH,
        0.95,
        None,
        has_profile=False,
        auto_enabled=True,
    )
    assert decision.route is ReplyRoute.FLAG
    assert decision.reason


@pytest.mark.parametrize(
    "kind",
    [
        RecruiterEmailKind.JOB_ALERT,
        RecruiterEmailKind.ATS_AUTOMATED,
        RecruiterEmailKind.NOT_RECRUITER,
    ],
)
def test_non_opportunities_are_recorded_but_never_routed(kind):
    decision = reply_routing.decide(
        kind, 0.95, 95.0, has_profile=True, auto_enabled=True
    )
    assert decision.route is None
    assert decision.replies is False
    assert decision.reason


def test_every_decision_explains_itself():
    for score in (10.0, 75.0, 95.0):
        decision = reply_routing.decide(
            RecruiterEmailKind.RECRUITER_OUTREACH,
            0.98,
            score,
            has_profile=True,
            auto_enabled=True,
            profile_label="Backend Engineer",
        )
        assert decision.reason.strip()


# --------------------------------------------------------------------------- #
# Reply generation                                                             #
# --------------------------------------------------------------------------- #


def _target(db_session, current_user):
    from app.services import profile_service

    return profile_service.active_targets(db_session, current_user)[0]


def test_reply_falls_back_to_a_template_without_a_model(
    db_session, current_user, profiles
):
    draft = inbound_reply.draft(
        candidate_name="Jordan Candidate",
        classification=_classification(),
        target=_target(db_session, current_user),
        message_body=RECRUITER_BODY,
        subject="Senior Backend Engineer at Northwind",
    )

    assert draft.generated_with == "heuristic"
    assert "Senior Backend Engineer" in draft.body
    assert "Jordan Candidate" in draft.body


def test_reply_rejects_an_invented_salary(db_session, current_user, profiles, monkeypatch):
    """The guard that matters most: a figure the candidate never named."""
    from app.services import llm_router

    monkeypatch.setattr(
        inbound_reply,
        "chat_completion_detailed",
        lambda *a, **k: llm_router.Completion(
            text=(
                '{"subject": "Re: role", "body": "Hi Alex, thanks for reaching '
                'out about the Senior Backend Engineer role. I am targeting '
                'around $250,000 for a role at this scope. Happy to chat.", '
                '"note": "", "questions_asked": []}'
            ),
            provider="openrouter",
            model="m",
        ),
    )

    draft = inbound_reply.draft(
        candidate_name="Jordan Candidate",
        classification=_classification(),
        target=_target(db_session, current_user),
        message_body=RECRUITER_BODY,
    )

    assert draft.generated_with == "heuristic"
    assert "250,000" not in draft.body


def test_reply_rejects_an_invented_meeting_time(
    db_session, current_user, profiles, monkeypatch
):
    from app.services import llm_router

    monkeypatch.setattr(
        inbound_reply,
        "chat_completion_detailed",
        lambda *a, **k: llm_router.Completion(
            text=(
                '{"subject": "Re: role", "body": "Hi Alex, thanks for the note '
                "about the backend role. Tuesday at 3pm works well for me — "
                'shall we lock that in?", "note": "", "questions_asked": []}'
            ),
            provider="openrouter",
            model="m",
        ),
    )

    draft = inbound_reply.draft(
        candidate_name="Jordan Candidate",
        classification=_classification(),
        target=_target(db_session, current_user),
        message_body=RECRUITER_BODY,
    )

    assert draft.generated_with == "heuristic"
    assert "3pm" not in draft.body


def test_reply_rejects_a_reasoning_scratchpad(
    db_session, current_user, profiles, monkeypatch
):
    """The free-tier failure that would be unrecoverable if it reached a recruiter."""
    from app.services import llm_router

    monkeypatch.setattr(
        inbound_reply,
        "chat_completion_detailed",
        lambda *a, **k: llm_router.Completion(
            text=(
                '{"subject": "Re", "body": "We need to write a reply. The user '
                'wants a warm response. Let us produce something friendly.", '
                '"note": "", "questions_asked": []}'
            ),
            provider="openrouter",
            model="m",
        ),
    )

    draft = inbound_reply.draft(
        candidate_name="Jordan Candidate",
        classification=_classification(),
        target=_target(db_session, current_user),
        message_body=RECRUITER_BODY,
    )

    assert draft.generated_with == "heuristic"
    assert "We need to write" not in draft.body


def test_reply_accepts_a_clean_generation(
    db_session, current_user, profiles, monkeypatch
):
    from app.services import llm_router

    monkeypatch.setattr(
        inbound_reply,
        "chat_completion_detailed",
        lambda *a, **k: llm_router.Completion(
            text=(
                '{"subject": "Re: Senior Backend Engineer", "body": "Hi Alex,\\n\\n'
                "Thanks for reaching out about the Senior Backend Engineer role at "
                "Northwind Payments. Payment infrastructure is exactly the kind of "
                "work I have been doing in Python and FastAPI, so I would be glad "
                "to hear more.\\n\\nCould you share the compensation band and how "
                "the team is structured? Happy to find time for a call in the next "
                'week or two.\\n\\nBest,\\nJordan", "note": "Confirms interest and '
                'asks for the band.", "questions_asked": []}'
            ),
            provider="openrouter",
            model="openai/gpt-oss-20b:free",
        ),
    )

    draft = inbound_reply.draft(
        candidate_name="Jordan Candidate",
        classification=_classification(),
        target=_target(db_session, current_user),
        message_body=RECRUITER_BODY,
    )

    assert draft.generated_with == "llm"
    assert "Northwind Payments" in draft.body
    assert draft.note == "Confirms interest and asks for the band."


def test_reply_subject_threads_on_the_original():
    assert (
        inbound_reply.reply_subject(_classification(), "Senior Backend Engineer")
        == "Re: Senior Backend Engineer"
    )
    assert (
        inbound_reply.reply_subject(_classification(), "Re: already a reply")
        == "Re: already a reply"
    )


# --------------------------------------------------------------------------- #
# Persistence and the handoff to the existing pipeline                         #
# --------------------------------------------------------------------------- #


def _detect(db_session, current_user, connected_gmail, stub_gmail, **kwargs):
    stub_gmail["m1"] = gmail_message("m1", **kwargs)
    _result, created = recruiter_reply_service.record_scan(
        db_session, current_user, connected_gmail
    )
    db_session.commit()
    return created[0]


@pytest.fixture()
def confident_classifier(monkeypatch):
    """Every message reads as a strong recruiter match."""
    monkeypatch.setattr(
        recruiter_reply_service.recruiter_classifier,
        "classify",
        lambda **kwargs: _classification(confidence=0.98),
    )


def test_a_recruiter_email_is_answered_while_every_llm_provider_is_down(
    db_session, current_user, connected_gmail, stub_gmail, profiles, monkeypatch
):
    """The exact production failure, end to end.

    Every provider 429s, so the classifier falls back to rules and is capped at
    0.6 confidence. The old routing multiplied that by the match score and
    compared against a 70 draft floor — unreachable at *any* match score — so the
    message was flagged and the candidate was never told. It must now produce a
    reply they can send, with the resume attached.
    """
    from app.services.openrouter_client import OpenRouterError

    def _rate_limited(*args, **kwargs):
        raise OpenRouterError("429 Too Many Requests")

    monkeypatch.setattr(recruiter_classifier, "chat_completion_detailed", _rate_limited)
    monkeypatch.setattr(inbound_reply, "chat_completion_detailed", _rate_limited)

    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert row.status is RecruiterEmailStatus.DRAFTED
    assert row.route is ReplyRoute.DRAFT
    assert row.reply_email_id is not None

    reply = db_session.get(Email, row.reply_email_id)
    assert reply.status is EmailStatus.DRAFT
    assert reply.body_text

    # And the CV travels with it when it goes.
    from app.services import email_attachments

    files = email_attachments.files_for_email(db_session, reply)
    assert files.resume_filename, "a reply to a recruiter must carry the resume"


def test_a_weak_fit_is_answered_rather_than_flagged(
    db_session, current_user, connected_gmail, stub_gmail, profiles, monkeypatch
):
    """A mediocre match is a thing to ask about, not a reason for silence."""
    monkeypatch.setattr(
        recruiter_reply_service.recruiter_classifier,
        "classify",
        lambda **kwargs: _classification(role_title="Registered Nurse", confidence=0.4),
    )

    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert row.status is RecruiterEmailStatus.DRAFTED
    assert row.reply_email_id is not None


def test_a_drafted_reply_builds_the_whole_chain(
    db_session, current_user, connected_gmail, stub_gmail, profiles, confident_classifier
):
    """recruiter -> campaign -> application -> thread -> two emails."""
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)

    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert row.status is RecruiterEmailStatus.DRAFTED
    assert row.route is ReplyRoute.DRAFT
    assert row.matched_profile_id == profiles[0].id

    application = db_session.get(Application, row.application_id)
    assert application is not None
    assert application.user_id == current_user.id
    assert application.profile_id == profiles[0].id

    campaign = db_session.get(Campaign, application.campaign_id)
    assert campaign.name == recruiter_reply_service.INBOUND_CAMPAIGN_NAME
    assert campaign.follow_up_enabled is False

    thread = db_session.query(EmailThread).filter_by(
        application_id=application.id
    ).one()
    # The handoff: poll_thread takes the conversation from here.
    assert thread.gmail_thread_id == "t-m1"

    emails = db_session.query(Email).filter_by(thread_id=thread.id).all()
    assert len(emails) == 2
    inbound = next(e for e in emails if e.direction == EmailDirection.RECEIVED)
    reply = next(e for e in emails if e.direction == EmailDirection.SENT)
    assert inbound.gmail_message_id == "m1"
    assert reply.status is EmailStatus.DRAFT
    assert row.reply_email_id == reply.id


def test_the_inbound_campaign_is_created_once(
    db_session, current_user, connected_gmail, stub_gmail, profiles, confident_classifier
):
    first = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, first)
    db_session.commit()

    stub_gmail["m2"] = gmail_message("m2", sender="sam@globex.com")
    _result, created = recruiter_reply_service.record_scan(
        db_session, current_user, connected_gmail
    )
    db_session.commit()
    recruiter_reply_service.process(db_session, created[0])
    db_session.commit()

    campaigns = (
        db_session.query(Campaign)
        .filter_by(name=recruiter_reply_service.INBOUND_CAMPAIGN_NAME)
        .all()
    )
    assert len(campaigns) == 1


def test_a_flag_creates_nothing_but_the_row(
    db_session, current_user, connected_gmail, stub_gmail, profiles, monkeypatch
):
    """A message we could not read at all is the flag band's remaining job.

    A *weak fit* no longer lands here — that gets drafted, since a real person
    still wrote about a real job. What flags is a message the classifier could
    not identify *and* that does not match any profile — there is nothing
    truthful to write either from the classification or from the match score.
    The mock below clears every structured field ``_classification()``
    otherwise defaults (role_title, company, location, seniority) — the
    matcher scores those extracted fields, not the raw body text, so leaving
    any of them at their "Senior Backend Engineer" defaults would still match
    the profile well regardless of what the email actually says.
    """
    monkeypatch.setattr(
        recruiter_reply_service.recruiter_classifier,
        "classify",
        lambda **kwargs: _classification(
            kind=RecruiterEmailKind.UNKNOWN,
            confidence=0.0,
            role_title=None,
            company=None,
            location=None,
            seniority=None,
        ),
    )
    row = _detect(
        db_session,
        current_user,
        connected_gmail,
        stub_gmail,
        subject="Hello",
        body="A short note about nothing in particular.",
    )

    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert row.status is RecruiterEmailStatus.FLAGGED
    assert row.route is ReplyRoute.FLAG
    assert row.flag_reason
    assert row.application_id is None
    assert row.reply_email_id is None
    assert db_session.query(Application).count() == 0
    assert db_session.query(EmailThread).count() == 0


def test_a_job_alert_is_classified_and_left_alone(
    db_session, current_user, connected_gmail, stub_gmail, profiles
):
    row = _detect(
        db_session,
        current_user,
        connected_gmail,
        stub_gmail,
        sender="jobalerts-noreply@linkedin.com",
        subject="New jobs for you",
        body=JOB_ALERT_BODY,
    )

    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert row.kind is RecruiterEmailKind.JOB_ALERT
    assert row.status is RecruiterEmailStatus.CLASSIFIED
    assert row.route is None
    assert db_session.query(Application).count() == 0


def test_an_opted_out_recruiter_is_never_replied_to(
    db_session, current_user, connected_gmail, stub_gmail, profiles, confident_classifier
):
    db_session.add(
        Recruiter(
            user_id=current_user.id, email="alex@northwind.com", opted_out=True
        )
    )
    db_session.commit()
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)

    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert row.status is RecruiterEmailStatus.FLAGGED
    assert row.reply_email_id is None
    assert "not to be contacted" in (row.flag_reason or "")


def test_processing_is_idempotent(
    db_session, current_user, connected_gmail, stub_gmail, profiles, confident_classifier
):
    """A replayed task re-reads rather than re-replies."""
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    outcome = recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert outcome["status"] == "skipped"
    assert db_session.query(Application).count() == 1


# --------------------------------------------------------------------------- #
# The auto-reply band                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def auto_on(monkeypatch, db_session, current_user):
    """Both switches on — the only configuration that can send unreviewed."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "recruiter_reply_auto_enabled", True)
    pref = recruiter_reply_service.preference_for(db_session, current_user)
    pref.enabled = True
    pref.auto_reply_enabled = True
    db_session.commit()
    return pref


def test_auto_reply_is_off_unless_both_switches_are_on(db_session, current_user):
    from app.core.config import settings

    pref = recruiter_reply_service.preference_for(db_session, current_user)
    pref.auto_reply_enabled = True
    db_session.commit()

    # Server switch still off (the conftest default).
    assert settings.recruiter_reply_auto_enabled is False
    assert recruiter_reply_service.auto_reply_allowed(db_session, current_user, pref) is False


def test_a_templated_reply_can_auto_reply_when_every_gate_agrees(
    db_session, current_user, connected_gmail, stub_gmail, profiles,
    confident_classifier, auto_on,
):
    """As of 2026-07-29, a template reply is no longer downgraded to DRAFT.

    Previously any reply not written by an LLM (``generated_with != "llm"``)
    was force-downgraded to DRAFT regardless of how confident the
    classification and match were. That made auto-reply impossible during an
    LLM outage even when the classifier (confident_classifier, here mocked to
    0.98) and the match score both clearly justify it. The template itself is
    a safe, professional response — the routing bands are the safety check,
    not the source of the text.
    """
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)

    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    # conftest's keyless chain means the reply text still comes from the
    # template fallback, not a model — that's the point being tested.
    assert row.route is ReplyRoute.AUTO
    assert row.status is RecruiterEmailStatus.REPLY_QUEUED
    reply = db_session.get(Email, row.reply_email_id)
    assert reply.status is EmailStatus.QUEUED


@pytest.fixture()
def clean_generation(monkeypatch):
    """A model reply that survives every invention check — the only kind that sends."""
    from app.services import llm_router

    monkeypatch.setattr(
        inbound_reply,
        "chat_completion_detailed",
        lambda *a, **k: llm_router.Completion(
            text=(
                '{"subject": "Re: role", "body": "Hi Alex,\\n\\nThanks for '
                "reaching out about the Senior Backend Engineer role. Payment "
                "infrastructure in Python is exactly what I have been building, "
                "so I would be glad to hear more. Could you share the "
                'compensation band?\\n\\nBest,\\nJordan", "note": "", '
                '"questions_asked": []}'
            ),
            provider="openrouter",
            model="m",
        ),
    )


def test_a_clean_generation_can_auto_reply(
    db_session, current_user, connected_gmail, stub_gmail, profiles,
    confident_classifier, auto_on, clean_generation,
):
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)

    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert row.route is ReplyRoute.AUTO
    assert row.status is RecruiterEmailStatus.REPLY_QUEUED
    reply = db_session.get(Email, row.reply_email_id)
    # QUEUED, not SENT: it goes out through the ordinary throttled sender, which
    # is where the reputation gate lives. There is no second send path.
    assert reply.status is EmailStatus.QUEUED


def test_the_daily_auto_reply_cap_downgrades_to_a_draft(
    db_session, current_user, profiles, auto_on
):
    from app.core.config import settings

    for i in range(settings.recruiter_auto_reply_daily_limit):
        db_session.add(
            RecruiterEmail(
                user_id=current_user.id,
                gmail_message_id=f"prev-{i}",
                from_address="someone@example.com",
                kind=RecruiterEmailKind.RECRUITER_OUTREACH,
                route=ReplyRoute.AUTO,
                status=RecruiterEmailStatus.REPLIED,
            )
        )
    db_session.commit()

    assert (
        recruiter_reply_service.auto_reply_allowed(db_session, current_user, auto_on)
        is False
    )


# --------------------------------------------------------------------------- #
# What travels with the reply                                                  #
# --------------------------------------------------------------------------- #
#
# A reply that says "here's why I fit" and attaches nothing is half an answer.
# The reply goes out through the ordinary sender, so it picks up attachments the
# same way outreach does — what these tests pin down is that the resume it picks
# is the one belonging to the profile the *matcher* chose, not whichever resume
# happens to be the user's default.


@pytest.fixture()
def nursing_resume(db_session, current_user) -> Resume:
    """A second document, so "the matched profile's resume" can be wrong."""
    row = Resume(
        user_id=current_user.id,
        filename="casey-nursing.pdf",
        raw_text="Casey Nightingale — Registered Nurse.",
        full_name="Casey Nightingale",
        headline="Registered Nurse",
        skills=["triage", "patient care"],
        experience=[{"company": "Mercy", "title": "RN", "start": "2019", "end": "2024"}],
        is_default=True,  # the default is deliberately the *wrong* one
        parsed_with="heuristic",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def split_profiles(db_session, profiles, resume, nursing_resume) -> list[Profile]:
    """The two intents, each arguing with its own document."""
    profiles[0].resume_id = resume.id          # Backend Engineer
    profiles[1].resume_id = nursing_resume.id  # Registered Nurse
    db_session.commit()
    return profiles


@pytest.fixture()
def capture_send(monkeypatch, db_session):
    """Point the sender at the test session and record what Gmail was handed."""
    monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    sent: list[dict] = []

    def _fake_send(**kwargs):
        sent.append(kwargs)
        return gmail_service.SentMessage(
            gmail_message_id="sent-1", gmail_thread_id="t-m1"
        )

    monkeypatch.setattr(gmail_service, "send_email", _fake_send)
    return sent


def test_an_auto_reply_leaves_with_the_matched_profiles_resume(
    db_session, current_user, connected_gmail, stub_gmail, split_profiles,
    confident_classifier, auto_on, clean_generation, capture_send,
):
    """End to end: recruiter writes, we answer, the right resume is attached."""
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert row.route is ReplyRoute.AUTO
    result = email_tasks.send_outreach_email(row.reply_email_id)

    assert result["status"] == "sent"
    attachments = capture_send[0]["attachments"]
    assert len(attachments) == 1
    # Jordan's backend resume — not Casey's, which is the account default.
    assert attachments[0].filename == "jordan-candidate-resume.pdf"
    assert attachments[0].content.startswith(b"%PDF")

    reply = db_session.get(Email, row.reply_email_id)
    assert reply.status is EmailStatus.SENT
    assert reply.attachment_filename == "jordan-candidate-resume.pdf"


def test_the_reply_threads_onto_the_recruiters_own_message(
    db_session, current_user, connected_gmail, stub_gmail, split_profiles,
    confident_classifier, auto_on, clean_generation, capture_send,
):
    """Attaching a file must not cost us the thread the recruiter started."""
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    email_tasks.send_outreach_email(row.reply_email_id)

    assert capture_send[0]["thread_id"] == row.gmail_thread_id
    assert capture_send[0]["to"] == "alex@northwind.com"


def test_a_drafted_reply_resolves_the_same_resume_on_approval(
    db_session, current_user, connected_gmail, stub_gmail, split_profiles,
    confident_classifier, capture_send,
):
    """The 70-89 band waits for a human, then sends with the same attachment."""
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert row.route is ReplyRoute.DRAFT
    reply = db_session.get(Email, row.reply_email_id)
    assert reply.status is EmailStatus.DRAFT

    # What approving it in /review does.
    reply.status = EmailStatus.QUEUED
    db_session.commit()
    email_tasks.send_outreach_email(reply.id)

    assert capture_send[0]["attachments"][0].filename == "jordan-candidate-resume.pdf"


def test_a_reply_still_goes_out_when_no_resume_can_be_rendered(
    db_session, current_user, connected_gmail, stub_gmail, split_profiles,
    confident_classifier, auto_on, clean_generation, capture_send, monkeypatch,
):
    """The answer matters more than the attachment. Recorded, not silent."""
    monkeypatch.setattr(resume_pdf, "is_available", lambda: False)
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    result = email_tasks.send_outreach_email(row.reply_email_id)

    assert result["status"] == "sent"
    assert not capture_send[0]["attachments"]
    reply = db_session.get(Email, row.reply_email_id)
    assert reply.attachment_filename is None


def test_the_auto_band_is_handed_to_the_throttled_sender(
    db_session, current_user, connected_gmail, stub_gmail, split_profiles,
    confident_classifier, auto_on, clean_generation, monkeypatch,
):
    """The task dispatches the send itself — nothing else polls for REPLY_QUEUED."""
    monkeypatch.setattr(recruiter_reply_tasks, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    dispatched: list[int] = []
    monkeypatch.setattr(
        recruiter_reply_tasks, "_dispatch_send", lambda email_id: dispatched.append(email_id)
    )

    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    outcome = recruiter_reply_tasks.process_recruiter_email(row.id)

    assert outcome["route"] == "AUTO"
    assert dispatched == [row.reply_email_id]


def test_a_drafted_reply_is_never_dispatched(
    db_session, current_user, connected_gmail, stub_gmail, split_profiles,
    confident_classifier, monkeypatch,
):
    """The whole point of the middle band: nothing leaves without a human."""
    monkeypatch.setattr(recruiter_reply_tasks, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    dispatched: list[int] = []
    monkeypatch.setattr(
        recruiter_reply_tasks, "_dispatch_send", lambda email_id: dispatched.append(email_id)
    )

    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_tasks.process_recruiter_email(row.id)

    assert dispatched == []


# --------------------------------------------------------------------------- #
# No opt-out footer on a reply                                                  #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def capture_raw_send(monkeypatch, db_session):
    """Send for real as far as the MIME, capturing what Gmail would receive.

    Deliberately *not* the ``capture_send`` stub above: that one replaces
    ``send_email`` and so can only see the arguments, and the footer is added
    inside it. The bytes on the wire are the thing being asserted here.
    """
    monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    payloads: list[dict] = []

    class _Request:
        def execute(self):
            return {"id": "sent-1", "threadId": "t-m1"}

    class _Messages:
        def send(self, *, userId, body):
            payloads.append(body)
            return _Request()

    class _Users:
        def messages(self):
            return _Messages()

    class _Service:
        def users(self):
            return _Users()

    monkeypatch.setattr(gmail_service, "_service", lambda account: _Service())
    return payloads


def _sent_text(payload: dict) -> str:
    """Every header and every *decoded* body part of one captured send.

    Decoded rather than grepped raw: ``MIMEText`` base64-encodes a utf-8 body,
    so searching the wire bytes for "unsubscribe" would pass whether or not the
    footer is there. This assertion has to be able to fail.
    """
    message = message_from_bytes(base64.urlsafe_b64decode(payload["raw"]))
    chunks = [f"{name}: {value}" for name, value in message.items()]
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        body = part.get_payload(decode=True)
        if body:
            chunks.append(body.decode("utf-8", "replace"))
    return "\n".join(chunks)


def test_a_recruiter_reply_carries_no_unsubscribe_link(
    db_session, current_user, connected_gmail, stub_gmail, split_profiles,
    confident_classifier, auto_on, clean_generation, capture_raw_send,
):
    """The recruiter wrote to *us*. There is no list, so there is no opt-out.

    Asserted over headers as well as the body: a ``List-Unsubscribe`` header
    puts a one-click "Unsubscribe" button in Gmail's own UI even when the body
    is clean, which is the same mistake wearing a hat.
    """
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    assert email_tasks.send_outreach_email(row.reply_email_id)["status"] == "sent"

    sent = _sent_text(capture_raw_send[0])
    assert "unsubscribe" not in sent.lower()
    assert settings.compliance_physical_address not in sent
    # The reply itself still went out whole — this removes a footer, not a body.
    assert db_session.get(Email, row.reply_email_id).body_text in sent


def test_a_reviewed_reply_carries_no_unsubscribe_link_either(
    db_session, current_user, connected_gmail, stub_gmail, split_profiles,
    confident_classifier, capture_raw_send,
):
    """The draft band goes out through the same sender, so it must match."""
    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    reply = db_session.get(Email, row.reply_email_id)
    assert reply.status is EmailStatus.DRAFT
    reply.status = EmailStatus.QUEUED  # what approving it in /review does
    db_session.commit()

    email_tasks.send_outreach_email(reply.id)

    assert "unsubscribe" not in _sent_text(capture_raw_send[0]).lower()


def test_a_tracked_recruiter_reply_carries_no_unsubscribe_link(
    db_session, current_user, connected_gmail, stub_gmail, split_profiles,
    confident_classifier, auto_on, clean_generation, capture_raw_send, monkeypatch,
):
    """The HTML alternative is built separately, and footered separately."""
    monkeypatch.setattr(settings, "email_tracking_enabled", True)

    row = _detect(db_session, current_user, connected_gmail, stub_gmail)
    recruiter_reply_service.process(db_session, row)
    db_session.commit()

    email_tasks.send_outreach_email(row.reply_email_id)

    sent = _sent_text(capture_raw_send[0])
    assert "<p>" in sent  # the HTML alternative really was built
    assert "unsubscribe" not in sent.lower()


def test_the_sender_still_footers_cold_outreach(
    db_session, current_user, connected_gmail, capture_raw_send,
):
    """The counterpart. Nothing here weakens CAN-SPAM on mail we initiate.

    An ordinary outreach row — nothing in ``recruiter_emails`` pointing at it —
    is what tells the sender this message was not solicited.
    """
    campaign = Campaign(
        user_id=current_user.id, name="Outbound", status=CampaignStatus.ACTIVE
    )
    db_session.add(campaign)
    db_session.flush()
    recruiter = Recruiter(
        user_id=current_user.id, email="cold@acme.com", name="Sam", company="Acme"
    )
    db_session.add(recruiter)
    db_session.flush()
    application = Application(
        user_id=current_user.id, campaign_id=campaign.id, recruiter_id=recruiter.id
    )
    db_session.add(application)
    db_session.flush()
    thread = EmailThread(application_id=application.id, subject="Hello")
    db_session.add(thread)
    db_session.flush()
    email = Email(
        thread_id=thread.id,
        direction=EmailDirection.SENT,
        status=EmailStatus.QUEUED,
        to_address="cold@acme.com",
        subject="Hello",
        body_text="Hi Sam, I'd love to talk.",
    )
    db_session.add(email)
    db_session.commit()

    assert email_tasks.send_outreach_email(email.id)["status"] == "sent"

    sent = _sent_text(capture_raw_send[0])
    assert "unsubscribe" in sent.lower()
    assert "List-Unsubscribe" in sent
    assert settings.compliance_physical_address in sent


# --------------------------------------------------------------------------- #
# The fallback sweep, and not stacking up                                       #
# --------------------------------------------------------------------------- #


def test_the_beat_sweep_is_a_five_minute_fallback(monkeypatch):
    """Beat is the safety net now; Gmail push is the primary route.

    It used to run every minute because it was the *only* route, and the thing
    being optimised is how long a recruiter waits for an answer. Push answers
    that in seconds (see services/gmail_push), so a minute-by-minute sweep of
    mailboxes push already covers is sixty scans an hour that find nothing.

    Five minutes is what a mailbox waits when push is off, failed, or has gone
    quiet — which is the only situation this tick now exists for.
    """
    from app.tasks.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["scan-recruiter-inboxes"]

    assert settings.recruiter_scan_interval_seconds == 300
    assert entry["schedule"] == 300.0
    # A tick still queued when its replacement is due has nothing left to
    # contribute — the newer scan sees everything it would have.
    assert entry["options"]["expires"] == 300.0


def test_a_mailbox_read_seconds_ago_is_not_read_again(db_session, current_user, watched):
    """Beat, Gmail push and the "scan now" button all aim at one mailbox.

    At a one-minute cadence a scan slower than the interval would have its
    successor queued behind it, and a slow mailbox becomes a queue that never
    drains. One gate in front of all three callers is what prevents that.
    """
    watched.last_scan_at = datetime.now(UTC) - timedelta(seconds=5)

    assert recruiter_reply_tasks.recently_scanned(watched) is True


def test_a_mailbox_read_long_enough_ago_is_read_again(db_session, current_user, watched):
    watched.last_scan_at = datetime.now(UTC) - timedelta(
        seconds=settings.recruiter_scan_min_gap_seconds + 5
    )

    assert recruiter_reply_tasks.recently_scanned(watched) is False


def test_a_mailbox_never_scanned_is_never_too_soon(watched):
    watched.last_scan_at = None

    assert recruiter_reply_tasks.recently_scanned(watched) is False


def test_the_debounce_refuses_the_automatic_callers(
    db_session, current_user, connected_gmail, stub_gmail, watched, monkeypatch
):
    monkeypatch.setattr(recruiter_reply_tasks, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    # On the mailbox, not the preference row: the gate is per mailbox so that one
    # account's recent scan cannot suppress another account's.
    connected_gmail.last_scan_at = datetime.now(UTC)
    db_session.commit()
    stub_gmail["m1"] = gmail_message("m1")

    outcome = recruiter_reply_tasks.scan_mailbox(current_user.id, connected_gmail.id)

    assert outcome["status"] == "too_soon"
    assert db_session.query(RecruiterEmail).count() == 0


def test_the_user_pressing_scan_now_is_never_refused(
    db_session, current_user, connected_gmail, stub_gmail, watched, monkeypatch
):
    """They asked for a scan. The debounce is for callers with no opinion on when."""
    monkeypatch.setattr(recruiter_reply_tasks, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        recruiter_reply_tasks.process_recruiter_email, "delay", lambda *a: None
    )
    connected_gmail.last_scan_at = datetime.now(UTC)
    db_session.commit()
    stub_gmail["m1"] = gmail_message("m1")

    outcome = recruiter_reply_tasks.scan_mailbox(
        current_user.id, connected_gmail.id, force=True
    )

    assert outcome["detected"] == 1


def test_a_push_about_an_untracked_thread_wakes_the_inbound_scanner(
    db_session, current_user, connected_gmail, monkeypatch
):
    """A recruiter writing in for the first time arrives on no thread of ours.

    The push handler used to skip exactly that message — "mail this product
    didn't send" — which is the one message this product most wants to see. The
    beat sweep would find it within five minutes; this makes it seconds.
    """
    from app.tasks import inbox_tasks

    monkeypatch.setattr(settings, "recruiter_reply_enabled", True)
    monkeypatch.setattr(settings, "celery_enabled", True)
    scanned: list[tuple] = []
    monkeypatch.setattr(
        recruiter_reply_tasks.scan_mailbox,
        "apply_async",
        lambda args=None, kwargs=None: scanned.append((tuple(args or ()), kwargs or {})),
    )

    inbox_tasks._scan_for_inbound(connected_gmail)

    assert scanned == [((current_user.id, connected_gmail.id), {"trigger": "push"})]


def test_a_push_triggered_scan_is_tagged_as_such(
    db_session, current_user, connected_gmail, monkeypatch
):
    """Whether push is doing the work has to be answerable without a log search.

    Making beat a five-minute fallback is only safe if "push is delivering" can
    be checked, and the scan-run row's trigger is how the stats view answers it.
    """
    from app.tasks import inbox_tasks

    monkeypatch.setattr(settings, "recruiter_reply_enabled", True)
    monkeypatch.setattr(settings, "celery_enabled", True)
    seen: list = []
    monkeypatch.setattr(
        recruiter_reply_tasks.scan_mailbox,
        "apply_async",
        lambda args=None, kwargs=None: seen.append((kwargs or {}).get("trigger")),
    )

    inbox_tasks._scan_for_inbound(connected_gmail)

    assert seen == ["push"]


def test_the_push_hook_is_quiet_when_the_feature_is_off(
    db_session, connected_gmail, monkeypatch
):
    """Best-effort in every direction: beat covers the same ground regardless."""
    from app.tasks import inbox_tasks

    monkeypatch.setattr(settings, "recruiter_reply_enabled", False)
    scanned: list = []
    monkeypatch.setattr(
        recruiter_reply_tasks.scan_mailbox,
        "apply_async",
        lambda *a, **kw: scanned.append((a, kw)),
    )

    inbox_tasks._scan_for_inbound(connected_gmail)

    assert scanned == []
