"""Smart Apply routes — tailor a resume to a job, and score the fit.

    POST /tailor                  -> tailored resume + cover letter (+ fit score)
    GET  /tailor                  -> past tailoring runs
    GET  /tailor/{id}             -> one run
    GET  /tailor/{id}/download    -> the tailored resume as Markdown or text
    DEL  /tailor/{id}             -> discard a run
    DEL  /cover-letter/{id}       -> discard a letter
    POST /fit-score               -> 0-100 fit with a per-dimension breakdown
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import Page, page_params, paginate
from app.core.rate_limit import rate_limit
from app.models.application import Application
from app.models.cover_letter import CoverLetter
from app.models.email import Email, EmailDirection
from app.models.email_thread import EmailThread
from app.models.tailored_resume import TailoredResume
from app.models.user import User
from app.schemas.smart_apply import (
    CoverLetterEdit,
    CoverLetterOut,
    CoverLetterRequest,
    FitRequest,
    FitResponse,
    FitScoreOut,
    ParsedJobOut,
    TailoredResumeOut,
    TailorRequest,
    TailorResponse,
)
from app.services import cover_letter_service, usage_events
from app.services import smart_apply_service as svc
from app.services.resume_pdf import filename_stem
from app.services.resume_tailor import render_markdown

logger = logging.getLogger(__name__)
router = APIRouter(
    tags=["smart-apply"],
    dependencies=[Depends(rate_limit(20, 60, scope="smart-apply"))],
)


def _parsed_out(job) -> ParsedJobOut:
    return ParsedJobOut(
        title=job.title,
        company=job.company,
        location=job.location,
        remote=job.remote,
        seniority=job.seniority,
        years_required=job.years_required,
        required_skills=job.required_skills,
        preferred_skills=job.preferred_skills,
        responsibilities=job.responsibilities,
        industry=job.industry,
        salary_text=job.salary_text,
        parsed_with=job.parsed_with,
    )


def _fit_out(row, result) -> FitScoreOut:
    """The stored row plus the parts of the verdict that only the run knows.

    ``capped`` comes off the :class:`~app.services.fit_scorer.FitResult` rather
    than the row because it is not stored — and it has to be said, because
    without it the response contradicts itself. A capped score is held down to
    ``UNEVALUATED_CAP`` *after* the weighting, so the six dimensions in
    ``breakdown`` still read 70-100 and weigh out to about 74 while ``overall``
    says 55. The panel that renders them is headed "Why this score", and the
    reason was the one thing it did not have.
    """
    return FitScoreOut(
        id=row.id,
        resume_id=row.resume_id,
        job_posting_id=row.job_posting_id,
        job_title=row.job_title,
        job_company=row.job_company,
        overall=row.overall,
        recommendation=row.recommendation or "poor",
        summary=row.summary,
        breakdown=svc.breakdown_payload(result),
        capped=result.capped,
        matched_skills=row.matched_skills or [],
        missing_skills=row.missing_skills or [],
        created_at=row.created_at,
    )


def _resolve(db: Session, user: User, payload):
    """Shared prologue: resolve and parse the job, then pick the resume for it.

    The job is resolved first because the resume depends on it. A request that
    names no ``resume_id`` runs off the resume belonging to whichever profile
    this job matched — the same document the feed scored the posting with —
    rather than the account's default. The two are only the same resume for a
    single-profile user; for anyone else the default belongs to an unrelated
    intent, and tailoring, scoring and cover letters all argued from it.

    Naming a ``resume_id`` still wins: that is the user pointing at a document
    on purpose, and the answer they want is about that document.
    """
    try:
        job, posting = svc.resolve_job(
            db,
            user,
            job_description=payload.job_description,
            job_url=payload.job_url,
            job_posting_id=payload.job_posting_id,
        )
        intent = svc.scoring_intent(db, user, job, posting)
        if payload.resume_id is None and intent.resume is not None:
            resume = intent.resume
        else:
            resume = svc.resolve_resume(db, user, payload.resume_id)
        if resume is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Upload a resume before using Smart Apply",
            )
    except svc.SmartApplyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return resume, job, posting, intent


@router.post("/tailor", response_model=TailorResponse, status_code=status.HTTP_201_CREATED)
def tailor(
    payload: TailorRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TailorResponse:
    """Tailor a resume to one job description and draft a matching cover letter.

    The tailoring never invents: skills are reordered, not added, and any
    generated prose that claims something the resume doesn't support is dropped
    in favour of a deterministic version. ``missing_keywords`` is the honest
    counterpart — the requirements the candidate genuinely doesn't meet.
    """
    resume, job, posting, intent = _resolve(db, user, payload)

    # Three events for one call, because the interesting number is the gap
    # between them. A `started` with no matching `completed` is a user who asked
    # for the headline feature of this product and got nothing back, and until
    # now that produced exactly the same trace as a user who never pressed the
    # button: none. Committed on its own so the record of the attempt survives
    # the attempt failing — which is the whole reason it is written first.
    usage_events.record(
        db,
        "smart_apply.started",
        user_id=user.id,
        commit=True,
        with_fit=payload.include_fit_score,
        with_letter=payload.include_cover_letter,
        from_url=bool(payload.job_url),
    )
    try:
        row, _output = svc.tailor_and_save(
            db, user, resume, job, posting, job_url=payload.job_url
        )
    except Exception:
        # Re-raised untouched — the caller still gets the error it would have
        # got. The rollback is what makes the event writable: the failure has
        # very likely left the session unusable, and `record` cannot insert
        # through a poisoned transaction.
        db.rollback()
        usage_events.record(db, "smart_apply.failed", user_id=user.id, commit=True)
        raise

    fit_out = None
    if payload.include_fit_score:
        fit_row, result = svc.score_and_save(
            db,
            user,
            resume,
            job,
            posting,
            targeting=intent.targeting,
            profile_id=intent.profile_id,
        )
        fit_out = _fit_out(fit_row, result)

    letter_out = None
    if payload.include_cover_letter:
        letter = cover_letter_service.upsert_letter(
            db,
            user_id=user.id,
            resume=resume,
            job=job,
            job_posting_id=posting.id if posting is not None else None,
            tailored_resume_id=row.id,
            profile=_company_profile(db, posting),
        )
        db.commit()
        db.refresh(letter)
        letter_out = _letter_out(letter)

    usage_events.record(
        db,
        "smart_apply.completed",
        user_id=user.id,
        commit=True,
        tailored_id=row.id,
        with_fit=fit_out is not None,
        with_letter=letter_out is not None,
    )
    return TailorResponse(
        tailored=TailoredResumeOut.model_validate(row),
        parsed_job=_parsed_out(job),
        fit=fit_out,
        markdown=render_markdown(resume, row),
        cover_letter=letter_out,
    )


@router.get("/tailor", response_model=list[TailoredResumeOut])
def list_tailored(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    page: Page = Depends(page_params),
) -> list[TailoredResume]:
    """Past tailoring runs, newest first.

    Bounded like every list here; see `app.core.pagination`. One row per job
    the pipeline tailored for, so the count is a function of how long autopilot
    has been running rather than of anything the user typed.
    """
    stmt = (
        select(TailoredResume)
        .where(TailoredResume.user_id == user.id)
        .order_by(TailoredResume.id.desc())
    )
    return paginate(db, stmt, page, response)


def _get_owned(db: Session, user: User, tailored_id: int) -> TailoredResume:
    row = db.get(TailoredResume, tailored_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tailored resume not found"
        )
    return row


@router.get("/tailor/{tailored_id}", response_model=TailoredResumeOut)
def get_tailored(
    tailored_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TailoredResume:
    return _get_owned(db, user, tailored_id)


@router.get("/tailor/{tailored_id}/download", response_class=PlainTextResponse)
def download_tailored(
    tailored_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: str = Query(default="resume", pattern="^(resume|cover_letter)$"),
) -> PlainTextResponse:
    """Download the tailored resume (Markdown) or the cover letter (plain text)."""
    row = _get_owned(db, user, tailored_id)
    resume = row.resume

    if doc == "cover_letter":
        body = row.cover_letter or ""
        suffix = "cover-letter.txt"
        media_type = "text/plain; charset=utf-8"
    else:
        body = render_markdown(resume, row)
        suffix = "resume.md"
        media_type = "text/markdown; charset=utf-8"

    # `filename_stem`, not a second `[^A-Za-z0-9]+` strip: an accented letter is
    # not in that class, so a bare strip reads one as punctuation — "Société
    # Générale" came out `soci-t-g-n-rale`, "Łukasz Software" as
    # `ukasz-software`. The same tailoring downloaded as a PDF one route below
    # already folds first and comes out `societe-generale`, so the two
    # downloads of one document disagreed about what it was called. See
    # :func:`app.services.resume_pdf.filename_stem`.
    stem = filename_stem(row.job_company or row.job_title or "job")
    filename = f"{stem or 'job'}-{suffix}"
    return PlainTextResponse(
        body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# Cover letters                                                                #
# --------------------------------------------------------------------------- #


def _letter_out(letter: CoverLetter) -> CoverLetterOut:
    out = CoverLetterOut.model_validate(letter)
    out.full_text = letter.full_text
    return out


def _company_profile(db: Session, posting) -> object | None:
    """Research for the posting's employer, from cache where possible.

    Thin alias for :func:`cover_letter_service.profile_for_posting`, which the
    autopilot pipeline needs too — the grounding facts a letter is allowed to use
    must not differ depending on whether a human or the agent asked for it.
    """
    return cover_letter_service.profile_for_posting(db, posting)


@router.post(
    "/cover-letter", response_model=CoverLetterOut, status_code=status.HTTP_201_CREATED
)
def write_cover_letter(
    payload: CoverLetterRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CoverLetterOut:
    """Write (or rewrite) the letter for one posting.

    Grounded twice over: claims about the candidate must come from their resume,
    and anything said about the employer must come from the cached company
    research, which is returned alongside so the user can see the basis.

    ``tailored_resume_id`` is checked like every other id a caller may name here.
    It was the one that was not: taken from the request and written straight onto
    the row, so a letter belonging to one user could hold a foreign key into
    another user's ``tailored_resumes``. Nothing rendered it — the attachment
    resolver finds the tailored PDF by posting, user-scoped — which is why it
    read as harmless, and is exactly the argument that stops holding the moment
    something else joins on the column. A cross-tenant reference is a bug when it
    is written, not when it is finally dereferenced.
    """
    resume, job, posting, _intent = _resolve(db, user, payload)
    if payload.tailored_resume_id is not None:
        _get_owned(db, user, payload.tailored_resume_id)
    letter = cover_letter_service.upsert_letter(
        db,
        user_id=user.id,
        resume=resume,
        job=job,
        job_posting_id=posting.id if posting is not None else None,
        tailored_resume_id=payload.tailored_resume_id,
        profile=_company_profile(db, posting),
        delivery=payload.delivery,
        force=payload.force,
    )
    usage_events.record(
        db,
        "cover_letter.written",
        user_id=user.id,
        rewrite=payload.force,
        delivery=payload.delivery,
    )
    db.commit()
    db.refresh(letter)
    return _letter_out(letter)


@router.get("/cover-letter", response_model=list[CoverLetterOut])
def list_cover_letters(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    page: Page = Depends(page_params),
) -> list[CoverLetterOut]:
    """Letters written for this user, newest first.

    Bounded like every list here; see `app.core.pagination`. Written by the
    composer rather than by hand, one per application, so the list only grows.
    """
    stmt = (
        select(CoverLetter)
        .where(CoverLetter.user_id == user.id)
        .order_by(CoverLetter.id.desc())
    )
    return [_letter_out(row) for row in paginate(db, stmt, page, response)]


def _get_owned_letter(db: Session, user: User, letter_id: int) -> CoverLetter:
    row = db.get(CoverLetter, letter_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Cover letter not found"
        )
    return row


@router.get("/cover-letter/{letter_id}", response_model=CoverLetterOut)
def get_cover_letter(
    letter_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CoverLetterOut:
    return _letter_out(_get_owned_letter(db, user, letter_id))


@router.patch("/cover-letter/{letter_id}", response_model=CoverLetterOut)
def edit_cover_letter(
    letter_id: int,
    payload: CoverLetterEdit,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CoverLetterOut:
    """Replace the body with the user's own wording.

    Marks the letter edited, which is what stops a later regeneration from
    quietly overwriting it.
    """
    letter = cover_letter_service.apply_edit(
        db, _get_owned_letter(db, user, letter_id), payload.body
    )
    db.commit()
    db.refresh(letter)
    return _letter_out(letter)


@router.get("/cover-letter/{letter_id}/download", response_class=PlainTextResponse)
def download_cover_letter(
    letter_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlainTextResponse:
    letter = _get_owned_letter(db, user, letter_id)
    stem = filename_stem(letter.job_company or letter.job_title or "job")
    filename = f"{stem or 'job'}-cover-letter.md"
    return PlainTextResponse(
        cover_letter_service.render_markdown(letter),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/tailor/{tailored_id}/pdf")
def download_tailored_pdf(
    tailored_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """The tailored resume as a PDF — what actually gets attached to an
    application, and what an ATS parses.

    Served from the bytes stored at tailoring time rather than re-rendered, so
    the file the candidate sends is the one they previewed. A run from before
    PDFs existed (or one whose render failed) 409s with a pointer to the
    Markdown download rather than silently handing back a different document.
    """
    row = _get_owned(db, user, tailored_id)
    if not row.has_pdf or not row.pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No PDF was rendered for this tailoring run — "
                "use /tailor/{id}/download for the Markdown version"
            ),
        )
    filename = row.pdf_filename or f"tailored-resume-{row.id}.pdf"
    return Response(
        content=row.pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _is_file_of_record(db: Session, row: TailoredResume) -> bool:
    """Whether an outbound message would resolve *row* as the resume it carries.

    Attachments are not stored on the message — they are resolved when the send
    happens, and re-resolved afterwards so a sent row can still show what it
    carried (:mod:`app.services.email_attachments`). That makes deleting a
    tailoring run a different act depending on which run it is: an older one is
    dead weight, and the one the resolver would pick is the only copy of a
    document a recruiter has already been sent.

    The resolver's rule is the newest *rendered* run for that exact posting, so
    that is the rule here — a superseded run stays deletable, which is most of
    what anybody wants to prune.

    Outbound mail counts whatever its status. A sent row is the record and a
    draft is a promise: the resolver runs at the *send*, so deleting the run a
    waiting draft would attach changes what that message carries without
    telling anybody. Degrading to the base resume is gentler than breaking a
    record, but it is still a silent edit to mail the user has not seen leave.
    """
    if row.job_posting_id is None or not row.has_pdf:
        return False
    newest = db.scalar(
        select(TailoredResume.id)
        .where(
            TailoredResume.user_id == row.user_id,
            TailoredResume.job_posting_id == row.job_posting_id,
            TailoredResume.pdf_generated_at.is_not(None),
        )
        .order_by(TailoredResume.id.desc())
        .limit(1)
    )
    if newest != row.id:
        return False
    return db.scalar(
        select(Email.id)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.user_id == row.user_id,
            Application.job_posting_id == row.job_posting_id,
            Email.direction == EmailDirection.SENT,
        )
        .limit(1)
    ) is not None


@router.delete("/tailor/{tailored_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tailored(
    tailored_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Discard a tailoring run.

    The list this prunes is the one screen in the product that only ever grew:
    a row per posting the pipeline tailored for, each carrying a rendered PDF in
    its own column, and no way to remove the run started against a job
    description pasted by mistake.

    Refused with a 409 when the run is what an outbound message would attach —
    see :func:`_is_file_of_record`. Deleting it there would not tidy a list, it
    would make a message the recruiter has already read unable to say what it
    sent.

    Letters written to accompany the run outlive it. A cover letter is its own
    artifact with its own edits and its own download, and losing one because the
    resume beside it was pruned is not what "delete this run" offers to do.
    """
    row = _get_owned(db, user, tailored_id)
    if _is_file_of_record(db, row):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This run is the resume attached to outreach that has gone out "
                "or is waiting to — tailor the posting again to supersede it "
                "first"
            ),
        )
    # Explicit rather than left to the column's ``ON DELETE SET NULL``: the
    # relationship is one-directional, so the ORM has no cascade to run, and
    # SQLite does not enforce the constraint unless foreign keys are switched on.
    for letter in db.scalars(
        select(CoverLetter).where(CoverLetter.tailored_resume_id == row.id)
    ):
        letter.tailored_resume_id = None
    db.delete(row)
    usage_events.record(db, "tailored_resume.deleted", user_id=user.id)
    db.commit()


