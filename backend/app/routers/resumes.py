"""Resume endpoints — step 2 of the wizard: upload a file, get a parsed profile.

    POST   /resumes            -> upload one or more PDFs/Word files, parsed on the spot
    GET    /resumes            -> list parsed resumes
    GET    /resumes/{id}/file  -> the document itself, inline, for previewing
    GET    /resumes/{id}/preview
                               -> the same document as something a browser draws
    GET    /resumes/{id}/suggested-preferences
                               -> autopilot preferences this resume implies
    PATCH  /resumes/{id}       -> correct the extraction (optional)
    DELETE /resumes/{id}

There is deliberately no profile-creation endpoint: every field comes out of the
uploaded file.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session, defer
from starlette.concurrency import run_in_threadpool

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import Page, page_params, paginate
from app.core.patching import reject_nulls
from app.core.pii import safe_reason
from app.core.rate_limit import rate_limit
from app.core.uploads import read_capped, safe_upload_filename
from app.models.resume import Resume
from app.models.user import User
from app.schemas.resume import (
    ExtractedProfile,
    ResumeOut,
    ResumePatch,
    SuggestedPreferencesOut,
    SuggestedPreferenceValues,
)
from app.services import document_preview, email_attachments, usage_events
from app.services.preference_suggester import (
    PreferenceSuggestions,
    extract_profile,
    merge_suggestions,
    suggest_preferences,
)
from app.services.resume_parser import (
    extract_text_from_docx,
    extract_text_from_pdf,
    parse_resume,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/resumes", tags=["resumes"])

_MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_RESUMES_PER_UPLOAD = 5

# What a resume may arrive as, keyed by extension. A candidate's resume lives in
# Word until the moment it is sent, so PDF-only meant the commonest upload in the
# world — the .docx sitting in Documents — was refused, and the user was sent off
# to export a PDF before the product would talk to them.
#
# The extension decides, not the browser's ``content_type``: Chrome, Safari and
# Windows disagree about what a .docx is (three spellings in the wild, plus
# ``application/octet-stream`` from a drag-and-drop), and the extension is the one
# thing the user can see and we can trust.
_ACCEPTED_SUFFIXES = (".pdf", ".docx")
_ACCEPTED = " or ".join(_ACCEPTED_SUFFIXES)

# What the stored upload is served and attached as. Taken from the extension for
# the same reason the extension decides acceptance: the browser's own
# ``content_type`` is unreliable, and this value ends up on a MIME part a
# recruiter's mail client has to recognise.
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
}

# Word's pre-2007 binary format. Recognised only so the error can say something
# useful: it is not a zip of XML and there is no way to read it without a heavy
# dependency, so the honest answer is "save it as .docx or PDF".
_LEGACY_DOC = ".doc"


def suffix_for(filename: str) -> str | None:
    """The accepted extension *filename* carries, or ``None``."""
    lowered = (filename or "").lower()
    return next((s for s in _ACCEPTED_SUFFIXES if lowered.endswith(s)), None)


def extract_text(suffix: str, data: bytes) -> str:
    """Read *data* as *suffix*, dispatching to the right extractor.

    Dispatches through the module-level names on every call rather than through a
    table built at import: the extractors are the natural seam for a test to
    stand in for, and a dict of bound functions would quietly ignore that.
    """
    if suffix == ".docx":
        return extract_text_from_docx(data)
    return extract_text_from_pdf(data)


def _get_owned(db: Session, user: User, resume_id: int) -> Resume:
    resume = db.get(Resume, resume_id)
    if resume is None or resume.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found"
        )
    return resume


def _make_default(db: Session, user: User, resume: Resume) -> None:
    """Promote *resume* to the user's default, demoting any previous one."""
    db.execute(
        update(Resume)
        .where(Resume.user_id == user.id, Resume.id != resume.id)
        .values(is_default=False)
    )
    resume.is_default = True


@router.get("", response_model=list[ResumeOut])
def list_resumes(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    page: Page = Depends(page_params),
) -> list[Resume]:
    """Newest first. Bounded like every list here; see `app.core.pagination`."""
    # ``file_bytes`` is deferred on the model; ``raw_text`` is the other half of
    # the same argument and was not. It holds the resume's entire extracted text
    # — the raw material every composer and scorer reads — and ``ResumeOut``
    # exposes none of it, so listing resumes was loading a full CV per row to
    # render a filename and a headline. Deferred here rather than on the model
    # because the services genuinely want it, and a column-level default would
    # turn each of those reads into its own query.
    stmt = (
        select(Resume)
        .where(Resume.user_id == user.id)
        .order_by(Resume.id.desc())
        .options(defer(Resume.raw_text))
    )
    return paginate(db, stmt, page, response)


