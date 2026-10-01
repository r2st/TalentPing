"""Orchestration for Smart Apply — resolve a job, score it, tailor against it.

The routers, the job-monitoring beat task and the tests all need the same three
moves: turn a request into a :class:`~app.services.jd_parser.ParsedJob`, persist
a fit score without duplicating rows, and persist a tailoring run. Keeping them
here means the Celery path and the request path can't drift apart.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.fit_score import FitScore
from app.models.job import JobPosting
from app.models.resume import Resume
from app.models.tailored_resume import TailoredResume
from app.models.user import User
from app.services import resume_bullets, resume_pdf
from app.services.fit_scorer import NO_TARGETING, WEIGHTS, FitResult, Targeting, score_fit
from app.services.jd_parser import JobFetchError, ParsedJob, jd_hash, parse_job_input
from app.services.resume_tailor import TailoredOutput, tailor_resume

logger = logging.getLogger(__name__)


class SmartApplyError(RuntimeError):
    """Raised when a Smart Apply request can't be satisfied as asked."""


def resolve_job(
    db: Session,
    user: User,
    *,
    job_description: str | None = None,
    job_url: str | None = None,
    job_posting_id: int | None = None,
    use_llm: bool = True,
) -> tuple[ParsedJob, JobPosting | None]:
    """Turn a request's job reference into a parsed posting.

    Returns ``(parsed_job, posting)`` — ``posting`` is set only when the caller
    pointed at a stored row, so the caller can link results back to the feed.
    """
    posting: JobPosting | None = None
    if job_posting_id is not None:
        posting = db.get(JobPosting, job_posting_id)
        if posting is None or posting.user_id != user.id:
            raise SmartApplyError("Job posting not found")
        job_description = job_description or posting.description
        job_url = job_url or posting.url

    try:
        parsed = parse_job_input(
            description=job_description, url=job_url, use_llm=use_llm
        )
    except JobFetchError as exc:
        raise SmartApplyError(str(exc)) from exc

    # A stored posting knows its own title/company more reliably than a parse of
    # the body text does — the feed got them from the source's structured fields.
    if posting is not None:
        parsed.title = posting.title or parsed.title
        parsed.company = posting.company or parsed.company
        parsed.location = posting.location or parsed.location
        if posting.remote is not None:
            parsed.remote = posting.remote
        # The band, carried as the whole triple. The row's figures were parsed
        # at ingest out of a ``salary_text`` that is very often the provider's
        # structured field rather than anything in ``description``, so a
        # re-parse of the description alone comes back with no figures at all.
        # Those figures are what ``fit_scorer.score_salary`` reads, and this
        # module writes its answer straight back over ``JobPosting.fit_score``
        # — so opening a posting moved the feed's own number, downwards, on a
        # band the card had been showing the candidate the whole time.
        #
        # Only when the parse found nothing: a band written in the body text is
        # the posting's own words about itself and stays authoritative.
        if (
            parsed.salary_min is None
            and parsed.salary_max is None
            and (posting.salary_min is not None or posting.salary_max is not None)
        ):
            parsed.salary_text = posting.salary_text or parsed.salary_text
            parsed.salary_min = posting.salary_min
            parsed.salary_max = posting.salary_max

    return parsed, posting


def resolve_resume(db: Session, user: User, resume_id: int | None) -> Resume | None:
    """The resume a request runs off: the named one, else the default, else newest."""
    if resume_id is not None:
        resume = db.get(Resume, resume_id)
        if resume is not None and resume.user_id == user.id:
            return resume
        raise SmartApplyError("Resume not found")
    return db.scalar(
        select(Resume)
        .where(Resume.user_id == user.id)
        .order_by(Resume.is_default.desc(), Resume.id.desc())
    )


