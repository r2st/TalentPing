"""Screening-question answering — the part that must never invent anything.

Three properties are worth more than the rest put together, and each has tests
here that would fail loudly if the behaviour regressed:

* a legally significant question (sponsorship, work authorization, salary) is
  answered from the candidate's own stated answer or not at all;
* a demographic question is declined, never answered;
* a model's reply is validated against the question it was asked, so it cannot
  smuggle an answer into a question it was told to leave alone.
"""
from __future__ import annotations

import json

import pytest

from app.models.form_apply import FormApplyProfile
from app.services.form_answers import (
    SOURCE_BANK,
    SOURCE_DECLINED,
    SOURCE_LLM,
    SOURCE_RESUME,
    SOURCE_UNANSWERED,
    AnswerBank,
    ScreeningQuestion,
    answer_from_bank,
    answer_from_resume,
    answer_questions,
    answer_with_llm,
    blocking_questions,
    decline_option,
    is_eeo,
    pick_option,
)

YES_NO = ("Yes", "No")


def _bank(**overrides) -> AnswerBank:
    base = {
        "work_authorized": True,
        "requires_sponsorship": False,
        "willing_to_relocate": True,
        "notice_period_days": 30,
        "desired_salary": "$180,000",
        "earliest_start": "1 September",
        "linkedin_url": "https://linkedin.com/in/jordanc",
        "github_url": "https://github.com/jordanc",
        "phone": "+1 415 555 0142",
        "years_experience": 8,
    }
    base.update(overrides)
    return AnswerBank(**base)


def q(text, **kwargs) -> ScreeningQuestion:
    return ScreeningQuestion(text=text, **kwargs)


class TestPickOption:
    def test_matches_a_boolean_to_yes_or_no(self):
        assert pick_option(YES_NO, True) == "Yes"
        assert pick_option(YES_NO, False) == "No"

    def test_matches_a_string_case_insensitively(self):
        assert pick_option(("Remote", "Hybrid", "On-site"), "remote") == "Remote"

    def test_returns_none_when_nothing_expresses_the_answer(self):
        # A select we can't map is a question we haven't answered — never the
        # first option as a consolation prize.
        assert pick_option(("Berlin", "Munich"), True) is None
        assert pick_option((), "anything") is None


class TestBankAnswers:
    def test_sponsorship_comes_from_the_candidate(self):
        answer = answer_from_bank(
            q("Will you now or in the future require visa sponsorship?", options=YES_NO),
            _bank(),
        )
        assert answer is not None
        assert answer.value == "No" and answer.source == SOURCE_BANK

    def test_sponsorship_is_matched_before_work_authorization(self):
        # Both rules could plausibly fire on a form that says "sponsorship to
        # work"; answering the wrong one inverts the meaning.
        answer = answer_from_bank(
            q("Do you require sponsorship to work in the US?", options=YES_NO),
            _bank(requires_sponsorship=True, work_authorized=False),
        )
        assert answer is not None and answer.value == "Yes"

    def test_work_authorization_comes_from_the_candidate(self):
        answer = answer_from_bank(
            q("Are you legally authorized to work in the United States?", options=YES_NO),
            _bank(),
        )
        assert answer is not None and answer.value == "Yes"

    def test_an_unstated_answer_is_left_unanswered(self):
        # The candidate hasn't said. Nothing may fill this in for them.
        assert (
            answer_from_bank(
                q("Are you legally authorized to work here?", options=YES_NO),
                _bank(work_authorized=None),
            )
            is None
        )

    def test_salary_expectations_come_from_the_candidate(self):
        answer = answer_from_bank(q("What are your salary expectations?"), _bank())
        assert answer is not None and answer.value == "$180,000"

    def test_notice_period_is_rendered_in_days(self):
        answer = answer_from_bank(q("What is your notice period?"), _bank())
        assert answer is not None and answer.value == "30 days"

    def test_a_custom_answer_beats_every_rule(self):
        bank = _bank(custom={"salary expectations": "Open to discussion"})
        answer = answer_from_bank(q("What are your salary expectations?"), bank)
        assert answer is not None
        assert answer.value == "Open to discussion"
        assert answer.note == "custom answer"

    def test_free_text_yes_no_when_the_form_offers_no_options(self):
        answer = answer_from_bank(q("Are you willing to relocate?"), _bank())
        assert answer is not None and answer.value == "Yes"


