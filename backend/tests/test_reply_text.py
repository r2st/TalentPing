"""Stripping the quoted thread off an inbound reply.

The rule under test is not "remove quotes" but "never lose the human's words":
every case here checks both that the quote went and that the reply stayed.
"""
from __future__ import annotations

from app.services.reply_text import visible_text

GMAIL_QUOTE = """Thanks for reaching out — we'll pass for now.

On Wed, Jul 29, 2026 at 4:12 PM Alex Candidate <alex@example.com> wrote:

> Hi Jane, I'd love to hear about any openings on your platform team.
> Are you available for a short call this week?
"""

OUTLOOK_QUOTE = """No openings at the moment, sorry.

________________________________
From: Alex Candidate <alex@example.com>
Sent: Wednesday, July 29, 2026 4:12 PM
To: Jane Recruiter <jane@acme.com>
Subject: Backend engineer

Hi Jane, are you available for a short call this week?
"""

ORIGINAL_MESSAGE_QUOTE = """Not a fit for this role.

-----Original Message-----
From: Alex Candidate
Sent: Wednesday, July 29, 2026

I'd love to chat about your openings.
"""


def test_gmail_style_quote_is_removed():
    assert visible_text(GMAIL_QUOTE) == "Thanks for reaching out — we'll pass for now."


def test_outlook_header_block_is_removed():
    assert visible_text(OUTLOOK_QUOTE) == "No openings at the moment, sorry."


def test_original_message_banner_is_removed():
    assert visible_text(ORIGINAL_MESSAGE_QUOTE) == "Not a fit for this role."


def test_wrapped_on_wrote_line_is_removed():
    """Gmail wraps the attribution line; the closing "wrote:" lands on its own."""
    body = (
        "Happy to chat next week.\n\n"
        "On Wed, Jul 29, 2026 at 4:12 PM Alex Candidate <alex@example.com>\n"
        "wrote:\n\n"
        "> Are you available for a call?\n"
    )
    assert visible_text(body) == "Happy to chat next week."


def test_signature_delimiter_is_removed():
    body = "Sounds good, let's talk.\n\n--\nJane Recruiter\nAcme Talent\n+1 555 0100"
    assert visible_text(body) == "Sounds good, let's talk."


def test_phone_signature_is_removed():
    body = "Calling you at 3.\n\nSent from my iPhone"
    assert visible_text(body) == "Calling you at 3."


def test_bottom_posted_reply_survives():
    """Cutting at the first marker would leave nothing — the fallback keeps it."""
    body = (
        "On Wed, Jul 29, 2026 at 4:12 PM Alex Candidate <alex@example.com> wrote:\n"
        "> Are you available for a short call this week?\n"
        "> Thanks, Alex\n"
        "\n"
        "We have no openings right now.\n"
    )
    assert visible_text(body) == "We have no openings right now."


def test_body_that_is_only_a_quote_is_returned_rather_than_emptied():
    body = "> Are you available for a call?\n> Thanks, Alex\n"
    assert visible_text(body).startswith(">")


def test_a_from_line_that_is_not_a_header_block_is_kept():
    """"From:" only starts a quote when the sibling headers follow it."""
    body = "From: what I understood of the spec, this is a platform role. Correct?"
    assert visible_text(body) == body


def test_crlf_and_blank_runs_are_normalised():
    assert visible_text("Yes.\r\n\r\n\r\n\r\nLet's talk.\r\n") == "Yes.\n\nLet's talk."


def test_empty_input():
    assert visible_text(None) == ""
    assert visible_text("   \n  ") == ""
