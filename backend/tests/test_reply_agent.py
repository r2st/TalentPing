"""Thread-aware reply drafting.

The behaviours worth pinning are the ones that separate this from drafting off
the latest message: that the whole conversation reaches the prompt, that a
below-market number turns an offer into a negotiation, and — most of all — that
a model which invents a Tuesday or a salary figure gets thrown away. The last
one is the feature's safety property: a draft the candidate has to walk back is
worse than a plainer template.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.email import ReplyIntent, ReplyTemplate
from app.services import reply_agent
from app.services.ai_composer import CandidateContext
from app.services.openrouter_client import OpenRouterError
from app.services.salary_service import SalaryBand


def msg(direction: str, body: str) -> reply_agent.ThreadMessage:
    return reply_agent.ThreadMessage(direction=direction, body=body)


def band(minimum=150_000, median=180_000, maximum=210_000) -> SalaryBand:
    return SalaryBand(
        role_family="software_engineering",
        role_label="Software Engineer",
        seniority="senior",
        location_key="us_remote",
        location_label="US (remote)",
        currency="USD",
        minimum=minimum,
        median=median,
        maximum=maximum,
    )


CAND = CandidateContext(name="Ada Lovelace")


class TestTranscript:
    def test_renders_both_sides_oldest_first(self):
        text = reply_agent.transcript(
            [
                msg("recruiter", "Are you open to a chat?"),
                msg("candidate", "Yes — Tuesday works for me."),
                msg("recruiter", "Great, what about compensation?"),
            ]
        )
        assert text.index("Are you open") < text.index("Tuesday works")
        assert text.index("Tuesday works") < text.index("what about compensation")
        assert "RECRUITER" in text and "CANDIDATE" in text

    def test_keeps_only_the_tail_of_a_long_thread(self):
        many = [msg("recruiter", f"message {i}") for i in range(40)]
        text = reply_agent.transcript(many)
        assert "message 39" in text
        assert "message 0\n" not in text

    def test_truncates_an_enormous_message(self):
        text = reply_agent.transcript([msg("recruiter", "x" * 9000)])
        assert len(text) < 2000


class TestOpenQuestions:
    def test_pulls_questions_from_the_latest_recruiter_message(self):
        found = reply_agent.open_questions(
            [
                msg("recruiter", "Old question that was already answered?"),
                msg("candidate", "Answered it."),
                msg("recruiter", "What is your notice period? And are you US-based?"),
            ]
        )
        assert any("notice period" in q for q in found)
        assert any("US-based" in q for q in found)
        assert not any("Old question" in q for q in found)

    def test_no_questions_when_the_recruiter_only_made_statements(self):
        assert reply_agent.open_questions([msg("recruiter", "Thanks, we'll be in touch.")]) == []


class TestSalaryExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("The base salary is $150,000", [150_000]),
            ("We're offering 120k base", [120_000]),
            ("compensation band is 90000 to 110000", [90_000, 110_000]),
            ("salary around €95k", [95_000]),
        ],
    )
    def test_reads_money_when_the_message_is_about_money(self, text, expected):
        assert reply_agent.extract_salary_figures(text) == expected

    def test_ignores_numbers_in_a_message_that_is_not_about_pay(self):
        assert reply_agent.extract_salary_figures("The team has 5 engineers in 2024") == []

    def test_ignores_implausible_figures(self):
        # "a budget of 12" is not an annual salary, and neither is a year.
        assert reply_agent.extract_salary_figures("our budget is 12 and the year 2024") == []


class TestNegotiationDetection:
    def test_below_median_offer_is_a_negotiation(self):
        found, note = reply_agent.detect_negotiation(
            "We can offer a base salary of $150,000.", band(median=180_000)
        )
        assert found is True
        assert "150,000" in note and "180,000" in note

    def test_at_or_above_median_is_not(self):
        found, _ = reply_agent.detect_negotiation(
            "We can offer a base salary of $190,000.", band(median=180_000)
        )
        assert found is False

    def test_no_band_means_no_signal(self):
        found, _ = reply_agent.detect_negotiation("base salary of $150,000", None)
        assert found is False

    def test_a_thread_with_no_money_is_not_a_negotiation(self):
        found, _ = reply_agent.detect_negotiation("Can we talk Thursday?", band())
        assert found is False


class TestTemplateChoice:
    def test_negotiation_overrides_the_classified_intent(self):
        assert (
            reply_agent.choose_template(ReplyIntent.OFFER, negotiation=True)
            is ReplyTemplate.SALARY_NEGOTIATION
        )

    def test_candidate_spoke_last_means_follow_up(self):
        assert (
            reply_agent.choose_template(
                ReplyIntent.INTERESTED, messages=[msg("candidate", "Following up.")]
            )
            is ReplyTemplate.FOLLOW_UP
        )

    @pytest.mark.parametrize(
        "intent,expected",
        [
            (ReplyIntent.SCHEDULING, ReplyTemplate.SCHEDULING),
            (ReplyIntent.NOT_INTERESTED, ReplyTemplate.DECLINING),
            (ReplyIntent.QUESTION, ReplyTemplate.QUESTION),
            (ReplyIntent.INTERESTED, ReplyTemplate.INTERESTED),
        ],
    )
    def test_maps_intent_to_brief(self, intent, expected):
        assert (
            reply_agent.choose_template(intent, messages=[msg("recruiter", "hi")]) is expected
        )


class TestDraftReply:
    def test_falls_back_to_a_template_without_a_key(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "")
        draft = reply_agent.draft_reply(
            CAND, [msg("recruiter", "We'd love to chat!")], intent=ReplyIntent.INTERESTED
        )
        assert draft.generated_with == "heuristic"
        assert "Ada Lovelace" in draft.body
        assert draft.template is ReplyTemplate.INTERESTED

    def test_falls_back_when_the_provider_fails(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        with patch(
            "app.services.reply_agent.chat_completion", side_effect=OpenRouterError("down")
        ):
            draft = reply_agent.draft_reply(CAND, [msg("recruiter", "Interested?")])
        assert draft.generated_with == "heuristic"

    def test_uses_the_model_when_the_draft_is_clean(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        with patch(
            "app.services.reply_agent.chat_completion",
            return_value="Thanks for reaching out — happy to find a time that suits you.",
        ):
            draft = reply_agent.draft_reply(
                CAND, [msg("recruiter", "Can we talk?")], intent=ReplyIntent.SCHEDULING
            )
        assert draft.generated_with == "llm"
        assert "happy to find a time" in draft.body

    def test_whole_thread_reaches_the_prompt(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        with patch(
            "app.services.reply_agent.chat_completion", return_value="Sounds good."
        ) as call:
            reply_agent.draft_reply(
                CAND,
                [
                    msg("recruiter", "Are you open to a chat?"),
                    msg("candidate", "I already sent my availability."),
                    msg("recruiter", "Remind me what it was?"),
                ],
            )
        prompt = call.call_args[0][0][1]["content"]
        # The middle turn is the one a latest-message-only drafter would lose.
        assert "already sent my availability" in prompt
        assert "Are you open to a chat" in prompt

    def test_rejects_a_draft_that_invents_a_date(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        with patch(
            "app.services.reply_agent.chat_completion",
            return_value="I'm free Tuesday at 3:00 pm — does that work?",
        ):
            draft = reply_agent.draft_reply(
                CAND, [msg("recruiter", "When are you free?")], intent=ReplyIntent.SCHEDULING
            )
        assert draft.generated_with == "heuristic"
        assert "Tuesday" not in draft.body

    def test_echoing_a_date_the_recruiter_proposed_is_allowed(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        with patch(
            "app.services.reply_agent.chat_completion",
            return_value="Tuesday works well for me — see you then.",
        ):
            draft = reply_agent.draft_reply(
                CAND,
                [msg("recruiter", "Would Tuesday suit you?")],
                intent=ReplyIntent.SCHEDULING,
            )
        assert draft.generated_with == "llm"

    def test_negotiation_draft_may_not_state_a_counter_figure(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        with patch(
            "app.services.reply_agent.chat_completion",
            return_value=(
                "Thanks! Given the scope I was targeting a base salary closer to "
                "$200,000 — can we discuss?"
            ),
        ):
            draft = reply_agent.draft_reply(
                CAND,
                [msg("recruiter", "We can offer a base salary of $150,000.")],
                intent=ReplyIntent.OFFER,
                band=band(median=180_000),
            )
        assert draft.template is ReplyTemplate.SALARY_NEGOTIATION
        # The invented number is the candidate's negotiating position, made up
        # for them. The template says the same thing without naming a figure.
        assert draft.generated_with == "heuristic"
        assert "200,000" not in draft.body

    def test_negotiation_draft_may_repeat_the_offered_figure(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        with patch(
            "app.services.reply_agent.chat_completion",
            return_value=(
                "Thank you for the $150,000 offer — I'd like to talk through the "
                "range before we go further."
            ),
        ):
            draft = reply_agent.draft_reply(
                CAND,
                [msg("recruiter", "We can offer a base salary of $150,000.")],
                intent=ReplyIntent.OFFER,
                band=band(median=180_000),
            )
        assert draft.generated_with == "llm"
        assert draft.negotiation_detected is True

    def test_market_band_reaches_the_negotiation_prompt(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        with patch(
            "app.services.reply_agent.chat_completion", return_value="Let's discuss the range."
        ) as call:
            reply_agent.draft_reply(
                CAND,
                [msg("recruiter", "The base salary is $150,000.")],
                intent=ReplyIntent.OFFER,
                band=band(minimum=150_000, median=180_000, maximum=210_000),
            )
        prompt = call.call_args[0][0][1]["content"]
        assert "180,000" in prompt
        assert "Do NOT state a counter-offer" in prompt

    def test_note_explains_the_angle(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "")
        draft = reply_agent.draft_reply(
            CAND,
            [msg("recruiter", "We can offer $150,000.")],
            intent=ReplyIntent.OFFER,
            band=band(median=180_000),
        )
        assert "under" in draft.note and "median" in draft.note


class TestDraftForThread:
    class _Thread:
        def __init__(self, emails):
            self.emails = emails

    class _Email:
        def __init__(self, direction, body):
            from app.models.email import EmailDirection

            self.direction = (
                EmailDirection.RECEIVED if direction == "recruiter" else EmailDirection.SENT
            )
            self.body_text = body
            self.subject = None
            self.to_email = None

    def test_appends_a_pending_inbound_message(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        thread = self._Thread([self._Email("candidate", "Applied last week.")])
        with patch(
            "app.services.reply_agent.chat_completion", return_value="Great, thanks!"
        ) as call:
            reply_agent.draft_for_thread(
                CAND, thread, latest_inbound="Thanks for applying — are you free to chat?"
            )
        prompt = call.call_args[0][0][1]["content"]
        assert "are you free to chat" in prompt
        assert "Applied last week" in prompt

    def test_the_quoted_thread_is_stripped_out_of_the_transcript(self, monkeypatch):
        """Stored bodies carry every earlier message quoted underneath them."""
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        thread = self._Thread(
            [
                self._Email(
                    "recruiter",
                    "Happy to chat.\n\n"
                    "On Wed, Jul 29, 2026 at 4:12 PM Ada <ada@example.com> wrote:\n"
                    "> I'd love to hear about openings on your platform team.\n",
                )
            ]
        )
        with patch(
            "app.services.reply_agent.chat_completion", return_value="Sounds good."
        ) as call:
            reply_agent.draft_for_thread(CAND, thread)

        prompt = call.call_args[0][0][1]["content"]
        assert "Happy to chat" in prompt
        assert "love to hear about openings" not in prompt

    def test_our_own_quoted_question_is_not_read_as_theirs(self, monkeypatch):
        """The open-questions pass scans the latest inbound message for "?", and
        our outreach is quoted inside it — so the draft answered its own
        questions back to the recruiter."""
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        thread = self._Thread([])
        with patch(
            "app.services.reply_agent.chat_completion", return_value="Thanks."
        ) as call:
            reply_agent.draft_for_thread(
                CAND,
                thread,
                latest_inbound=(
                    "We have no openings right now.\n\n"
                    "On Wed, Jul 29, 2026 at 4:12 PM Ada <ada@example.com> wrote:\n"
                    "> Are you available for a short call this week?\n"
                ),
            )

        prompt = call.call_args[0][0][1]["content"]
        assert "Are you available for a short call" not in prompt

    def test_stripping_keeps_the_duplicate_check_working(self, monkeypatch):
        """The stored copy and the pending one quote differently; they are still
        the same turn and must not both appear."""
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        thread = self._Thread(
            [
                self._Email(
                    "recruiter",
                    "Are you free to chat?\n\n"
                    "On Wed, Jul 29, 2026 at 4:12 PM Ada <ada@example.com> wrote:\n"
                    "> Hello there.\n",
                )
            ]
        )
        with patch(
            "app.services.reply_agent.chat_completion", return_value="Yes."
        ) as call:
            reply_agent.draft_for_thread(
                CAND, thread, latest_inbound="Are you free to chat?\n\n--\nJane\n"
            )

        conversation = call.call_args[0][0][1]["content"].split("== STILL UNANSWERED")[0]
        assert conversation.count("Are you free to chat?") == 1

    def test_does_not_duplicate_an_already_flushed_message(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "k")
        thread = self._Thread([self._Email("recruiter", "Are you free to chat?")])
        with patch(
            "app.services.reply_agent.chat_completion", return_value="Yes."
        ) as call:
            reply_agent.draft_for_thread(
                CAND, thread, latest_inbound="Are you free to chat?"
            )
        prompt = call.call_args[0][0][1]["content"]
        # It legitimately appears again under "STILL UNANSWERED"; what must not
        # happen is the same turn being shown twice in the conversation itself.
        conversation = prompt.split("== STILL UNANSWERED")[0]
        assert conversation.count("Are you free to chat?") == 1

    def test_skips_messages_with_no_body(self, monkeypatch):
        monkeypatch.setattr("app.services.reply_agent.settings.openrouter_api_key", "")
        thread = self._Thread(
            [self._Email("candidate", ""), self._Email("recruiter", "Interested?")]
        )
        messages = reply_agent.thread_messages(thread)
        assert len(messages) == 1
        assert messages[0].is_recruiter