@router.delete("/cover-letter/{letter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cover_letter(
    letter_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Discard a letter.

    Unlike the resume beside it, a letter travels because a message points at it
    by id — the composer decides a letter belongs on this outreach and nowhere
    else, and the sender re-renders whatever ``Email.cover_letter_id`` names. So
    the guard here is exact rather than a rule about resolution: any message
    naming this letter blocks the delete, drafts included. A draft is a promise
    about what will go out, and silently emptying it is the same bug as breaking
    a sent row's record, one step earlier.
    """
    letter = _get_owned_letter(db, user, letter_id)
    attached = db.scalar(
        select(Email.id).where(Email.cover_letter_id == letter.id).limit(1)
    )
    if attached is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This letter is attached to an outreach message — remove it "
                "from that message before deleting the letter"
            ),
        )
    db.delete(letter)
    usage_events.record(db, "cover_letter.deleted", user_id=user.id)
    db.commit()


@router.post("/fit-score", response_model=FitResponse)
def fit_score(
    payload: FitRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FitResponse:
    """Score how well a resume matches a job, 0-100, with the reasoning.

    Scoring is deterministic — the same resume and posting always produce the
    same number. Only the prose summary comes from a model.

    The number is also the same one the job feed shows, because it is scored
    under the same profile the feed matched the posting to; see
    ``smart_apply_service.scoring_intent``.
    """
    resume, job, posting, intent = _resolve(db, user, payload)
    row, result = svc.score_and_save(
        db,
        user,
        resume,
        job,
        posting,
        targeting=intent.targeting,
        profile_id=intent.profile_id,
    )
    usage_events.record(
        db,
        "fit.scored",
        user_id=user.id,
        commit=True,
        score=row.overall,
        profile_id=intent.profile_id,
    )
    return FitResponse(
        fit=_fit_out(row, result),
        parsed_job=_parsed_out(job),
    )