def save_fit_score(
    db: Session,
    user: User,
    resume: Resume,
    job: ParsedJob,
    result: FitResult,
    posting: JobPosting | None = None,
    *,
    profile_id: int | None = None,
) -> FitScore:
    """Upsert the cached score for this (profile, resume, job description) triple.

    The profile is part of the key because the score depends on it: two profiles
    can share a resume and still disagree about a posting, because they want
    different places, pay and titles.

    Read-then-insert against a unique constraint is a race, and here it is a
    race that happens on an ordinary Tuesday rather than an exotic one: the
    nightly job scan writes a score per (posting, profile) for every user, and
    the same user opening the tailor modal on a posting scores the same triple
    on the request thread. Whoever commits second used to take an
    ``IntegrityError`` out of ``db.commit()``.

    That is two different failures depending on which path lost. On the request
    it is a 500 on a score the user can see is already computed. In the scan it
    is worse: this is called in a loop that commits per row, so one collision
    aborted the whole scan — abandoning every posting after it and the
    ``last_run_at``/``jobs_found`` bookkeeping at the end, which is how a scan
    silently stops advancing.

    The loser re-reads and applies its own verdict on top. Safe because both
    writers were answering the same question — the key *is* the question — and
    the later answer is the fresher one. Same idea as
    ``ats_boards.remember_board`` and ``salary_service``.

    Unlike those two, the insert goes inside a **savepoint**, because this one
    is called mid-transaction. ``ats_boards`` can afford to roll back the whole
    transaction on a collision; here the caller has usually just flushed new
    ``JobPosting`` rows that have not been committed yet, and a full rollback
    would take them with it — trading a crashed scan for a scan that quietly
    discarded the postings it had found. The savepoint undoes the failed insert
    and nothing else.
    """
    key = jd_hash(job.raw_text or f"{job.title}{job.company}")

    def _existing() -> FitScore | None:
        return db.scalar(
            select(FitScore).where(
                FitScore.resume_id == resume.id,
                FitScore.jd_hash == key,
                FitScore.profile_id.is_(None)
                if profile_id is None
                else FitScore.profile_id == profile_id,
            )
        )

    def _apply(row: FitScore) -> None:
        row.job_posting_id = posting.id if posting is not None else row.job_posting_id
        row.job_title = job.title
        row.job_company = job.company
        row.overall = result.overall
        row.skills_score = result.skills.score
        row.role_score = result.role.score
        row.experience_score = result.experience.score
        row.industry_score = result.industry.score
        row.location_score = result.location.score
        row.salary_score = result.salary.score
        row.matched_skills = result.matched_skills
        row.missing_skills = result.missing_skills
        row.notes = result.notes()
        row.recommendation = result.recommendation
        row.summary = result.summary

    row = _existing()
    if row is None:
        row = FitScore(
            user_id=user.id, resume_id=resume.id, jd_hash=key, profile_id=profile_id
        )
        _apply(row)
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            logger.debug(
                "fit score for jd %s was written by a concurrent scorer", key[:12]
            )
            # The savepoint rollback expunged our row. Re-read: under READ
            # COMMITTED this statement sees the winner's committed insert, which
            # the earlier read in this transaction could not.
            row = _existing()
            if row is None:
                # The constraint fired for a reason we did not predict; re-raise
                # rather than returning a row that is not this score.
                raise
            _apply(row)
    else:
        _apply(row)

    if posting is not None:
        posting.fit_score = result.overall

    db.commit()
    db.refresh(row)
    return row


@dataclass
class ScoringIntent:
    """Which intent a Smart Apply request runs under, and with which document.

    The three travel together on purpose: a score is only reproducible as the
    triple *(resume, targeting, profile)*, and :func:`save_fit_score` keys the
    cached row on two of them. Splitting them across separate lookups is what
    let the resume and the targeting come from different profiles.

    ``resume`` is ``None`` for a user with no profiles — the caller falls back
    to :func:`resolve_resume` — and ``profile_id`` is ``None`` both for that
    user and for the legacy target built from autopilot preferences.
    """

    targeting: Targeting
    profile_id: int | None
    resume: Resume | None = None


def scoring_intent(
    db: Session, user: User, job: ParsedJob, posting: JobPosting | None
) -> ScoringIntent:
    """Which of the candidate's intents this request should be scored under.

    Everywhere else in the product a posting is scored against a *profile* —
    what the candidate said they want — and only falls back to reading the
    resume when they have said nothing (``fit_scorer.score_location`` explains
    why the two are different answers). Smart Apply asked for no targeting at
    all, so it scored every posting as if the candidate had no preferences, and
    then wrote that number over ``JobPosting.fit_score`` — the column the feed
    orders and filters on and the one ``auto_apply_service.candidate_postings``
    gates sending on. Opening the tailor modal could therefore move a job across
    the auto-apply bar in either direction, on a score the user never saw and
    the feed disagreed with.

    A stored posting scores under the profile it was matched to at scan time:
    that is the number already on the card, and re-deriving a different one is
    the disagreement itself. Anything pasted in has never been matched, so it
    gets the same question the feed asks of a new posting — which intent does
    this job best fit — answered by the same function.

    The winning target's *resume* comes back with it, because the intent is not
    only what the candidate wants but the document they argue it with — a
    profile owns both (``profile_service.resume_for``). Scoring the account's
    default resume under some other profile's targeting is a document/intent
    pair that exists nowhere else in the product, and the feed never agrees
    with it.

    Returns an empty intent for a user with no profiles and no autopilot
    preferences, which is what this function did implicitly before.
    """
    # Imported here rather than at module scope: profile_service imports the
    # scorer, and the routers import both, so a top-level import closes a cycle.
    from app.services import profile_service

    empty = ScoringIntent(NO_TARGETING, None)
    targets = profile_service.active_targets(db, user)
    if not targets:
        return empty

    if posting is not None:
        target = profile_service.target_for_posting(db, user, posting, targets)
    else:
        match = profile_service.score_against_targets(targets, job, explain=False)
        target = match.target if match is not None else None

    if target is None:
        return empty
    return ScoringIntent(target.targeting, target.profile_id, target.resume)


