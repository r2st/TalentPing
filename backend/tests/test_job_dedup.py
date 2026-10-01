"""Cross-board dedup: normalization, the fuzzy match, and which copy survives.

The bias under test throughout is asymmetric on purpose. A missed duplicate
costs the candidate a repeated line in the feed; a wrong merge *hides a job they
never see*. So the "does not merge" cases matter more than the "does" ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.services import job_dedup
from app.services.job_dedup import (
    absorb,
    find_duplicate_of,
    group_duplicates,
    is_duplicate,
    merge_source_urls,
    metadata_richness,
    normalize_company,
    normalize_location,
    normalize_title,
    title_similarity,
)


@dataclass
class Job:
    """The subset of a posting dedup reads — RawJob and JobPosting both fit."""

    title: str | None = "Senior Software Engineer"
    company: str | None = "Northwind Labs"
    location: str | None = "San Francisco, CA"
    url: str | None = "https://example.com/1"
    description: str | None = "Build payment APIs in Python."
    salary_text: str | None = None
    remote: bool | None = None
    source: str | None = "remoteok"
    posted_at: datetime | None = None
    source_urls: list[dict[str, str]] = field(default_factory=list)


class TestNormalizeCompany:
    @pytest.mark.parametrize(
        "name",
        ["Acme, Inc.", "ACME Inc", "Acme  LLC", "Acme GmbH", "acme ltd."],
    )
    def test_legal_suffixes_are_one_company(self, name):
        assert normalize_company(name) == "acme"

    def test_a_suffix_word_in_the_middle_is_kept(self):
        """"Group" names this company; it isn't a legal form here."""
        assert normalize_company("Group Nine Media") == "group nine media"

    def test_different_companies_stay_different(self):
        assert normalize_company("Acme") != normalize_company("Acme Labs")

    def test_empty_input_is_empty(self):
        assert normalize_company(None) == ""


class TestNormalizeTitle:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Sr. Software Engineer", "Senior Software Engineer"),
            ("Jr Backend Dev", "Junior Backend Developer"),
            ("Staff SWE", "Staff Software Engineer"),
            ("Engineering Mgr", "Engineering Manager"),
            ("Software Engineer II", "Software Engineer 2"),
        ],
    )
    def test_abbreviations_normalize_together(self, left, right):
        assert normalize_title(left) == normalize_title(right)

    def test_board_furniture_is_dropped(self):
        assert normalize_title("Backend Engineer (Remote) — Full-Time") == normalize_title(
            "Backend Engineer"
        )

    def test_gender_tags_are_dropped(self):
        assert normalize_title("Backend Engineer (m/w/d)") == normalize_title("Backend Engineer")

    def test_the_location_is_stripped_only_when_the_posting_confirms_it(self):
        """"Berlin" in a title is noise here and signal in "Berlin Operations Lead"."""
        assert normalize_title("Backend Engineer - Berlin", "Berlin, Germany") == (
            normalize_title("Backend Engineer")
        )
        assert "berlin" in normalize_title("Berlin Operations Lead", "San Francisco, CA")

    def test_seniority_survives(self):
        """Levels are the whole difference between two real, separate reqs."""
        assert normalize_title("Senior Engineer") != normalize_title("Junior Engineer")


class TestTitleSimilarity:
    def test_identical_titles_score_one(self):
        assert title_similarity("backend engineer", "backend engineer") == 1.0

    def test_reordered_titles_still_match(self):
        """"Engineer, Backend" and "Backend Engineer" are one job."""
        assert title_similarity("engineer backend", "backend engineer") >= 0.86

    def test_different_disciplines_do_not_match(self):
        assert title_similarity("backend engineer", "frontend engineer") < 0.86

    def test_empty_scores_zero(self):
        assert title_similarity("", "backend engineer") == 0.0


class TestIsDuplicate:
    def test_the_same_role_on_two_boards(self):
        assert is_duplicate(
            Job(title="Sr. Software Engineer", source="serpapi"),
            Job(title="Senior Software Engineer", url="https://other/2", source="remoteok"),
        )

    def test_a_different_company_is_never_a_duplicate(self):
        assert not is_duplicate(Job(company="Northwind Labs"), Job(company="Globex"))

    def test_a_different_discipline_is_not_a_duplicate(self):
        assert not is_duplicate(
            Job(title="Senior Backend Engineer"), Job(title="Senior Frontend Engineer")
        )

    def test_a_different_level_is_not_a_duplicate(self):
        assert not is_duplicate(
            Job(title="Junior Software Engineer"), Job(title="Principal Software Engineer")
        )

    def test_without_a_company_there_is_no_anchor(self):
        """Half the internet is hiring a "Senior Software Engineer"."""
        assert not is_duplicate(Job(company=None), Job(company=None))

    def test_two_cities_are_two_reqs(self):
        assert not is_duplicate(
            Job(location="Berlin, Germany", remote=False),
            Job(location="Tokyo, Japan", remote=False),
        )

    def test_remote_is_compatible_with_a_city(self):
        assert is_duplicate(
            Job(location="Remote", remote=True), Job(location="San Francisco, CA")
        )

    def test_a_missing_location_is_compatible(self):
        assert is_duplicate(Job(location=None), Job(location="Berlin, Germany"))


