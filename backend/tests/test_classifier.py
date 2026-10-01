"""Reply-intent classifier (rule-based fallback path, no LLM key in tests)."""
from __future__ import annotations

import pytest

from app.models.email import ReplyIntent
from app.services import reply_classifier
from app.services.reply_classifier import classify_reply


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Please unsubscribe me from this list.", ReplyIntent.UNSUBSCRIBE),
        ("I am out of office until next Monday.", ReplyIntent.OUT_OF_OFFICE),
        ("How about Tuesday at 2pm for a call?", ReplyIntent.SCHEDULING),
        ("Thanks, but we have no openings right now.", ReplyIntent.NOT_INTERESTED),
        ("This sounds great, let's talk!", ReplyIntent.INTERESTED),
        ("", ReplyIntent.OTHER),
        ("k", ReplyIntent.OTHER),
    ],
)
def test_rule_based_classification(text, expected):
    assert classify_reply(text) == expected


def test_ambiguous_defaults_to_other():
    assert classify_reply("Received, will review internally and circle back.") == (
        ReplyIntent.OTHER
    )


# --------------------------------------------------------------------------- #
# The quoted thread must not classify the reply                                #
# --------------------------------------------------------------------------- #
# Our own outreach is written in the vocabulary this classifier keys on, and it
# comes back quoted under every reply. These are the cases that were wrong.


def test_rejection_quoting_our_invitation_is_not_scheduling():
    body = (
        "Thanks for reaching out — we'll pass for now.\n\n"
        "On Wed, Jul 29, 2026 at 4:12 PM Alex <alex@example.com> wrote:\n"
        "> I'd love to hear about openings on your platform team.\n"
        "> Are you available for a short call this week?\n"
    )
    assert classify_reply(body) == ReplyIntent.NOT_INTERESTED


def test_out_of_office_quoting_our_invitation_stays_out_of_office():
    body = (
        "I am on annual leave until 12 August with no access to email.\n\n"
        "-----Original Message-----\n"
        "From: Alex Candidate\n\n"
        "Are you available for a call? I'd love to chat.\n"
    )
    assert classify_reply(body) == ReplyIntent.OUT_OF_OFFICE


def test_our_own_signature_does_not_classify_the_reply():
    body = "Passing you on to our hiring manager.\n\n--\nJane, keen to hire\n"
    assert classify_reply(body) == ReplyIntent.INTERESTED


# --------------------------------------------------------------------------- #
# Weighted scoring beats first-match ordering                                  #
# --------------------------------------------------------------------------- #


def test_a_reschedule_is_not_a_rejection():
    """"unfortunately" is a hedge word, not a decision — it must not outrank a
    concrete proposal in the same sentence."""
    text = "Unfortunately Tuesday no longer works — how about Thursday at 3pm?"
    assert classify_reply(text) == ReplyIntent.SCHEDULING


def test_a_lone_hedge_word_decides_nothing():
    assert classify_reply("Unfortunately I only saw this now.") == ReplyIntent.OTHER


def test_negated_interest_is_a_rejection():
    assert classify_reply("We are not interested at this time.") == (
        ReplyIntent.NOT_INTERESTED
    )


def test_role_filled_is_a_rejection():
    assert classify_reply("That position has been filled, sorry!") == (
        ReplyIntent.NOT_INTERESTED
    )


def test_offer_outranks_the_scheduling_phrasing_around_it():
    text = "We are pleased to offer you the role. Can we set up a call to walk through it?"
    assert classify_reply(text) == ReplyIntent.OFFER


@pytest.mark.parametrize(
    "text",
    [
        "What are your salary expectations for this role?",
        "Could you send over your notice period and visa status?",
        "Are you authorized to work in the US without sponsorship?",
    ],
)
def test_questions_are_classified_as_questions(text):
    assert classify_reply(text) == ReplyIntent.QUESTION


def test_calendly_link_is_scheduling():
    assert classify_reply("Grab any slot here: https://calendly.com/jane/30min") == (
        ReplyIntent.SCHEDULING
    )


# --------------------------------------------------------------------------- #
# Automatic messages never reach the model                                     #
# --------------------------------------------------------------------------- #


def test_unsubscribe_short_circuits_before_any_llm_call(monkeypatch):
    """An unsubscribe is too consequential to spend a flaky model call on."""
    monkeypatch.setattr(reply_classifier.settings, "openrouter_api_key", "test-key")

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the classifier model was called")

    monkeypatch.setattr(reply_classifier, "chat_completion", _explode)
    assert classify_reply("Please remove me from your list.") == ReplyIntent.UNSUBSCRIBE


def test_llm_verdict_wins_for_judgement_calls(monkeypatch):
    monkeypatch.setattr(reply_classifier.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(
        reply_classifier, "chat_completion", lambda *a, **k: "INTERESTED"
    )
    assert classify_reply("Circling back on this.") == ReplyIntent.INTERESTED


def test_llm_outage_falls_back_to_the_rules(monkeypatch):
    from app.services.openrouter_client import OpenRouterError

    monkeypatch.setattr(reply_classifier.settings, "openrouter_api_key", "test-key")

    def _fail(*args, **kwargs):
        raise OpenRouterError("provider down")

    monkeypatch.setattr(reply_classifier, "chat_completion", _fail)
    assert classify_reply("We have no openings right now.") == ReplyIntent.NOT_INTERESTED


def test_llm_is_only_shown_the_visible_reply(monkeypatch):
    """The quoted thread is stripped before the prompt, not just before the rules."""
    seen: dict[str, str] = {}

    monkeypatch.setattr(reply_classifier.settings, "openrouter_api_key", "test-key")

    def _capture(messages, **kwargs):
        seen["text"] = messages[-1]["content"]
        return "NOT_INTERESTED"

    monkeypatch.setattr(reply_classifier, "chat_completion", _capture)
    classify_reply(
        "We'll pass.\n\nOn Wed, Jul 29, 2026 at 4:12 PM Alex <a@example.com> wrote:\n"
        "> Are you available for a short call?\n"
    )
    assert seen["text"] == "We'll pass."


def test_garbage_llm_output_falls_back_to_the_rules(monkeypatch):
    monkeypatch.setattr(reply_classifier.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(reply_classifier, "chat_completion", lambda *a, **k: "MAYBE?")
    assert classify_reply("Sounds great, let's talk.") == ReplyIntent.INTERESTED