class TestEeoQuestions:
    @pytest.mark.parametrize(
        "text",
        [
            "What is your gender?",
            "Please select your race/ethnicity",
            "Are you a protected veteran?",
            "Do you have a disability?",
            "Voluntary self-identification",
        ],
    )
    def test_recognised_as_demographic(self, text):
        assert is_eeo(q(text))

    def test_the_forms_own_decline_option_is_selected(self):
        answer = answer_from_bank(
            q(
                "What is your gender?",
                options=("Male", "Female", "I don't wish to answer"),
            ),
            _bank(),
        )
        assert answer is not None
        assert answer.value == "I don't wish to answer"
        assert answer.source == SOURCE_DECLINED

    def test_left_blank_when_there_is_no_decline_option(self):
        answer = answer_from_bank(
            q("What is your gender?", options=("Male", "Female")), _bank()
        )
        assert answer is not None
        assert answer.value is None and answer.source == SOURCE_DECLINED

    def test_decline_option_finds_the_usual_phrasings(self):
        for option in ("Decline to self-identify", "I prefer not to say", "Choose not to disclose"):
            assert decline_option(("A", "B", option)) == option


class TestResumeAnswers:
    def test_years_of_experience_comes_off_the_resume(self, resume):
        answer = answer_from_resume(
            q("How many years of experience do you have?"), resume, _bank()
        )
        assert answer is not None
        assert answer.value == "8" and answer.source == SOURCE_RESUME

    def test_a_banded_select_picks_the_band_the_candidate_falls_in(self, resume):
        answer = answer_from_resume(
            q(
                "How many years of Python experience do you have?",
                options=("0-2 years", "3-5 years", "6-10 years", "10+ years"),
                field_type="select",
            ),
            resume,
            _bank(),
        )
        assert answer is not None and answer.value == "6-10 years"

    def test_other_questions_are_not_answered_from_the_resume(self, resume):
        assert answer_from_resume(q("Why do you want this job?"), resume, _bank()) is None


class TestLlmAnswers:
    def test_parses_and_matches_answers_to_their_questions(self, resume):
        questions = [q("Why do you want to work here?"), q("Describe a system you scaled.")]
        payload = json.dumps(
            {
                "answers": [
                    {"index": 0, "answer": "Your payments work matches mine."},
                    {"index": 1, "answer": "I ran the ledger service at Acme."},
                ]
            }
        )
        answers = answer_with_llm(questions, resume, completion=lambda *a, **k: payload)
        assert answers[0].startswith("Your payments work")
        assert answers[1].startswith("I ran the ledger")

    def test_a_null_answer_is_dropped_rather_than_invented(self, resume):
        payload = json.dumps({"answers": [{"index": 0, "answer": None}]})
        assert answer_with_llm(
            [q("Do you hold a security clearance?")],
            resume,
            completion=lambda *a, **k: payload,
        ) == {}

    def test_a_restricted_question_is_never_answered_by_the_model(self, resume):
        # The prompt forbids it; this is the second line of defence, for a model
        # that answers anyway.
        payload = json.dumps(
            {"answers": [{"index": 0, "answer": "No sponsorship needed"}]}
        )
        answers = answer_with_llm(
            [q("Will you require visa sponsorship?")],
            resume,
            completion=lambda *a, **k: payload,
        )
        assert answers == {}

    def test_an_option_answer_must_be_one_of_the_options(self, resume):
        payload = json.dumps({"answers": [{"index": 0, "answer": "Maybe"}]})
        answers = answer_with_llm(
            [q("Can you work on-site?", options=YES_NO, field_type="select")],
            resume,
            completion=lambda *a, **k: payload,
        )
        assert answers == {}

    def test_a_dead_model_answers_nothing_rather_than_raising(self, resume):
        from app.services.openrouter_client import OpenRouterError

        def dead(*_args, **_kwargs):
            raise OpenRouterError("all providers failed")

        assert answer_with_llm([q("Why us?")], resume, completion=dead) == {}

    def test_unparseable_output_answers_nothing(self, resume):
        assert answer_with_llm(
            [q("Why us?")], resume, completion=lambda *a, **k: "I think, therefore…"
        ) == {}