class TestMetadataRichness:
    def test_the_longer_description_wins(self):
        assert metadata_richness(Job(description="x" * 4000)) > metadata_richness(
            Job(description="short")
        )

    def test_a_published_salary_counts(self):
        assert metadata_richness(Job(salary_text="$150k - $190k")) > metadata_richness(Job())


class TestGrouping:
    def test_three_boards_become_one_group(self):
        groups = group_duplicates(
            [
                Job(title="Sr. Software Engineer", source="serpapi", url="https://a/1"),
                Job(title="Senior Software Engineer (Remote)", source="remoteok", url="https://b/2"),
                Job(title="Senior Software Engineer - Berlin", source="arbeitnow", url="https://c/3"),
            ]
        )
        assert len(groups) == 1
        assert len(groups[0].duplicates) == 2

    def test_unrelated_roles_stay_apart(self):
        groups = group_duplicates(
            [
                Job(title="Backend Engineer", company="Acme"),
                Job(title="Frontend Engineer", company="Acme"),
                Job(title="Backend Engineer", company="Globex"),
            ]
        )
        assert len(groups) == 3

    def test_the_richest_copy_becomes_canonical(self):
        thin = Job(title="Senior Software Engineer", description="Apply now.", source="jobicy")
        rich = Job(
            title="Sr. Software Engineer",
            description="x" * 3000,
            salary_text="$180k",
            source="serpapi",
            url="https://rich/1",
        )
        groups = group_duplicates([thin, rich])
        assert groups[0].canonical is rich
        assert groups[0].duplicates == [thin]

    def test_a_posting_with_no_company_stands_alone(self):
        groups = group_duplicates([Job(company=None), Job(company=None)])
        assert len(groups) == 2

    def test_ordering_is_stable(self):
        jobs = [Job(title="Backend Engineer", company=f"C{i}") for i in range(5)]
        first = [g.canonical.company for g in group_duplicates(jobs)]
        assert first == [g.canonical.company for g in group_duplicates(jobs)]


class TestAbsorb:
    def test_gaps_are_filled_from_the_copy(self):
        canonical = Job(salary_text=None, description="x" * 2000)
        copy = Job(salary_text="$150k - $190k", url="https://other/2", source="jobicy")
        absorb(canonical, copy)
        assert canonical.salary_text == "$150k - $190k"

    def test_existing_values_are_never_overwritten(self):
        canonical = Job(description="the good one")
        absorb(canonical, Job(description="a worse one", url="https://other/2"))
        assert canonical.description == "the good one"

    def test_every_board_is_linked(self):
        canonical = Job(url="https://a/1", source="serpapi")
        absorb(canonical, Job(url="https://b/2", source="remoteok"))
        assert [entry["source"] for entry in canonical.source_urls] == ["serpapi", "remoteok"]

    def test_merging_is_idempotent(self):
        canonical = Job(url="https://a/1")
        copy = Job(url="https://b/2", source="remoteok")
        absorb(canonical, copy)
        absorb(canonical, copy)
        assert len(canonical.source_urls) == 2


class TestMergeSourceUrls:
    def test_prior_links_are_kept_first(self):
        merged = merge_source_urls(
            [{"source": "serpapi", "url": "https://a/1"}], Job(url="https://b/2", source="remoteok")
        )
        assert [e["url"] for e in merged] == ["https://a/1", "https://b/2"]

    def test_a_posting_with_no_url_contributes_nothing(self):
        assert merge_source_urls(None, Job(url=None)) == []


class TestFindDuplicateOf:
    def test_finds_the_stored_copy(self):
        stored = [Job(title="Senior Software Engineer"), Job(title="Data Analyst", company="Globex")]
        found = find_duplicate_of(Job(title="Sr. Software Engineer", url="https://new/1"), stored)
        assert found is stored[0]

    def test_returns_none_when_nothing_matches(self):
        assert find_duplicate_of(Job(company="Umbrella"), [Job()]) is None

    def test_a_nameless_company_never_matches(self):
        assert find_duplicate_of(Job(company=None), [Job(company=None)]) is None


class TestThreshold:
    def test_the_setting_moves_the_line(self, monkeypatch):
        left, right = Job(title="Senior Software Engineer"), Job(title="Software Engineer")
        monkeypatch.setattr(job_dedup.settings, "dedup_title_threshold", 0.99, raising=False)
        assert not is_duplicate(left, right)
        monkeypatch.setattr(job_dedup.settings, "dedup_title_threshold", 0.4, raising=False)
        assert is_duplicate(left, right)


class TestNormalizeLocation:
    @pytest.mark.parametrize(
        "value", ["Remote", "Anywhere", "Worldwide", "Remote (US)", "Distributed"]
    )
    def test_every_flavour_of_remote_is_one_word(self, value):
        assert normalize_location(value) == "remote"

    def test_a_city_keeps_its_words(self):
        assert normalize_location("Berlin, Germany") == "berlin germany"


def test_posted_at_counts_toward_richness():
    assert metadata_richness(Job(posted_at=datetime.now(UTC))) > metadata_richness(Job())
