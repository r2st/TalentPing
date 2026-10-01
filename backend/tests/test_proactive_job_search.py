"""Proactive job search: finding the roles the feed used to be blind to.

The autopilot already discovered, scored, gated and applied. In production it
reported `scanned: 42, applied: 0, skipped_irrelevant: 42` every hour, on the
same forty-two rows. These are the three reasons, each pinned by a test:

* the pre-filter could not express "AI Engineer", only "engineer";
* the search asked for the candidate's literal words and no others;
* a posting the gates refused was never retired, so every run re-judged it.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import role_expansion
from app.services.job_search_service import JobQuery, RawJob, matches_query, term_words


# --------------------------------------------------------------------------- #
# The pre-filter can name a specialism                                         #
# --------------------------------------------------------------------------- #


def test_short_tokens_survive_the_word_split():
    """`ai`, `ml`, `qa` and `sre` name a field; `the` and `for` do not.

    The old rule kept only tokens longer than three characters, which reads as
    "drop the noise words" and isn't — it dropped exactly the tokens that carry
    the specialism.
    """
    assert term_words("AI Engineer") == ["ai", "engineer"]
    assert term_words("SRE") == ["sre"]
    assert term_words("Head of QA") == ["head", "qa"]
    assert "the" not in term_words("The Backend Engineer")


def _job(title: str, description: str = "") -> RawJob:
    return RawJob(title=title, company="Acme", location="Remote", description=description)


def test_an_ai_role_matches_an_ai_search():
    query = JobQuery(roles=["AI Engineer"])
    assert matches_query(_job("AI Engineer"), query) is True
    assert matches_query(_job("Senior AI Engineer, Platform"), query) is True


def test_a_generic_engineering_post_no_longer_matches_an_ai_search():
    """The bug in the permissive direction.

    With `ai` discarded, the search term became `engineer` alone and a
    two-thirds majority of one word is one word — so every engineering post on
    every board matched a search for an AI role.
    """
    query = JobQuery(roles=["AI Engineer"])
    assert matches_query(_job("Sales Engineer"), query) is False
    assert matches_query(_job("Civil Engineer"), query) is False


def test_the_junk_that_filled_the_production_feed_is_rejected():
    """Real titles from the feed that produced `applied: 0` forty-two times."""
    query = JobQuery(roles=["AI Engineer"])
    for title in (
        "Executive Assistant",
        "Onboarding Specialist",
        "Caretaker",
        "Hand Assembly",
        "THOUGHTFUL NAVIGATOR",
        "We don't currently have any open roles",
    ):
        assert matches_query(_job(title), query) is False, title


def test_a_seniority_qualifier_may_still_go_missing():
    """The two-thirds rule is what the fit scorer relies on; it must survive."""
    query = JobQuery(roles=["Senior Backend Engineer"])
    assert matches_query(_job("Backend Engineer"), query) is True


# --------------------------------------------------------------------------- #
# Role expansion                                                               #
# --------------------------------------------------------------------------- #


def test_the_stated_role_always_leads():
    """The candidate's own words go to the boards first, in their order."""
    expanded = role_expansion.expand_roles(["AI Engineer"], use_llm=False)
    assert expanded[0] == "AI Engineer"
    assert len(expanded) > 1


def test_expansion_finds_the_names_boards_actually_use():
    expanded = [r.lower() for r in role_expansion.expand_roles(["AI Engineer"], use_llm=False)]
    assert "machine learning engineer" in expanded


def test_seniority_does_not_change_what_a_job_is_called():
    assert role_expansion.normalise("Senior AI Engineer") == "ai engineer"
    assert role_expansion.normalise("Sr. Backend Engineer") == "backend engineer"
    senior = role_expansion.expand_roles(["Senior AI Engineer"], use_llm=False)
    assert senior[0] == "Senior AI Engineer"
    assert any("machine learning" in r.lower() for r in senior)


def test_an_unknown_role_is_returned_unchanged_without_a_model():
    """Discovery must not depend on a live model call.

    Every provider rate-limited for days in production; a design whose *search*
    step needs a model inherits that outage as a silent no-op.
    """
    assert role_expansion.expand_roles(["Underwater Basket Weaver"], use_llm=False) == [
        "Underwater Basket Weaver"
    ]