@router.post(
    "",
    response_model=list[ResumeOut],
    status_code=status.HTTP_201_CREATED,
    # Each file is text-extracted and then parsed by an LLM. `read_capped` bounds
    # how large one upload can be; this bounds how often. A single request may
    # carry several files, so the real ceiling is higher than the number here —
    # which is why it is a small number.
    dependencies=[Depends(rate_limit(10, 300, scope="resume-upload"))],
)
async def upload_resumes(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Resume]:
    """Upload one or more resumes; each is parsed into a profile immediately.

    PDF or Word (.docx). Multiple files are supported so a candidate can target
    several roles at once — one resume per role, each drivable by its own
    campaign.
    """
    if not files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No file provided"
        )
    if len(files) > _MAX_RESUMES_PER_UPLOAD:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"At most {_MAX_RESUMES_PER_UPLOAD} resumes per upload",
        )

    had_default = db.scalar(
        select(Resume.id).where(Resume.user_id == user.id, Resume.is_default.is_(True))
    )
    created: list[Resume] = []

    for upload in files:
        # Sanitised *before* the extension is read, not after it is stored. The
        # raw value is a client-written multipart header, and reading the
        # suffix off one string while storing another is how a name that looks
        # like a PDF to this check gets stored as something else — see
        # `app.core.uploads.safe_upload_filename`. One name from here down: the
        # one that is checked, the one in the errors, the one on the row.
        filename = safe_upload_filename(upload.filename, fallback="resume.pdf")
        suffix = suffix_for(filename)
        if suffix is None:
            detail = f"{filename}: resumes must be {_ACCEPTED}"
            if filename.lower().endswith(_LEGACY_DOC):
                detail = (
                    f"{filename}: Word 97-2003 files can't be read — open it and "
                    'use "Save As" to make a .docx or a PDF.'
                )
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=detail
            )
        # Capped during the read, not after it: see `app.core.uploads`.
        data = await read_capped(upload, _MAX_RESUME_BYTES, filename=filename)
        if not data:
            # Answered here rather than left to the extractor. Both extractors
            # do refuse an empty payload, but they refuse it the way they
            # refuse a corrupt one — pypdf with an `EmptyFileError` whose class
            # name is all the user gets, and a stack trace in the logs of a
            # deployment nobody needs to debug. "The file is empty" is a
            # complete diagnosis, and it is the same sentence the attachment
            # endpoint already gives.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{filename}: the file is empty",
            )
        try:
            # Off the event loop. Both halves of this are long, blocking and
            # synchronous — pypdf walks every page of the document, and
            # `parse_resume` then makes a *network* call to a model provider
            # with a 60-second timeout, once per provider in the fallback
            # chain. This endpoint is `async def`, so calling either one
            # directly runs it on the event loop, where nothing else in the
            # process gets a turn until it returns: not another user's
            # dashboard, not a tracking pixel, not the health check the
            # deploy verifier reads. Five files per request multiplies it.
            #
            # `run_in_threadpool` is the whole fix. It is safe here because
            # neither function touches the session — the ORM work stays on the
            # loop, below — and the provider circuit breaker they share is
            # already lock-guarded for the Celery workers that call it.
            text = await run_in_threadpool(extract_text, suffix, data)
        except Exception as exc:  # noqa: BLE001 - surface a clean 422 for bad files
            # ``ValueError`` is this codebase's own contract for "the file is
            # not what it claims" — ``extract_text_from_docx`` raises it with a
            # sentence written for the person who uploaded the file, and that
            # sentence is the whole point of the 422. It travels.
            #
            # Anything else came out of a library, and reads like it: a pypdf
            # object number, a zipfile offset, occasionally a temp path. That
            # is written for a developer, and this detail is rendered on screen
            # next to the filename — so only the class name goes, and the full
            # exception goes to the log under the request id the response
            # already carries.
            if not isinstance(exc, ValueError):
                logger.warning(
                    "resume upload could not be read",
                    exc_info=True,
                    extra={"resume_filename": filename},
                )
            reason = str(exc) if isinstance(exc, ValueError) else safe_reason(exc)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{filename}: could not be read ({reason})",
            ) from exc
        if not text.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{filename}: no text found — scanned or image-only files "
                    "aren't supported yet."
                ),
            )

        parsed = await run_in_threadpool(parse_resume, text)
        resume = Resume(
            user_id=user.id,
            filename=filename,
            raw_text=text,
            # Kept verbatim so this exact file is what a recruiter opens. See
            # the note on Resume.file_bytes.
            file_bytes=data,
            file_content_type=_CONTENT_TYPES[suffix],
            file_size=len(data),
            full_name=parsed.full_name or user.full_name,
            email=parsed.email or user.email,
            phone=parsed.phone,
            location=parsed.location,
            headline=parsed.headline,
            summary=parsed.summary,
            years_experience=parsed.years_experience,
            seniority=parsed.seniority,
            skills=parsed.skills,
            target_roles=parsed.target_roles,
            target_industries=parsed.target_industries,
            experience=parsed.experience,
            education=parsed.education,
            links=parsed.links,
            parsed_with=parsed.parsed_with,
        )
        db.add(resume)
        created.append(resume)

    db.flush()
    # The first resume a user ever uploads becomes the campaign default.
    if not had_default and created:
        _make_default(db, user, created[0])

    # Backfill the account name from the resume when registration left it blank.
    if not user.full_name and created[0].full_name:
        user.full_name = created[0].full_name

    usage_events.record(
        db,
        "resume.uploaded",
        user_id=user.id,
        count=len(created),
        # Which parser answered, per upload. The chain degrades silently to a
        # keyword fallback when no model is reachable, and how often that
        # happens is a fact about the deployment that nothing else counts.
        parsed_with=sorted({r.parsed_with for r in created if r.parsed_with}),
        first=not had_default,
    )
    db.commit()
    for resume in created:
        db.refresh(resume)
    return created


