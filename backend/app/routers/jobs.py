"""Job feed + saved searches — the discovery half of Smart Apply.

    GET    /jobs                      -> the feed, best fit first
    GET    /jobs/{id}                 -> one posting with its full description
    GET    /jobs/{id}/intel           -> salary benchmark + company research
    PATCH  /jobs/{id}                 -> triage (save / dismiss / applied)
    DELETE /jobs/{id}
    GET    /jobs/searches             -> saved searches
    POST   /jobs/searches             -> create one and run it immediately
    PATCH  /jobs/searches/{id}
    DELETE /jobs/searches/{id}
    POST   /jobs/searches/{id}/run    -> re-run now
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, defer

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import MAX_DB_INT, Page, page_params, paginate
from app.core.patching import reject_nulls
from app.core.rate_limit import rate_limit
from app.core.sql_text import SEARCH_MAX_LENGTH, search_clause
from app.models.job import JobPosting, JobSearch, JobStatus
from app.models.user import User
from app.schemas.smart_apply import (
    BulkJobAction,
    BulkJobRequest,
    BulkJobResult,
    CompanyProfileOut,
    JobIntelOut,
    JobPostingDetail,
    JobPostingOut,
    JobPostingPatch,
    JobSearchCreate,
    JobSearchOut,
    JobSearchPatch,
    JobSearchRunResult,
    SalaryInsightOut,
)
from app.services import (
    career_apply_service,
    company_research,
    salary_service,
    usage_events,
)
from app.services.career_apply_service import ApplicantProfile
from app.services.job_search_service import run_search
from app.services.smart_apply_service import resolve_resume

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])


def _get_search(db: Session, user: User, search_id: int) -> JobSearch:
    search = db.get(JobSearch, search_id)
    if search is None or search.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found"
        )
    return search


def _get_posting(db: Session, user: User, job_id: int) -> JobPosting:
    posting = db.get(JobPosting, job_id)
    if posting is None or posting.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found"
        )
    return posting


# --------------------------------------------------------------------------- #
# Saved searches                                                               #
# --------------------------------------------------------------------------- #
# Declared before /jobs/{job_id} so "searches" is never captured as an id.


@router.get("/searches", response_model=list[JobSearchOut])
def list_searches(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    page: Page = Depends(page_params),
) -> list[JobSearch]:
    """Newest first. Bounded like every list here; see `app.core.pagination`."""
    stmt = (
        select(JobSearch)
        .where(JobSearch.user_id == user.id)
        .order_by(JobSearch.id.desc())
    )
    return paginate(db, stmt, page, response)


@router.post(
    "/searches",
    response_model=JobSearchRunResult,
    status_code=status.HTTP_201_CREATED,
    # Creating a search runs it, so this costs a discovery sweep like
    # `/searches/{id}/run` does. Same budget, deliberately: otherwise the cheaper
    # limit is bypassed by creating a throwaway search instead of re-running one.
    dependencies=[Depends(rate_limit(10, 300, scope="job-search-run"))],
)
def create_search(
    payload: JobSearchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobSearchRunResult:
    """Save a search and run it once immediately, so the feed is never empty."""
    search = JobSearch(
        user_id=user.id,
        resume_id=payload.resume_id,
        name=payload.name or " · ".join([*payload.roles, *payload.keywords][:3])[:255],
        roles=payload.roles,
        keywords=payload.keywords,
        location=payload.location,
        remote_only=payload.remote_only,
        min_fit_score=payload.min_fit_score,
        min_salary=payload.min_salary,
        max_ghost_risk=payload.max_ghost_risk,
        interval_hours=payload.interval_hours,
    )
    db.add(search)
    db.flush()
    usage_events.record(
        db,
        "search.created",
        user_id=user.id,
        search_id=search.id,
        roles=len(payload.roles),
        keywords=len(payload.keywords),
        remote_only=payload.remote_only,
        has_location=bool(payload.location),
    )
    db.commit()
    db.refresh(search)
    return _run(db, search)


@router.patch("/searches/{search_id}", response_model=JobSearchOut)
def patch_search(
    search_id: int,
    payload: JobSearchPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobSearch:
    search = _get_search(db, user, search_id)
    fields = payload.model_dump(exclude_unset=True)

    # The same invariant `JobSearchCreate._require_criteria` enforces, checked
    # against whatever survives the patch rather than only against what is in
    # it — clearing the roles of a keyword-less search is the same empty search
    # the create path refuses, arrived at one field at a time.
    #
    # A search with neither is not a narrow search, it is no filter at all:
    # `matches_query` returns True for every posting when the term list is
    # empty, so a saved search left in that state stores whatever the providers
    # happen to return, every `interval_hours`, forever. Those rows are the feed
    # `auto_apply_service.candidate_postings` draws from, so an unfiltered scan
    # does not merely add noise — it puts postings the user never searched for
    # in front of the thing that emails people.
    roles = fields.get("roles", search.roles)
    keywords = fields.get("keywords", search.keywords)
    if not roles and not keywords:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Give the search at least one role or keyword",
        )

    # After the criteria check, not before: a patch that nulls *both* term lists
    # is an empty search first and a malformed value second, and "give the search
    # at least one role or keyword" is the sentence that tells the user what to
    # do about it. What is left for this to catch is the rest of the row — an
    # `interval_hours` or an `is_active` sent as null, which no column will hold.
    reject_nulls(JobSearch, fields)

    for name, value in fields.items():
        setattr(search, name, value)
    db.commit()
    db.refresh(search)
    return search


@router.delete("/searches/{search_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_search(
    search_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    db.delete(_get_search(db, user, search_id))
    db.commit()


@router.post(
    "/searches/{search_id}/run",
    response_model=JobSearchRunResult,
    # `_run` calls several third-party discovery APIs on the request thread —
    # the docstring below says so — and the deployment runs one worker. Left
    # unmetered this is the cheapest way to hold that worker down.
    dependencies=[Depends(rate_limit(10, 300, scope="job-search-run"))],
)
def run_now(
    search_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobSearchRunResult:
    """Scan for new jobs right now instead of waiting for the next sweep."""
    search = _get_search(db, user, search_id)
    # Before the scan, not after: `_run` reaches several third-party APIs and
    # can take the whole request timeout, and "somebody pressed Scan now" is
    # true whether or not the boards answered. Recorded here it also survives a
    # discovery failure, which is the case worth being able to count.
    usage_events.record(
        db, "search.ran", user_id=user.id, commit=True, search_id=search.id
    )
    return _run(db, search)


def _run(db: Session, search: JobSearch) -> JobSearchRunResult:
    """Scan inline, or hand it to a worker when one is configured.

    Discovery hits several third-party APIs, so it is worker work. But a user who
    just created a search expects results on the page, and a queued task gives
    them an empty feed — so the create/run endpoints wait for it. The beat task
    is what keeps it off the request path day to day.
    """
    result = run_search(db, search)
    notes = []
    if result.below_threshold:
        notes.append(f"{result.below_threshold} below your {search.min_fit_score}+ fit threshold")
    if result.below_salary:
        notes.append(
            f"{result.below_salary} advertised under your ${search.min_salary:,} floor"
        )
    if result.merged:
        copies = "copy" if result.merged == 1 else "copies"
        notes.append(f"{result.merged} cross-board {copies} merged")
    if result.ghosts:
        # Said out loud for the same reason the other two are: a scan that
        # suppressed most of what it found and reported only "3 added" reads as
        # a product that cannot find jobs.
        notes.append(f"{result.ghosts} screened out as likely ghost postings")
    if result.reposts:
        roles = "role" if result.reposts == 1 else "roles"
        notes.append(f"{result.reposts} {roles} re-advertised since the last scan")
    return JobSearchRunResult(
        search_id=search.id,
        scanned=result.scanned,
        added=result.added,
        below_threshold=result.below_threshold,
        below_salary=result.below_salary,
        duplicates=result.duplicates,
        merged=result.merged,
        reranked=result.reranked,
        ghosts=result.ghosts,
        reposts=result.reposts,
        jobs=[JobPostingOut.model_validate(p) for p in result.postings],
        detail=result.detail or (", ".join(notes) or None),
    )


# --------------------------------------------------------------------------- #
# Feed                                                                         #
# --------------------------------------------------------------------------- #

# The feed's sort, named so it can be asserted on rather than only exercised.
#
# Both halves are load-bearing and neither is visible under SQLite, which is
# what the tests run on. `nullslast` is a no-op there — SQLite already ranks
# NULL below every value, so a descending sort puts unscored postings at the
# back on its own. Postgres does the opposite and would open the feed with
# every posting the scorer has not reached yet. `id` is the tie-break that
# makes a paged window stable: `fit_score` alone is not a total order, and the
# scorer produces round numbers, so ties are the common case rather than the
# edge one. See `test_pagination.TestTheFeed`.
_FEED_ORDER = (JobPosting.fit_score.desc().nullslast(), JobPosting.id.desc())


@router.get("", response_model=list[JobPostingOut])
def list_jobs(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    status_filter: JobStatus | None = Query(default=None, alias="status"),
    search_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    company: str | None = Query(default=None, max_length=SEARCH_MAX_LENGTH),
    min_fit: float | None = Query(default=None, ge=0, le=100),
    min_salary: int | None = Query(
        default=None,
        ge=0,
        le=10_000_000,
        description="Hide postings whose advertised band tops out below this",
    ),
    has_salary: bool | None = Query(
        default=None, description="Only postings that publish a salary band"
    ),
    location: str | None = Query(
        default=None,
        max_length=SEARCH_MAX_LENGTH,
        description="Match the posting's location text",
    ),
    remote: bool | None = Query(default=None, description="Remote roles only"),
    max_ghost_risk: int | None = Query(
        default=None,
        ge=0,
        le=100,
        description="Hide postings whose ghost risk is above this",
    ),
    include_duplicates: bool = Query(
        default=False, description="Also return copies of a role found on other boards"
    ),
    page: Page = Depends(page_params),
) -> list[JobPosting]:
    """The job feed — highest fit first, then newest.

    Bounded like every list here; see `app.core.pagination`. This is the
    largest of them by a wide margin — every saved search writes a row per
    posting it finds, on every run, so the count is a function of how many
    searches are saved and how long they have been running. It took a private
    `limit` that stopped at 500 with no `offset` beside it, which put a hard
    ceiling on what an account could ever see: the 501st posting was not on
    the page and there was no page to move to.

    The sort is a total order — `id` breaks every tie in `fit_score`, and
    nulls sort last — so a row cannot drift between pages while you read them.
    That matters more here than on the newest-first lists: the interesting
    postings are at the top of the *first* page, which is exactly where an
    unstable sort would shuffle them.

    Dismissed postings are hidden unless asked for by name: the whole point of
    triage is that a dismissed job stops taking up attention.

    Cross-board duplicates are hidden on the same argument — the canonical row
    carries every source URL, so showing its copies would only pad the feed with
    the same job. ``include_duplicates=true`` is there for debugging a merge.

    ``min_salary`` keeps postings that advertise nothing, exactly as the saved
    search's floor does — most employers publish no band, and a filter that
    silently dropped them would look like the feed had broken. ``has_salary``
    is the separate switch for "only show me the ones that said".

    ``max_ghost_risk`` keeps postings that were never assessed, on the same
    argument as the salary filter keeping postings that published no band: a
    null there means we have no opinion, not that the posting is a ghost. Rows
    over the *search's* own ceiling never reached the feed in the first place —
    this filter is the reader-side tightening on top of that.

    ``min_fit`` keeps unscored postings for the third time on the same argument,
    and it is the one that used to not. Scoring is asynchronous — a posting is
    written by the search run and scored afterwards — so "no score yet" is a
    state every new row passes through, and it is the state the *newest*
    postings are in. Excluding them made the filter's own zero destructive:
    ``min_fit=0`` reads as "no minimum" and is what the filter box sends when a
    user types the `0` its placeholder shows them, and it hid every posting the
    scorer had not reached. The nulls cost nothing where they land, either —
    `_FEED_ORDER` sorts them last, so they sit under every scored row rather
    than in front of the matches the filter was narrowing towards.

    ``remote=true`` also admits postings whose location merely *says* remote:
    the ``remote`` flag is only as good as the board that set it, and a row
    reading "Remote (US)" with a null flag is not a row worth hiding.
    """
    stmt = select(JobPosting).where(JobPosting.user_id == user.id)
    if not include_duplicates:
        stmt = stmt.where(JobPosting.duplicate_of_id.is_(None))
    if status_filter is not None:
        stmt = stmt.where(JobPosting.status == status_filter)
    else:
        stmt = stmt.where(JobPosting.status != JobStatus.DISMISSED)
    if search_id is not None:
        stmt = stmt.where(JobPosting.search_id == search_id)
    if (clause := search_clause(company, JobPosting.company)) is not None:
        stmt = stmt.where(clause)
    if min_fit is not None:
        stmt = stmt.where(
            or_(
                JobPosting.fit_score.is_(None),
                JobPosting.fit_score >= min_fit,
            )
        )
    if min_salary is not None:
        stmt = stmt.where(
            or_(
                JobPosting.salary_max.is_(None),
                JobPosting.salary_max >= min_salary,
            )
        )
    if has_salary is not None:
        stmt = stmt.where(
            JobPosting.salary_max.is_not(None)
            if has_salary
            else JobPosting.salary_max.is_(None)
        )
    if max_ghost_risk is not None:
        stmt = stmt.where(
            or_(
                JobPosting.ghost_risk.is_(None),
                JobPosting.ghost_risk <= max_ghost_risk,
            )
        )
    if (clause := search_clause(location, JobPosting.location)) is not None:
        stmt = stmt.where(clause)
    if remote is True:
        stmt = stmt.where(
            or_(JobPosting.remote.is_(True), JobPosting.location.ilike("%remote%"))
        )
    elif remote is False:
        # The exact mirror of the branch above, so the two halves of the switch
        # partition the feed: a row admitted by ``remote=true`` must not also be
        # admitted by ``remote=false``.
        stmt = stmt.where(
            JobPosting.remote.is_not(True),
            or_(
                JobPosting.location.is_(None),
                ~JobPosting.location.ilike("%remote%"),
            ),
        )

    # The feed's rows are cards, and a card has no room for a job description.
    # ``JobPostingOut`` does not carry one — ``JobPostingDetail``, which
    # ``GET /jobs/{id}`` returns, is the schema that adds it — but the query was
    # selecting the column anyway and Pydantic was dropping it on the floor. A
    # description is the whole scraped posting, tens of kilobytes of it, and this
    # list runs to 500 rows: several megabytes read out of the database, over the
    # wire and into the API process to render titles and companies.
    stmt = stmt.order_by(*_FEED_ORDER).options(defer(JobPosting.description))
    rows = paginate(db, stmt, page, response)

    # Which knobs on the filter row are worth the code that maintains them is
    # not answerable from anything else this application stores, and the feed is
    # the screen with the most of them. `filters_used` keeps the *names* and the
    # result count; the two free-text ones keep their value as well, because
    # "what do people type into the company box" is the question a search
    # feature is designed against and there is no way to ask it from a list of
    # booleans. `commit=True` because this handler has no commit of its own —
    # see `usage_events.record` for why that cannot expire `rows`.
    usage_events.record(
        db,
        "job.searched",
        user_id=user.id,
        commit=True,
        results=len(rows),
        page=page.offset,
        filters=usage_events.filters_used(
            {
                "status": status_filter,
                "search_id": search_id,
                "company": company,
                "min_fit": min_fit,
                "min_salary": min_salary,
                "has_salary": has_salary,
                "location": location,
                "remote": remote,
                "max_ghost_risk": max_ghost_risk,
            }
        ),
        company=company,
        location=location,
    )
    return rows


# Declared before /{job_id} so "bulk" is never captured as a posting id.
_BULK_STATUS: dict[BulkJobAction, JobStatus] = {
    BulkJobAction.SAVE: JobStatus.SAVED,
    BulkJobAction.APPLY: JobStatus.APPLIED,
    BulkJobAction.DISMISS: JobStatus.DISMISSED,
}

# Which usage event each triage outcome is. Keyed on the resulting status rather
# than on the action, so the single-posting PATCH — which has no `BulkJobAction`
# — and the bulk endpoint read the same table and cannot drift into counting the
# same decision under two names. ARCHIVE has no status to land on and is
# recorded by the delete branch itself.
_TRIAGE_EVENT: dict[JobStatus, str] = {
    JobStatus.SAVED: "job.saved",
    JobStatus.APPLIED: "job.applied",
    JobStatus.DISMISSED: "job.dismissed",
}


@router.post("/bulk", response_model=BulkJobResult)
def bulk_triage(
    payload: BulkJobRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BulkJobResult:
    """Triage a whole selection at once — save, apply, dismiss or archive.

    Triage is the job the feed exists for, and doing it one row at a time is
    where a 40-result scan stops being worth opening. Ids that aren't this
    user's come back in ``not_found`` rather than 404-ing the whole call: a
    stale tab shouldn't cost the user the other thirty-nine.
    """
    postings = list(
        db.scalars(
            select(JobPosting).where(
                JobPosting.id.in_(payload.job_ids), JobPosting.user_id == user.id
            )
        )
    )
    found = {p.id for p in postings}
    not_found = [job_id for job_id in payload.job_ids if job_id not in found]

    if payload.action is BulkJobAction.ARCHIVE:
        for posting in postings:
            db.delete(posting)
        if postings:
            usage_events.record(
                db, "job.archived", user_id=user.id, count=len(postings), bulk=True
            )
        db.commit()
        return BulkJobResult(
            action=payload.action, deleted=len(postings), not_found=not_found
        )

    target = _BULK_STATUS[payload.action]
    updated = skipped = 0
    for posting in postings:
        if posting.status == target:
            skipped += 1
            continue
        posting.status = target
        if target is JobStatus.APPLIED and posting.applied_at is None:
            posting.applied_at = datetime.now(UTC)
        updated += 1
    # One row for the gesture, not one per posting. Bulk triage is a single act
    # — that is the whole reason the endpoint exists — and counting it per id
    # would make one drag-select outrank a week of considered decisions in every
    # ranking the report draws.
    if updated and (name := _TRIAGE_EVENT.get(target)) is not None:
        usage_events.record(db, name, user_id=user.id, count=updated, bulk=True)
    db.commit()
    return BulkJobResult(
        action=payload.action, updated=updated, skipped=skipped, not_found=not_found
    )


@router.get("/{job_id}", response_model=JobPostingDetail)
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobPosting:
    """One posting, with its description.

    Opening a card is the single clearest signal the feed produces about whether
    the ranking above it is any good — a feed nobody opens is a feed nobody
    trusts — and it left no trace anywhere until this line.
    """
    posting = _get_posting(db, user, job_id)
    usage_events.record(
        db,
        "job.viewed",
        user_id=user.id,
        commit=True,
        job_id=posting.id,
        source=posting.source,
        # The rank the user was shown, not a judgement of the posting: it is
        # what says whether people open what the scorer put at the top.
        fit_score=posting.fit_score,
    )
    return posting


@router.get(
    "/{job_id}/intel",
    response_model=JobIntelOut,
    # Company research is an LLM call on a cache miss, and `?refresh=true` is a
    # miss by definition. Generous, because the panel opens as the user browses
    # and most opens are cache hits — this is a ceiling on the misses, not a
    # budget for reading.
    dependencies=[Depends(rate_limit(30, 60, scope="job-intel"))],
)
def job_intel(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    refresh: bool = Query(default=False, description="Re-research the company now"),
) -> JobIntelOut:
    """Market and company context for one posting — the expandable panel's data.

    Both halves are cached globally (a salary band per role/level/market, a
    company profile per employer), so the first candidate to open a card pays
    for the research and everyone after them gets it for free. That is why this
    is a separate call rather than part of the feed: it costs nothing on a
    hundred-row page nobody expanded.

    Company research degrades rather than fails — a dead LLM provider yields a
    heuristic card read off the posting itself, and the response says which.
    """
    posting = _get_posting(db, user, job_id)

    salary = SalaryInsightOut.model_validate(
        salary_service.insight_for_posting(db, posting).as_dict()
    )

    company: CompanyProfileOut | None = None
    try:
        profile = company_research.research_company(
            db,
            posting.company,
            posting_text=posting.description,
            location=posting.location,
            force=refresh,
        )
        if profile is not None:
            company = CompanyProfileOut.model_validate(profile)
    except Exception:  # noqa: BLE001 - the salary half is still worth returning
        logger.exception("company research failed for %s", posting.company)
        db.rollback()

    return JobIntelOut(
        job_id=posting.id,
        llm_fit_score=posting.llm_fit_score,
        llm_reasoning=posting.llm_reasoning,
        salary=salary,
        company=company,
        also_on=list(posting.source_urls or []),
    )


@router.patch("/{job_id}", response_model=JobPostingOut)
def patch_job(
    job_id: int,
    payload: JobPostingPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobPosting:
    """Move a posting through triage: saved, applied, or dismissed."""
    posting = _get_posting(db, user, job_id)
    posting.status = payload.status
    if (name := _TRIAGE_EVENT.get(payload.status)) is not None:
        usage_events.record(db, name, user_id=user.id, job_id=posting.id, bulk=False)
    db.commit()
    db.refresh(posting)
    return posting


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    db.delete(_get_posting(db, user, job_id))
    db.commit()


@router.post(
    "/{job_id}/apply-form",
    response_model=dict,
    # The third door into a browser run, and the bluntest: this one predates
    # the form-apply router, calls `career_apply_service` instead of it, and
    # never dispatches to a worker — `autofill_application` launches Chromium
    # on the request thread and holds it for up to its 20s page timeout. Same
    # `form-apply-run` budget as the other two doors, because they are the same
    # work against the same posting's application URL; a scope of its own would
    # just be the one a caller alternates into for double the budget.
    dependencies=[Depends(rate_limit(10, 300, scope="form-apply-run"))],
)
def apply_via_form(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    submit: bool = Query(default=False, description="Submit the form, not just fill it"),
) -> dict:
    """Best-effort auto-fill of a posting's own application form (browser agent).

    Fills the common fields from the candidate's resume; only submits when
    ``submit=true``. Returns ``unsupported`` when browser automation isn't
    installed on this server, so the UI can offer it conditionally.
    """
    posting = _get_posting(db, user, job_id)
    if not posting.url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This posting has no application URL",
        )
    resume = resolve_resume(db, user, None)
    if resume is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload a resume before auto-applying",
        )

    profile = ApplicantProfile.from_resume(resume)
    result = career_apply_service.autofill_application(posting.url, profile, submit=submit)

    posting.form_apply_status = result.status
    posting.form_apply_note = result.note
    if result.status == "submitted":
        posting.status = JobStatus.APPLIED
        posting.applied_at = datetime.now(UTC)
    db.commit()
    return result.as_dict()


@router.get("/providers/status", response_model=dict)
def provider_status() -> dict:
    """Which discovery sources this deployment can reach.

    Surfaced in the UI so "no jobs found" is distinguishable from "Google Jobs
    isn't configured on this server".
    """
    return {
        "google_jobs_serpapi": bool(settings.serpapi_api_key),
        "public_boards": ["remoteok", "arbeitnow", "jobicy"],
        "form_autofill": career_apply_service.is_available(),
    }