def score_and_save(
    db: Session,
    user: User,
    resume: Resume,
    job: ParsedJob,
    posting: JobPosting | None = None,
    *,
    explain: bool = True,
    targeting: Targeting | None = None,
    profile_id: int | None = None,
) -> tuple[FitScore, FitResult]:
    """Score a resume against a posting and persist the verdict.

    *targeting* and *profile_id* travel together — they are the intent the score
    was produced under and the key the cached row is stored against. Pass them
    from :func:`scoring_intent` rather than separately; a score saved under the
    wrong profile is worse than one saved under none, because the feed will read
    it back as its own.
    """
    result = score_fit(resume, job, targeting=targeting, explain=explain)
    row = save_fit_score(db, user, resume, job, result, posting, profile_id=profile_id)
    return row, result


def save_tailored(
    db: Session,
    user: User,
    resume: Resume,
    job: ParsedJob,
    output: TailoredOutput,
    posting: JobPosting | None = None,
    *,
    job_url: str | None = None,
) -> TailoredResume:
    """Persist one tailoring run. Always a new row — history is the point."""
    row = TailoredResume(
        user_id=user.id,
        resume_id=resume.id,
        job_posting_id=posting.id if posting is not None else None,
        job_title=job.title,
        job_company=job.company,
        job_url=job_url or (posting.url if posting is not None else None),
        job_description=(job.raw_text or "")[:20000] or None,
        tailored_summary=output.tailored_summary,
        ordered_skills=output.ordered_skills,
        highlighted_experience=output.highlighted_experience,
        matched_keywords=output.matched_keywords,
        missing_keywords=output.missing_keywords,
        cover_letter=output.cover_letter,
        generated_with=output.generated_with,
        model=output.model,
        bullets_generated_with=output.bullets_generated_with,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _attach_pdf(db, resume, row)
    return row


def _attach_pdf(db: Session, resume: Resume, row: TailoredResume) -> None:
    """Render and store the PDF, best-effort.

    Stored rather than rendered per download so what the candidate sends is
    byte-for-byte what they reviewed. A rendering failure is never allowed to
    fail the tailoring request — the Markdown download still works, which is
    what the product did before PDFs existed.
    """
    if not resume_pdf.is_available():
        return
    try:
        row.pdf_bytes = resume_pdf.render_pdf(resume, row)
        row.pdf_filename = resume_pdf.filename_for(resume, row)
        row.pdf_generated_at = datetime.now(UTC)
        db.commit()
    except Exception:  # noqa: BLE001 - a PDF is a nice-to-have, not the request
        logger.warning("tailored resume PDF render failed", exc_info=True)
        db.rollback()


def tailor_and_save(
    db: Session,
    user: User,
    resume: Resume,
    job: ParsedJob,
    posting: JobPosting | None = None,
    *,
    job_url: str | None = None,
) -> tuple[TailoredResume, TailoredOutput]:
    """Tailor the resume to the posting and persist the result."""
    output = tailor_resume(resume, job)
    # Re-angle the bullets under each highlighted role. Done here rather than
    # inside tailor_resume so the pure tailorer stays one LLM call: this is a
    # second, independently-rejectable pass, and a rejected bullet rewrite must
    # not cost the summary that was already accepted.
    output.highlighted_experience, output.bullets_generated_with = (
        resume_bullets.tailor_experience_bullets(
            resume, job, output.highlighted_experience, output.missing_keywords
        )
    )
    row = save_tailored(db, user, resume, job, output, posting, job_url=job_url)
    return row, output


def breakdown_payload(result: FitResult) -> dict[str, dict[str, float | str]]:
    """The per-dimension view the API returns: 0-100 per dimension plus weight."""
    dimensions = {
        "skills": result.skills,
        "role": result.role,
        "experience": result.experience,
        "location": result.location,
        "salary": result.salary,
        "industry": result.industry,
    }
    return {
        name: {
            "score": round(dim.score * 100, 1),
            "weight": WEIGHTS[name],
            "note": dim.note,
        }
        for name, dim in dimensions.items()
    }