def _suggestions_response(
    suggested: PreferenceSuggestions,
    resumes: list[Resume],
    *,
    resume_id: int | None,
) -> SuggestedPreferencesOut:
    profile = extract_profile(resumes)
    return SuggestedPreferencesOut(
        resume_id=resume_id,
        suggestions=SuggestedPreferenceValues(**suggested.values),
        sources=suggested.sources,
        notes=suggested.notes,
        prefilled_fields=suggested.prefilled_fields,
        profile=ExtractedProfile(
            resume_ids=profile.resume_ids,
            resume_count=len(profile.resume_ids),
            full_name=profile.full_name,
            location=profile.location,
            seniority=profile.seniority,
            years_experience=profile.years_experience,
            skills=profile.skills,
            titles=profile.titles,
        ),
    )


@router.get("/suggested-preferences", response_model=SuggestedPreferencesOut)
def merged_suggested_preferences(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> SuggestedPreferencesOut:
    """One search, derived from *every* resume the user has uploaded.

    This is what the setup page pre-fills from. Merging rather than picking one
    resume is the point: someone who uploads a backend resume and a platform
    resume is telling us they'll take either, and a search built off only the
    default would silently drop half of what they asked for.

    Read-only and side-effect free — nothing is saved until the user reviews the
    form and presses save. With no resumes uploaded it returns the product
    defaults and an empty profile rather than a 404, because the setup page asks
    for this before the user has necessarily uploaded anything.
    """
    resumes = list(
        db.scalars(
            select(Resume).where(Resume.user_id == user.id).order_by(Resume.id.desc())
        )
    )
    return _suggestions_response(merge_suggestions(resumes), resumes, resume_id=None)


@router.get("/{resume_id}/suggested-preferences", response_model=SuggestedPreferencesOut)
def suggested_preferences(
    resume_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SuggestedPreferencesOut:
    """The preferences one specific resume implies.

    Kept alongside the merged read for the case where a user has several resumes
    and wants the search shaped by exactly one of them.
    """
    resume = _get_owned(db, user, resume_id)
    return _suggestions_response(
        suggest_preferences(resume), [resume], resume_id=resume.id
    )


@router.get("/{resume_id}/file")
def resume_file(
    resume_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """The resume document itself, for reading in the page.

    Setup could take a resume and then only ever describe it back — a headline, a
    filename, a row of skills. Which is no way to tell two uploads of the same CV
    apart, and no way at all to check that the file about to be sent under a
    candidate's name is the one they meant to upload. So the document opens.

    Served ``inline`` so a PDF renders rather than downloads, and resolved
    through the same function the sender runs: the candidate's own upload when it
    is on file, else the PDF rendered from the parse. A preview that resolved the
    document its own way could show the upload while the sender attached a
    reconstruction, and the user would have no way to know.

    404 for a resume that isn't the caller's. 409 when there is no document to be
    had — a row uploaded before the bytes were kept, on a deployment without the
    renderer. That is an answer about the state of the resume, not a fault.
    """
    resume = _get_owned(db, user, resume_id)
    document = email_attachments.document_for_resume(resume)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "We don't have the original file for this resume — re-upload it "
                "to be able to read it here."
            ),
        )
    # What decides whether the browser renders the document or offers to save
    # it — and, for a .docx, which of the two the UI offers: there is no
    # in-page viewer for a Word file, so the page reads it from ``/preview``
    # and keeps these bytes for the download.
    #
    # Resolved through the same allowlist an emailed attachment goes through,
    # even though these bytes are the candidate's own upload rather than a
    # stranger's. The stored type comes off the upload's declared content type,
    # so "the user chose it" is the whole of the protection, and it costs
    # nothing to serve their own file under a type we are sure of.
    media_type, headers = email_attachments.serving_headers(
        document.mime_type, document.filename
    )
    return Response(content=document.content, media_type=media_type, headers=headers)


@router.get("/{resume_id}/preview")
def resume_preview(
    resume_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """The same document, as something a browser will actually draw.

    ``/file`` serves the resume as it is, which is right for a PDF and useless
    for a Word document: a browser pointed at a .docx draws nothing, and nothing
    on screen is indistinguishable from a broken preview. So the commonest
    resume format in the world — the .docx sitting in the candidate's Documents
    — was the one document this product would send under their name and could
    not show them. This endpoint converts it for reading.

    Reading only. The conversion never travels: the sender resolves its bytes
    through the same :func:`email_attachments.document_for_resume` this endpoint
    reads, and attaches *that* — the candidate's upload, byte for byte. What a
    recruiter opens is unaffected by anything below, and the UI says out loud
    that the frame holds a rendering rather than the file.

    A PDF is returned unchanged, so the page has one endpoint it can always
    read from. 409 when no rendering can be produced — a spreadsheet, a legacy
    .doc on a box without LibreOffice, a .docx that isn't one — carrying the
    sentence the UI shows before offering the download instead.
    """
    resume = _get_owned(db, user, resume_id)
    document = email_attachments.document_for_resume(resume)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "We don't have the original file for this resume — re-upload it "
                "to be able to read it here."
            ),
        )

    preview = document_preview.preview_for(
        document.filename,
        document.content,
        document.mime_type or email_attachments.media_type_for(document.filename),
    )
    if preview is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=document_preview.UNAVAILABLE_DETAIL,
        )

    return Response(
        content=preview.content,
        media_type=preview.media_type,
        headers={
            "Content-Disposition": email_attachments.inline_content_disposition(
                preview.filename
            ),
            # A rendering carries the name of the document it renders, so a
            # client has something truthful to label the frame with without
            # having to infer it from the original filename.
            "X-Preview-Of": email_attachments.header_safe(
                preview.source_filename or preview.filename
            ),
            # This response is a whole HTML document served from the API's
            # origin. Its body contains no markup the converter didn't write and
            # every text node is escaped, but a header that forbids scripts and
            # network access costs nothing to also be right about.
            "Content-Security-Policy": document_preview.CONTENT_SECURITY_POLICY,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch("/{resume_id}", response_model=ResumeOut)
def patch_resume(
    resume_id: int,
    payload: ResumePatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resume:
    """Correct an extraction — the only manual editing the product offers.

    ``model_dump`` rather than assigning the parsed models: ``experience`` and
    ``education`` are ``JSON`` columns, and handing SQLAlchemy a list of
    Pydantic objects serialises to whatever ``json.dumps`` makes of them — which
    is a ``TypeError`` at flush time, inside the request, after the other fields
    have already been assigned. The nested models exist to *validate* the shape
    on the way in; the column stores plain dicts, exactly as the parser writes
    them.

    A null clears a nullable field, which is the point for this endpoint in
    particular: "the parser invented a phone number" is a correction whose only
    honest value is empty. ``reject_nulls`` still refuses one for a column that
    cannot hold it — the JSON lists are ``NOT NULL``, and a stored ``null``
    there is the bug ``app.core.patching`` documents at length.
    """
    resume = _get_owned(db, user, resume_id)
    fields = payload.model_dump(exclude_unset=True, mode="json")
    # `exclude_unset` reaches *into* the nested entries, so correcting only a job
    # title would store `{"title": ...}` and drop the other three keys — a row
    # shaped differently from every row the parser writes, for every reader
    # downstream to discover one at a time. The list itself is opt-in (it is only
    # here at all if the client sent it); the keys inside each entry are not.
    for name in ("experience", "education"):
        entries = getattr(payload, name, None)
        if name in fields and entries is not None:
            fields[name] = [entry.model_dump(mode="json") for entry in entries]
    reject_nulls(Resume, fields)
    make_default = fields.pop("is_default", None)
    for name, value in fields.items():
        setattr(resume, name, value)
    if make_default:
        _make_default(db, user, resume)
    db.commit()
    db.refresh(resume)
    return resume


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    resume = _get_owned(db, user, resume_id)
    was_default = resume.is_default
    db.delete(resume)
    db.flush()
    if was_default:
        # Keep exactly one default alive so campaigns always resolve a resume.
        replacement = db.scalar(
            select(Resume).where(Resume.user_id == user.id).order_by(Resume.id.desc())
        )
        if replacement is not None:
            replacement.is_default = True
    db.commit()
