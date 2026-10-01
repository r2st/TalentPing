"""Re-scoring a feed after the intent behind it changed.

A fit score is a verdict about a posting *and* a profile — the same job scores
differently for a candidate chasing staff-level backend roles in Berlin and one
chasing the same title in Lisbon at half the pay. :mod:`app.services.fit_scorer`
has always taken both, and :class:`~app.models.fit_score.FitScore` has been keyed
on both since profiles existed.

What was missing is the other half of that statement: **when the profile changes,
every score computed under it is a verdict on a question nobody is asking any
more.** Nothing recomputed them. A candidate who widened their locations, raised
their salary floor or renamed the roles they wanted came back to a feed sorted by
the old intent — the postings the edit should have promoted still ranked where
the old profile put them, ``matched_profile_id`` still named whichever profile had
won under the *previous* targeting, and the breakdown panel behind the number
(``autopilot_plan._fit_row``) explained a score against reasoning the user had
just replaced. The number was not merely old; it was answering a superseded
question while presenting itself as current.

Two decisions worth stating.

**Which edits count.** Only the fields the scorer actually reads —
:data:`SCORING_FIELDS`, which is ``Targeting.from_profile`` written as a set.
Renaming a profile or making it the default does not move a score, and rescoring
a whole feed because somebody fixed a typo is the kind of cost that gets a
feature switched off. ``is_default`` is the one near-miss and it *is* included:
it breaks ties in ``score_against_targets``, so it can change which profile wins
a posting even though it changes no dimension. ``is_active`` likewise — switching
a profile off removes a target from the set entirely.

**Every posting against every target, not one profile's postings.** A profile
edit cannot be scoped to the postings that profile already won. The whole point
of an intent that got wider is that postings which lost under it may now win, so
the recompute has to be the same all-targets comparison the scan does
(:func:`profile_service.score_against_targets`) or it can only ever confirm the
existing matching.

Terminal postings are left alone. A dismissed posting is a decision the candidate
already made, and an applied one is history — restating either under new
targeting would edit the record of what they did rather than the list of what
they could do next.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.job import JobPosting, JobStatus
from app.models.profile import Profile
from app.models.user import User
from app.services import profile_service, salary_service
from app.services.jd_parser import parse_job

logger = logging.getLogger(__name__)

#: Profile columns the scorer reads, directly or through a tie-break. Edit one of
#: these and every cached verdict under the profile is stale; edit anything else
#: and none of them are.
#:
#: This is ``fit_scorer.Targeting.from_profile`` restated as a set, plus the two
#: that decide *whether and how* a profile competes rather than how it scores.
#: ``tests/test_fit_refresh.py`` holds it against the real column list, so a
#: column added to ``Profile`` is a failing test rather than a stale feed.
SCORING_FIELDS: frozenset[str] = frozenset(
    {
        # Read by Targeting.from_profile — the dimensions themselves.
        "target_roles",
        "target_industries",
        "skills",
        "location_preferences",
        "remote_only",
        "salary_min",
        "salary_max",
        "experience_level",
        "employment_types",
        "company_sizes",
        "excluded_companies",
        # Not a dimension, but the document every dimension is scored against.
        "resume_id",
        # Not dimensions either: these decide whether this profile is in the
        # comparison at all, and which way a tie goes.
        "is_active",
        "is_default",
    }
)

#: Profile columns that provably do not move a score, listed so the drift test
#: can insist every column is in exactly one of the two sets. A new column left
#: out of both is the bug this pair exists to catch.
UNSCORED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "user_id",
        "name",
        "gmail_account_id",
        "created_at",
        "updated_at",
    }
)

#: The statuses worth re-scoring. See the module docstring: the other two are
#: records of a decision, not a shortlist.
LIVE_STATUSES = (JobStatus.NEW, JobStatus.SAVED, JobStatus.TAILORED)

#: Postings re-scored in one pass. A ceiling rather than a page: this runs to
#: completion or reports what it left, and the number it left is logged — see
#: :func:`rescore_user`. Newest first, because a feed is read from the top.
RESCORE_CEILING = 500


def affects_scoring(edited: set[str] | frozenset[str]) -> bool:
    """Whether this set of edited profile fields can change any score."""
    return bool(SCORING_FIELDS & set(edited))


def _parsed(posting: JobPosting):
    """The posting as the scorer wants it, parsed the way the scan parses it.

    Heuristic only (``use_llm=False``) for the reason ``_score_raw_job`` gives:
    this runs over a whole feed, and a model call per posting would spend a free
    tier on one profile edit. The provider's own structured fields beat anything
    read out of the body, exactly as at scan time — a rescore that disagreed with
    the scan about the posting's own title would be a second bug wearing the
    first one's clothes.
    """
    parsed = parse_job(
        posting.description or f"{posting.title or ''}\n{posting.company or ''}",
        page_title=posting.title,
        use_llm=False,
    )
    parsed.title = posting.title or parsed.title
    parsed.company = posting.company or parsed.company
    parsed.location = posting.location or parsed.location
    if posting.remote is not None:
        parsed.remote = posting.remote
    if posting.salary_text and not parsed.salary_text:
        parsed.salary_text = posting.salary_text
    # The band is three fields and the row stores all three. Prefer the stored
    # figures — they are what the feed filters on — and fall back to parsing the
    # text only when the row has none, which is the same order `_score_raw_job`
    # arrives at from the other direction.
    if posting.salary_min is not None or posting.salary_max is not None:
        parsed.salary_min = posting.salary_min
        parsed.salary_max = posting.salary_max
    elif parsed.salary_min is None and parsed.salary_max is None and parsed.salary_text:
        parsed.salary_min, parsed.salary_max = salary_service.parse_offered(
            parsed.salary_text
        )
    return parsed


def rescore_user(db: Session, user: User, *, limit: int = RESCORE_CEILING) -> dict:
    """Re-score this user's live feed against their current profiles. Commits.

    Returns a summary rather than nothing so the caller — a Celery task, a test,
    or the inline fallback — can say what happened. ``changed`` counts postings
    whose score or matched profile actually moved, which is the only number that
    distinguishes "the edit mattered" from "we did the work for nothing".

    A user with no scorable target (no profiles, or none with a resume) is left
    exactly as they are. Blanking every score because the last resume was deleted
    would turn a recoverable mistake into a feed with no order in it.
    """
    targets = profile_service.active_targets(db, user)
    if not targets:
        logger.debug("rescore skipped for user %s: no scoring targets", user.id)
        return {"scored": 0, "changed": 0, "skipped": 0, "remaining": 0}

    stmt = (
        select(JobPosting)
        .where(
            JobPosting.user_id == user.id,
            JobPosting.status.in_(LIVE_STATUSES),
            # A duplicate is not shown on its own; the canonical row it points at
            # is the one that carries a score.
            JobPosting.duplicate_of_id.is_(None),
        )
        .order_by(JobPosting.id.desc())
    )
    postings = list(db.scalars(stmt.limit(limit)))
    remaining = 0
    if len(postings) == limit:
        # Counted rather than inferred from an over-fetch of one. "There is at
        # least one more" and "there are four hundred more" call for different
        # decisions from whoever reads the log line, and a `limit + 1` read can
        # only ever report the first of those.
        total = int(
            db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        )
        remaining = max(0, total - limit)

    scored = 0
    changed = 0
    skipped = 0
    for posting in postings:
        try:
            match = profile_service.score_against_targets(
                targets, _parsed(posting), explain=False
            )
        except Exception:  # noqa: BLE001 - one unparseable posting must not
            # cost the user the rest of their feed. Counted, not raised.
            logger.exception("rescore failed for posting %s", posting.id)
            skipped += 1
            continue
        if match is None:  # pragma: no cover - targets is non-empty above
            skipped += 1
            continue
        scored += 1
        was = (posting.fit_score, posting.matched_profile_id)
        posting.fit_score = match.fit.overall
        posting.matched_profile_id = match.target.profile_id
        if was != (posting.fit_score, posting.matched_profile_id):
            changed += 1

    db.commit()
    result = {
        "scored": scored,
        "changed": changed,
        "skipped": skipped,
        "remaining": remaining,
    }
    if remaining:
        # Never silently. A ceiling that is not reported reads as "your whole
        # feed was rescored" to everyone downstream, including the next person
        # to debug a score that did not move.
        logger.warning(
            "rescore hit its ceiling for user %s; %s postings left unscored",
            user.id,
            remaining,
            extra={"rescore": result},
        )
    return result


def profile_columns() -> set[str]:
    """Every mapped column on :class:`Profile` — the drift test's left-hand side."""
    return {column.key for column in Profile.__mapper__.columns}


__all__ = [
    "LIVE_STATUSES",
    "RESCORE_CEILING",
    "SCORING_FIELDS",
    "UNSCORED_FIELDS",
    "affects_scoring",
    "profile_columns",
    "rescore_user",
]