class TestAnswerQuestions:
    def test_uses_the_cheapest_tier_that_can_answer(self, resume):
        questions = [
            q("Are you legally authorized to work in the US?", options=YES_NO),
            q("How many years of experience do you have?"),
            q("Why do you want to work here?"),
        ]
        payload = json.dumps({"answers": [{"index": 0, "answer": "Payments, mostly."}]})
        answers = answer_questions(
            questions,
            bank=_bank(),
            resume=resume,
            completion=lambda *a, **k: payload,
        )
        assert [a.source for a in answers] == [SOURCE_BANK, SOURCE_RESUME, SOURCE_LLM]

    def test_the_model_only_sees_what_the_earlier_tiers_could_not_answer(self, resume):
        seen: dict = {}

        def spy(messages, **_kwargs):
            seen["prompt"] = messages[-1]["content"]
            return json.dumps({"answers": []})

        answer_questions(
            [
                q("Are you legally authorized to work in the US?", options=YES_NO),
                q("Why do you want to work here?"),
            ],
            bank=_bank(),
            resume=resume,
            completion=spy,
        )
        assert "Why do you want to work here?" in seen["prompt"]
        assert "legally authorized" not in seen["prompt"]

    def test_an_unanswerable_question_comes_back_unanswered(self, resume):
        answers = answer_questions(
            [q("What is your employee ID at our company?")],
            bank=_bank(),
            resume=resume,
            completion=lambda *a, **k: json.dumps({"answers": []}),
        )
        assert answers[0].source == SOURCE_UNANSWERED
        assert not answers[0].answered

    def test_disabling_the_model_skips_it_entirely(self, resume):
        def explode(*_args, **_kwargs):  # pragma: no cover - must never run
            raise AssertionError("the model should not have been called")

        answers = answer_questions(
            [q("Why do you want to work here?")],
            bank=_bank(llm_enabled=False),
            resume=resume,
            completion=explode,
        )
        assert answers[0].source == SOURCE_UNANSWERED

    def test_blocking_questions_are_the_required_unanswered_ones(self, resume):
        answers = answer_questions(
            [
                q("What is your employee ID?", required=True),
                q("Anything else to add?", required=False),
            ],
            bank=_bank(),
            resume=resume,
            completion=lambda *a, **k: json.dumps({"answers": []}),
        )
        assert blocking_questions(answers) == ["What is your employee ID?"]


class TestAnswerBankFromModels:
    def test_reads_the_profile_and_falls_back_to_the_resume(self, resume, current_user):
        resume.links = ["https://linkedin.com/in/jordanc", "https://jordan.dev"]
        profile = FormApplyProfile(
            user_id=current_user.id,
            work_authorized=True,
            requires_sponsorship=False,
            desired_salary="$200k",
            custom_answers={"favourite editor": "vim"},
        )
        bank = AnswerBank.from_models(profile, resume)
        assert bank.work_authorized is True
        assert bank.desired_salary == "$200k"
        # Not on the profile, so it comes off the resume.
        assert bank.linkedin_url == "https://linkedin.com/in/jordanc"
        assert bank.years_experience == resume.years_experience
        assert bank.custom == {"favourite editor": "vim"}

    def test_works_with_no_profile_at_all(self, resume):
        bank = AnswerBank.from_models(None, resume)
        assert bank.work_authorized is None
        assert bank.years_experience == resume.years_experience