def test_a_dead_model_leaves_the_stated_roles_intact(monkeypatch):
    from app.services.openrouter_client import OpenRouterError

    monkeypatch.setattr(
        role_expansion,
        "chat_completion_detailed",
        lambda *a, **k: (_ for _ in ()).throw(OpenRouterError("429")),
    )
    assert role_expansion.expand_roles(["Underwater Basket Weaver"]) == [
        "Underwater Basket Weaver"
    ]


def test_expansion_never_repeats_a_role():
    expanded = role_expansion.expand_roles(
        ["AI Engineer", "Machine Learning Engineer"], use_llm=False
    )
    assert len(expanded) == len({role_expansion.normalise(r) for r in expanded})


def test_expansion_is_bounded():
    many = [f"AI Engineer {i}" for i in range(30)]
    assert len(role_expansion.expand_roles(many, use_llm=False)) <= role_expansion.MAX_TOTAL


def test_a_model_echoing_the_input_is_dropped(monkeypatch):
    class _Completion:
        text = '{"titles": ["Underwater Basket Weaver", "Basket Weaving Specialist"]}'
        provider = "test"
        model = "test"

    monkeypatch.setattr(
        role_expansion, "chat_completion_detailed", lambda *a, **k: _Completion()
    )
    expanded = role_expansion.expand_roles(["Underwater Basket Weaver"])
    assert expanded == ["Underwater Basket Weaver", "Basket Weaving Specialist"]


# --------------------------------------------------------------------------- #
# Screening out is recorded, so a run considers something new                  #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def posting(db_session, current_user):
    from app.models.job import JobPosting, JobStatus

    row = JobPosting(
        user_id=current_user.id,
        title="Caretaker",
        company="Discovery Parks",
        url="https://example.com/caretaker",
        fingerprint="fp-caretaker",
        status=JobStatus.NEW,
        fit_score=80.0,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_a_screened_out_posting_is_not_reconsidered(db_session, current_user, posting):
    """The whole reason `scanned: 42` never moved."""
    from app.models.autopilot import AutopilotPreference
    from app.services.auto_apply_service import candidate_postings

    pref = AutopilotPreference(user_id=current_user.id, min_fit_score=50)
    db_session.add(pref)
    db_session.commit()

    assert posting.id in [p.id for p in candidate_postings(db_session, current_user, pref)]

    posting.screened_out_at = datetime.now(UTC)
    posting.screened_out_reason = "'Caretaker' is not one of your target roles"
    db_session.commit()

    assert posting.id not in [
        p.id for p in candidate_postings(db_session, current_user, pref)
    ]


def test_clearing_the_screen_puts_a_posting_back_in_play(
    db_session, current_user, posting
):
    """Changed criteria reopen a posting; that is why this isn't a JobStatus."""
    from app.models.autopilot import AutopilotPreference
    from app.services.auto_apply_service import candidate_postings

    pref = AutopilotPreference(user_id=current_user.id, min_fit_score=50)
    db_session.add(pref)
    posting.screened_out_at = datetime.now(UTC)
    posting.screened_out_reason = "not one of your target roles"
    db_session.commit()

    posting.screened_out_at = None
    posting.screened_out_reason = None
    db_session.commit()

    assert posting.id in [p.id for p in candidate_postings(db_session, current_user, pref)]


# --------------------------------------------------------------------------- #
# The run report                                                               #
# --------------------------------------------------------------------------- #


def test_the_run_summarises_instead_of_listing_forty_two_rejections():
    from app.services.auto_apply_service import AutoApplyResult

    result = AutoApplyResult(
        scanned=42,
        skipped_irrelevant=38,
        skipped_no_contact=4,
        notes=[f"Company {i}: not one of your target roles" for i in range(42)],
    )
    summary = result.summary
    assert "38" in summary and "target roles" in summary
    assert "4" in summary
    # The examples are examples, not the report.
    assert len(result.as_dict()["notes"]) <= 5


def test_a_run_that_applied_says_what_it_applied_as():
    from app.services.auto_apply_service import AutoApplyResult

    result = AutoApplyResult(scanned=10, applied=2, applied_by_profile={"AI Engineer": 2})
    assert "applied to 2" in result.summary
    assert "AI Engineer" in result.summary


def test_an_empty_feed_says_so():
    from app.services.auto_apply_service import AutoApplyResult

    assert "no new postings" in AutoApplyResult().summary
