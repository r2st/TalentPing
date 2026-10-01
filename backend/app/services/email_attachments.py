"""Resolve the files that travel with an outbound email.

An application email without the resume attached is not an application — the
recruiter has a pitch and no document. Everything the pipeline needs was already
being produced (:mod:`app.services.smart_apply_service` renders and stores a
tailored PDF per posting); nothing was ever picking it up at send time. This
module is that missing step, and it is deliberately the *sender's* job rather
than the composer's: a draft can sit in the review queue for days, and what
should go out is the best resume as of the send, not as of the draft.

Resolution order for the resume on one application:

1. The resume tailored to **this posting**, if one was rendered. Never a
   tailoring run for some other job — its header carries that company's role
   title, and sending Acme's resume to Initech is worse than sending a generic
   one.
2. The resume belonging to the **profile this application was filed under**. A
   candidate with a backend profile and a nursing profile has two documents, and
   the one that argues for the matched intent is the only correct answer. This
   is what an inbound recruiter reply resolves through: it has no posting to
   tailor against, but :mod:`app.services.recruiter_reply_service` has already
   decided which profile the message matched, and that decision should reach the
   attachment rather than being re-derived as "whichever resume is default".
3. A PDF rendered from the campaign's base resume.

The cover letter is resolved differently, because *whether* it travels is a
decision only the composer can make: a letter belongs on the first outreach and
nowhere else, and a follow-up that re-attached it would be sending the same
letter twice. So the composer records ``Email.cover_letter_id`` and this module
only decides what that letter looks like on the day — re-rendering it so an edit
made while the draft waited is the version the recruiter receives.

Every step is best-effort: the resolvers return ``None`` rather than raising,
because a missing attachment must never swallow the email. The caller records
what actually travelled on the ``Email`` row, so "sent without a resume" is
visible after the fact instead of being silently indistinguishable from "sent
with one".
"""
from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.application import Application
from app.models.campaign import Campaign
from app.models.cover_letter import CoverLetter
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_attachment import EmailAttachment
from app.models.email_thread import EmailThread
from app.models.profile import Profile
from app.models.recruiter_email import RecruiterEmail
from app.models.resume import Resume
from app.models.tailored_resume import TailoredResume
from app.models.user import User
from app.services import gmail_accounts, gmail_service, resume_pdf
from app.services.gmail_service import Attachment

logger = logging.getLogger(__name__)


@dataclass
class _BaseResumeView:
    """A base resume shaped like a tailoring run, for :func:`resume_pdf.render_pdf`.

    The renderer only ever reads these five attributes off the tailored side, so
    an untailored resume can reuse the same (ATS-safe) layout instead of needing
    a second template. ``job_title``/``job_company`` stay ``None``: there is no
    posting behind this render, and the renderer falls back to the candidate's
    own headline.
    """

    job_title: str | None = None
    job_company: str | None = None
    tailored_summary: str | None = None
    ordered_skills: list[str] = field(default_factory=list)
    highlighted_experience: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_resume(cls, resume: Resume) -> _BaseResumeView:
        return cls(
            tailored_summary=resume.summary,
            ordered_skills=list(resume.skills or []),
            highlighted_experience=[
                {
                    "company": entry.get("company"),
                    "title": entry.get("title"),
                    "start": entry.get("start"),
                    "end": entry.get("end"),
                    # No re-angled bullets exist off a base resume; the renderer
                    # simply prints the role line when a role has none.
                    "bullets": entry.get("bullets") or [],
                }
                for entry in (resume.experience or [])[:6]
            ],
        )


def _filename_for_base(resume: Resume) -> str:
    """The derived name, which is the candidate's name and nothing else.

    Shares :func:`app.services.resume_pdf.filename_stem` with the tailored-resume and
    cover-letter names, so one person's three documents agree on the spelling
    of their own name rather than each mangling it differently.
    """
    return f"{resume_pdf.filename_stem(resume.full_name) or 'resume'}-resume.pdf"


def filename_for(resume: Resume) -> str:
    """What this resume is called on the wire.

    The uploaded name when the upload itself is on file, which is the normal
    case and the honest one — it is the document, so it should arrive under its
    own name. Only a re-render gets a derived name, and that name is the same
    for every resume one person owns: proof, if the user sees it, that they are
    looking at a reconstruction rather than their file.
    """
    if resume.has_original_file and resume.filename:
        return resume.filename
    return _filename_for_base(resume)


def _original_upload(resume: Resume) -> Attachment | None:
    """The candidate's own file, or ``None`` if we never kept it.

    Reading ``file_bytes`` loads a deferred column, so the cheap ``file_size``
    check comes first — every caller that only needs the *name* goes through
    :func:`filename_for` and never reaches here.
    """
    if not resume.has_original_file:
        return None
    content = resume.file_bytes
    if not content:
        # file_size says there should be bytes and there are none. A row like
        # this is corrupt rather than empty, so say so and let the caller fall
        # back to a render instead of attaching a zero-byte document.
        logger.warning("resume %s records a file but holds no bytes", resume.id)
        return None
    return Attachment(
        filename=filename_for(resume),
        content=content,
        mime_type=resume.file_content_type or media_type_for(filename_for(resume)),
    )


def document_for_resume(resume: Resume) -> Attachment | None:
    """The document that *is* this resume: the upload, else a re-render.

    Split out of :func:`resume_attachment_for_application` so the file a user
    previews and the file a recruiter receives cannot drift apart. They are the
    same bytes resolved by the same code — a preview served off a second
    implementation would be free to say the candidate's own PDF is on file while
    the sender was still attaching a reconstruction, which is precisely the
    failure the original bytes were kept to end.

    ``None`` means there is no document to be had: no upload on file *and* no
    renderer to fall back on. Callers decide what to do about that — the sender
    goes out without one, the preview endpoint says so.
    """
    original = _original_upload(resume)
    if original is not None:
        return original

    if not resume_pdf.is_available():
        logger.warning("reportlab unavailable — no resume document can be produced")
        return None

    try:
        view = _BaseResumeView.from_resume(resume)
        return Attachment(
            filename=filename_for(resume),
            content=resume_pdf.render_pdf(resume, view),
        )
    except Exception:  # noqa: BLE001 - never let a render failure block the send
        logger.warning("base resume PDF render failed", exc_info=True)
        return None


def header_safe(filename: str) -> str:
    """*filename* reduced to characters that cannot terminate or inject a header.

    A stored filename is user-influenced data — it comes off an upload, or off
    a MIME part a stranger sent — and it lands in a response header. Anything
    that could close the quoting or start a new line is collapsed to a dash.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-")


def inline_content_disposition(filename: str) -> str:
    """``inline`` plus a header-safe filename.

    ``inline`` is the whole point: it is what makes a browser render the PDF in
    place instead of downloading a file the user then has to go find.

    Lives here rather than in one router because several endpoints now serve a
    document for reading in the page: an email's attachment, a resume, and the
    browser-readable rendering of either.
    """
    return f'inline; filename="{header_safe(filename) or "attachment.pdf"}"'


def attachment_content_disposition(filename: str) -> str:
    """``attachment`` plus a header-safe filename — save it, never render it.

    The other half of :func:`inline_content_disposition`, for the files
    :func:`served_media_type` refuses to let a browser draw.
    """
    return f'attachment; filename="{header_safe(filename) or "attachment.bin"}"'


#: Types a document may be *rendered* as, rather than merely downloaded.
#:
#: An allowlist, and it has to be one. The type on an inbound attachment is
#: whatever the sending mail client wrote in the MIME part — see
#: ``gmail_service.list_attachments``, which reads ``part["mimeType"]`` verbatim
#: — so it is a value a stranger chooses and hands to us. Passing it into a
#: ``Response(media_type=...)`` served ``inline`` let that stranger pick what
#: kind of document the browser thinks it is holding.
#:
#: That is not theoretical here. The web client fetches this endpoint and frames
#: the result from ``URL.createObjectURL(blob)``, and a blob URL **inherits the
#: origin of the page that created it** while the blob's type comes from this
#: header. A recruiter attaching ``interview-brief.html`` therefore got their
#: markup framed as a document on the app's own origin, where the session's
#: bearer token lives in ``localStorage`` — a stored cross-site scripting hole
#: whose input arrives by email from anyone who can find the candidate's
#: address.
#:
#: ``image/svg+xml`` is deliberately *absent*. An SVG rendered as a document is
#: a scripting context like any other; it is an image only in the sense that it
#: is drawn.
_RENDERABLE_TYPES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
    }
)

#: What a sender says when it does not know, and what a browser must never take
#: as licence to guess. Treated as "unstated" so the filename gets a say.
_UNSTATED_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

#: Types a browser executes rather than displays. Never claimed, whatever the
#: sender declared and whatever the extension agrees to — these are the ones the
#: allowlist above exists to keep out, and naming them separately is what lets a
#: .docx keep its own type while ``brief.html`` does not keep its.
_SCRIPTABLE_TYPES = frozenset(
    {
        "text/html",
        "text/xml",
        "text/xsl",
        "text/javascript",
        "application/xhtml+xml",
        "application/xml",
        "application/xslt+xml",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "image/svg+xml",
    }
)

#: The type a file that may not be rendered is served as. Nothing interprets it.
OPAQUE_MEDIA_TYPE = "application/octet-stream"

#: What the response is allowed to do if a browser ever loads it as a document.
#: Belt-and-braces beside :func:`served_media_type` rather than the defence —
#: the page reads these bytes through a blob URL, which carries no headers at
#: all, so the *type* is what actually decides this and the header only covers
#: someone navigating to the URL directly.
ATTACHMENT_CONTENT_SECURITY_POLICY = "default-src 'none'; sandbox"


def _bare_type(value: str | None) -> str:
    """A media type without its parameters, lower-cased. ``"" `` when absent."""
    return (value or "").split(";")[0].strip().lower()


def served_media_type(mime_type: str | None, filename: str) -> str:
    """The type this file may safely be served as.

    *mime_type* is what the sender declared, and the question this answers is
    how much of that to believe. Four rules, in order:

    1. A type on :data:`_RENDERABLE_TYPES` is honoured. This is the PDF the
       whole preview exists for.
    2. A sender that declares nothing — or the ``application/octet-stream``
       every mail client falls back to when it is guessing — gets the benefit of
       the filename instead. That is the ordinary case for a PDF attached from a
       phone, and refusing it would break the feature for the file it is for.
    3. A type on :data:`_SCRIPTABLE_TYPES` is refused outright, even when the
       extension agrees with it. ``brief.html`` really is HTML; that is the
       problem, not a mislabelling of it.
    4. Anything else keeps its declared type **only while the filename agrees**.
       A ``.docx`` announced as a ``.docx`` is served as one — it is inert, and
       a browser has never rendered a zip of XML — so the user downloads a file
       their machine still recognises. A ``.pdf`` announced as something else is
       a file whose sender is wrong about one of the two, and neither answer is
       worth serving under a name that might get it drawn.

    Everything that falls through comes back as :data:`OPAQUE_MEDIA_TYPE`, which
    no browser interprets. Note that only rule 1 and rule 2 produce a type that
    :func:`content_disposition_for` will offer ``inline``; the rest are
    downloads whatever their type.
    """
    declared = _bare_type(mime_type)
    guessed = _bare_type(media_type_for(filename))

    if declared in _RENDERABLE_TYPES:
        return declared
    if declared in _UNSTATED_TYPES:
        return guessed if guessed in _RENDERABLE_TYPES else OPAQUE_MEDIA_TYPE
    if declared in _SCRIPTABLE_TYPES or declared != guessed:
        return OPAQUE_MEDIA_TYPE
    return declared


def content_disposition_for(media_type: str, filename: str) -> str:
    """``inline`` for something the page may draw, ``attachment`` otherwise.

    Paired with :func:`served_media_type` so the two headers can never disagree:
    a file we refuse to name a renderable type for is also a file we refuse to
    ask the browser to display.
    """
    if _bare_type(media_type) in _RENDERABLE_TYPES:
        return inline_content_disposition(filename)
    return attachment_content_disposition(filename)


def serving_headers(mime_type: str | None, filename: str) -> tuple[str, dict[str, str]]:
    """``(media_type, headers)`` for serving one document's bytes as they are.

    One call so that every endpoint handing back a stored or fetched file gets
    the same three decisions — what type to claim, whether to offer it for
    display, and the two headers that stop a browser second-guessing either.

    ``nosniff`` is what makes :func:`served_media_type` binding. Without it a
    browser is free to content-sniff its way back to ``text/html`` on a response
    we deliberately labelled opaque, which would hand the decision straight back
    to whoever wrote the bytes.
    """
    media_type = served_media_type(mime_type, filename)
    return media_type, {
        "Content-Disposition": content_disposition_for(media_type, filename),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": ATTACHMENT_CONTENT_SECURITY_POLICY,
    }


@dataclass(frozen=True)
class PlanPrefetch:
    """The per-email lookups behind :func:`plan_for_email`, read once for many.

    A review queue or an inbox page calls ``plan_for_email`` once per row, and
    each call asks four questions that a list can answer in bulk: what the
    inbound pipeline selected for this message, whether the posting has a
    tailored PDF, what the user's fallback resume is, and which files they
    attached by hand. Building this first turns those into four queries for the
    whole page.

    It is a *cache, not a substitute*. Every field is consulted only for keys
    this object was actually built from — ``covered_email_ids`` and
    ``covered_posting_ids`` say which those are, and anything outside them falls
    through to the same query the un-prefetched path would run. That matters
    because a miss and an empty answer are indistinguishable in a plain dict:
    without the guard, planning an email the prefetch had never heard of would
    quietly report no attachments rather than reading them. Being a pure cache
    also means passing one can change how many queries run, never what is
    resolved — which is the only reason it is safe near the send path, where
    this chain decides the document a real employer receives.
    """

    user_id: int
    covered_email_ids: frozenset[int]
    covered_posting_ids: frozenset[int]
    user_attachments: dict[int, list[EmailAttachment]]
    recruiter_emails: dict[int, RecruiterEmail]
    tailored: dict[int, TailoredResume]
    default_resume: Resume | None
    #: Whether :attr:`default_resume` was actually read. ``None`` means two
    #: different things — the user owns no resume, or nobody looked — and only
    #: the first may be reported as an answer. Kept as its own flag rather than
    #: inferred from the field being set, so that skipping the read (for an
    #: empty page, say) stays a safe change: the worst it can do is fall back to
    #: the query, never quietly strip a resume off an email to an employer.
    default_resume_loaded: bool

    def covers_email(self, email_id: int | None) -> bool:
        """Whether this cache was built from *email_id*.

        Membership implies ownership: the set is built from one user's messages,
        so an id in it is one of theirs.
        """
        return email_id is not None and email_id in self.covered_email_ids

    def covers_posting(self, user_id: int, posting_id: int | None) -> bool:
        return (
            user_id == self.user_id
            and posting_id is not None
            and posting_id in self.covered_posting_ids
        )

    def covers_default_resume(self, user_id: int) -> bool:
        return user_id == self.user_id and self.default_resume_loaded


def prefetch_plans(db: Session, user: User, emails: list[Email]) -> PlanPrefetch:
    """Read everything :func:`plan_for_email` needs for *emails* in four queries.

    *emails* must all belong to *user*; the resulting cache answers for those
    messages and defers to the ordinary lookups for anything else.

    Callers should have eager-loaded ``Email.thread.application``: this walks
    that path to learn which postings to ask about, and one that has not pays
    those loads here rather than in its loop. Forgetting costs a speedup, never
    an answer.
    """
    email_ids = {email.id for email in emails if email.id is not None}

    posting_ids = {
        posting_id
        for email in emails
        if (thread := email.thread) is not None
        and (application := thread.application) is not None
        and (posting_id := application.job_posting_id) is not None
    }

    user_attachments: dict[int, list[EmailAttachment]] = {}
    recruiter_emails: dict[int, RecruiterEmail] = {}
    tailored: dict[int, TailoredResume] = {}

    if email_ids:
        for row in db.scalars(
            select(EmailAttachment)
            .where(EmailAttachment.email_id.in_(email_ids))
            .order_by(EmailAttachment.id)
        ):
            user_attachments.setdefault(row.email_id, []).append(row)
        for row in db.scalars(
            select(RecruiterEmail).where(RecruiterEmail.reply_email_id.in_(email_ids))
        ):
            recruiter_emails.setdefault(row.reply_email_id, row)

    if posting_ids:
        # Ascending, so each posting's later rows overwrite its earlier ones and
        # the highest id survives — the same answer as the per-posting
        # ``order_by(id.desc())`` scalar, for one query.
        for row in db.scalars(
            select(TailoredResume)
            .where(
                TailoredResume.user_id == user.id,
                TailoredResume.job_posting_id.in_(posting_ids),
                TailoredResume.pdf_generated_at.is_not(None),
            )
            .order_by(TailoredResume.id)
        ):
            tailored[row.job_posting_id] = row

    # Ordered exactly as the fallback in `_base_resume_for` orders it, so the
    # first row *is* that fallback. Reading them all also warms the identity
    # map, which is what makes the `db.get(Resume, ...)` in every resolver above
    # it free rather than a query per draft.
    resumes = list(
        db.scalars(
            select(Resume)
            .where(Resume.user_id == user.id)
            .order_by(Resume.is_default.desc(), Resume.id.desc())
        )
    )

    return PlanPrefetch(
        user_id=user.id,
        covered_email_ids=frozenset(email_ids),
        covered_posting_ids=frozenset(posting_ids),
        user_attachments=user_attachments,
        recruiter_emails=recruiter_emails,
        tailored=tailored,
        default_resume=resumes[0] if resumes else None,
        default_resume_loaded=True,
    )


def _tailored_for_posting(
    db: Session,
    user_id: int,
    job_posting_id: int | None,
    *,
    prefetch: PlanPrefetch | None = None,
) -> TailoredResume | None:
    """The newest rendered PDF tailored to this exact posting, if any."""
    if job_posting_id is None:
        return None
    if prefetch is not None and prefetch.covers_posting(user_id, job_posting_id):
        return prefetch.tailored.get(job_posting_id)
    return db.scalar(
        select(TailoredResume)
        .where(
            TailoredResume.user_id == user_id,
            TailoredResume.job_posting_id == job_posting_id,
            TailoredResume.pdf_generated_at.is_not(None),
        )
        .order_by(TailoredResume.id.desc())
    )


def _profile_resume_for(db: Session, user: User, profile_id: int | None) -> Resume | None:
    """The document the matched profile argues with, or ``None``.

    Deliberately does *not* fall back the way
    :func:`app.services.profile_service.resume_for` does. A profile whose resume
    was deleted should drop through to the ordinary campaign/default resolution
    below rather than short-circuit it — this function answers only "does this
    profile name a document of its own?".
    """
    if profile_id is None:
        return None
    profile = db.get(Profile, profile_id)
    if profile is None or profile.user_id != user.id or profile.resume_id is None:
        return None
    resume = db.get(Resume, profile.resume_id)
    if resume is None or resume.user_id != user.id:
        return None
    return resume


def _selected_resume_for(
    db: Session, user: User, email: Email, *, prefetch: PlanPrefetch | None = None
) -> Resume | None:
    """The resume the inbound pipeline chose for *this* role, if it chose one.

    :mod:`app.services.resume_selector` scores every document the candidate owns
    against the role the recruiter described and records the winner on the
    ``RecruiterEmail`` at draft time. Only the *choice* is made then — the
    rendering still happens here at send time, so a draft that waits three days
    goes out with the resume as it stands today. That split is the point: the
    review screen can name the document while it is still reviewable, and the
    bytes are never stale.

    Ranks above the profile's own resume because it is strictly more specific:
    the profile says which career this is, the selection says which role.
    """
    if prefetch is not None and prefetch.covers_email(email.id):
        row = prefetch.recruiter_emails.get(email.id)
    else:
        row = db.scalar(
            select(RecruiterEmail).where(RecruiterEmail.reply_email_id == email.id)
        )
    if row is None or row.selected_resume_id is None:
        return None
    resume = db.get(Resume, row.selected_resume_id)
    if resume is None or resume.user_id != user.id:
        return None
    return resume


def _picked_resume_for(db: Session, user: User, email: Email) -> Resume | None:
    """The resume the *user* chose for this message, if they chose one.

    Outranks every resolver below it, including a tailoring run for the posting.
    That is the whole point: resolution is inference, and when the candidate
    opens their own draft and picks a different document, no amount of scoring
    should be allowed to overrule them.
    """
    if email.attachment_resume_id is None:
        return None
    resume = db.get(Resume, email.attachment_resume_id)
    if resume is None or resume.user_id != user.id:
        return None
    return resume


def _base_resume_for(
    db: Session,
    user: User,
    campaign: Campaign | None,
    profile_id: int | None = None,
    email: Email | None = None,
    *,
    prefetch: PlanPrefetch | None = None,
) -> Resume | None:
    """The user's pick, else the chosen resume, else the matched profile's, else
    the campaign's, else the default."""
    if email is not None:
        picked = _picked_resume_for(db, user, email)
        if picked is not None:
            return picked
        selected = _selected_resume_for(db, user, email, prefetch=prefetch)
        if selected is not None:
            return selected

    profile_resume = _profile_resume_for(db, user, profile_id)
    if profile_resume is not None:
        return profile_resume
    if campaign is not None and campaign.resume_id is not None:
        resume = db.get(Resume, campaign.resume_id)
        if resume is not None and resume.user_id == user.id:
            return resume
    if prefetch is not None and prefetch.covers_default_resume(user.id):
        return prefetch.default_resume
    return db.scalar(
        select(Resume)
        .where(Resume.user_id == user.id)
        .order_by(Resume.is_default.desc(), Resume.id.desc())
    )


def resume_attachment_for_application(
    db: Session, application: Application, user: User, email: Email | None = None
) -> Attachment | None:
    """The resume PDF to send with *application*, or ``None`` if there is none.

    *email* is optional because two callers have no email at all — form fills and
    career-page applications attach a document without composing a message. When
    it is given, the user's own pick wins outright, and after that a resume the
    inbound pipeline chose for this specific role wins over the profile's
    default.
    """
    # A user who picked a document has overruled the tailoring run too. The
    # tailored PDF is the better *automatic* answer and stays first whenever
    # nobody has said otherwise.
    picked = _picked_resume_for(db, user, email) if email is not None else None
    if picked is None:
        tailored = _tailored_for_posting(db, user.id, application.job_posting_id)
        if tailored is not None and tailored.pdf_bytes:
            return Attachment(
                filename=tailored.pdf_filename or "resume.pdf",
                content=tailored.pdf_bytes,
            )

    campaign = (
        db.get(Campaign, application.campaign_id)
        if application.campaign_id is not None
        else None
    )
    resume = _base_resume_for(db, user, campaign, application.profile_id, email)
    if resume is None:
        logger.warning(
            "no resume on file for user %s (profile=%s campaign=%s) — sending "
            "without one",
            user.id,
            application.profile_id,
            application.campaign_id,
        )
        return None

    # The document itself when we have it, the re-render when we don't — the same
    # resolution the preview endpoint serves, so what the user reads before
    # approving is what the recruiter opens.
    document = document_for_resume(resume)
    if document is None:
        logger.warning("no resume document for user %s — sending without one", user.id)
    return document


def resume_attachment_for_email(db: Session, email: Email) -> Attachment | None:
    """Resolve the resume for one outbound email. Never raises."""
    try:
        thread = db.get(EmailThread, email.thread_id)
        if thread is None:
            return None
        application = db.get(Application, thread.application_id)
        if application is None:
            return None
        user = db.get(User, application.user_id)
        if user is None:  # pragma: no cover - the FK makes this unreachable
            return None
        return resume_attachment_for_application(db, application, user, email)
    except Exception:  # noqa: BLE001 - the email matters more than the attachment
        logger.warning("resume attachment lookup failed for email %s", email.id, exc_info=True)
        return None


def cover_letter_attachment_for_email(
    db: Session, email: Email
) -> tuple[Attachment | None, int | None]:
    """The letter attached to *email*, and the id of the one that travelled.

    Driven strictly by ``email.cover_letter_id`` — see the module docstring for
    why the composer, not the sender, decides whether a letter goes at all.

    Returns ``(attachment, cover_letter_id)``. Both are ``None`` when nothing
    travelled, including when the letter exists but couldn't be rendered: a sent
    row's ``cover_letter_id`` should mean "this letter went out", not "we meant
    to send one". Never raises.
    """
    if email.cover_letter_id is None:
        return None, None
    try:
        letter = db.get(CoverLetter, email.cover_letter_id)
        if letter is None or not (letter.body or "").strip():
            logger.info(
                "cover letter %s is missing or empty — sending without it",
                email.cover_letter_id,
            )
            return None, None
        if not resume_pdf.is_available():
            logger.warning("reportlab unavailable — sending without the cover letter")
            return None, None

        resume = db.get(Resume, letter.resume_id) if letter.resume_id else None
        return (
            Attachment(
                filename=resume_pdf.letter_filename_for(letter, resume),
                content=resume_pdf.render_letter_pdf(letter, resume),
            ),
            letter.id,
        )
    except Exception:  # noqa: BLE001 - the email matters more than the attachment
        logger.warning(
            "cover letter attachment failed for email %s", email.id, exc_info=True
        )
        return None, None


#: The kinds of file that can travel. The first two are resolved from the
#: candidate's profile and can be suppressed by name; the third is a file the
#: user attached by hand and is removed by deleting the row.
RESUME = "resume"
COVER_LETTER = "cover_letter"
UPLOAD = "upload"

#: What :attr:`Email.suppressed_attachments` may name. An unknown value is
#: ignored rather than rejected — a column of free-form strings should not be
#: able to make a send fail.
SUPPRESSIBLE = frozenset({RESUME, COVER_LETTER})


def suppressed_kinds(email: Email) -> set[str]:
    """Which resolved attachments the user has removed from *email*."""
    return {k for k in (email.suppressed_attachments or []) if k in SUPPRESSIBLE}


@dataclass(frozen=True)
class PlannedFile:
    """One file queued to travel, and what kind of thing it is.

    The kind is what lets the UI offer the right control: a resume can be
    swapped for another of the candidate's documents, an upload can only be
    deleted, and both can be removed. ``attachment_id`` is set on uploads alone
    — it is the row to delete, and deleting by row id rather than by position
    means a concurrent change can't remove the wrong file.
    """

    filename: str
    kind: str
    attachment_id: int | None = None


@dataclass(frozen=True)
class AttachmentPlan:
    """What *would* travel with an email that hasn't been sent yet.

    Resolution happens at send time, which is right — a draft can wait days and
    should go out with the resume as it stands on the day. But it left a draft
    with nothing to show, and "it drafted a reply and I can't see the
    attachment" is indistinguishable, from the user's side, from the attachment
    not existing. This runs the same resolvers *without rendering*, so the
    review screen can say which document is queued up, or say why none is.

    ``files`` is what will be attached, in the order it will be attached — the
    same order :func:`files_for_email` produces, because the preview endpoint
    addresses attachments by position. ``reason`` is filled only when the resume
    could not be resolved, in the words the UI shows.
    """

    files: list[PlannedFile] = field(default_factory=list)
    reason: str | None = None

    @property
    def filenames(self) -> list[str]:
        """Just the names, for the callers that only ever showed names."""
        return [item.filename for item in self.files]


def resume_plan_for_email(
    db: Session, email: Email, *, prefetch: PlanPrefetch | None = None
) -> tuple[str | None, str | None]:
    """``(filename, reason_it_is_missing)`` for the resume on *email*.

    Names the document without rendering it: a review screen is read far more
    often than a send happens, and a PDF render per page load is a waste.
    """
    try:
        thread = db.get(EmailThread, email.thread_id)
        application = (
            db.get(Application, thread.application_id) if thread is not None else None
        )
        if application is None:
            return None, "This email isn't attached to an application."
        user = db.get(User, application.user_id)
        if user is None:  # pragma: no cover - the FK makes this unreachable
            return None, None

        picked = _picked_resume_for(db, user, email)
        if picked is None:
            tailored = _tailored_for_posting(
                db, user.id, application.job_posting_id, prefetch=prefetch
            )
            if tailored is not None and tailored.pdf_bytes:
                return tailored.pdf_filename or "resume.pdf", None

        campaign = (
            db.get(Campaign, application.campaign_id)
            if application.campaign_id is not None
            else None
        )
        resume = _base_resume_for(
            db, user, campaign, application.profile_id, email, prefetch=prefetch
        )
        if resume is None:
            return None, "No resume on file — upload one and it will be attached."
        # Only a re-render needs reportlab. A resume whose original is on file
        # travels as those bytes, so a server without the renderer still sends
        # it — and must not be told otherwise.
        if not resume.has_original_file and not resume_pdf.is_available():
            return None, "PDF rendering is unavailable on this server."
        return filename_for(resume), None
    except Exception:  # noqa: BLE001 - a preview must never break the page
        logger.warning("attachment plan failed for email %s", email.id, exc_info=True)
        return None, None


def user_attachments_for_email(
    db: Session, email: Email, *, prefetch: PlanPrefetch | None = None
) -> list[EmailAttachment]:
    """The files the user attached by hand, oldest first. Never raises."""
    try:
        if prefetch is not None and prefetch.covers_email(email.id):
            return list(prefetch.user_attachments.get(email.id, []))
        return list(
            db.scalars(
                select(EmailAttachment)
                .where(EmailAttachment.email_id == email.id)
                .order_by(EmailAttachment.id)
            )
        )
    except Exception:  # noqa: BLE001 - an attachment list must not break a page
        logger.warning("user attachments lookup failed for email %s", email.id, exc_info=True)
        return []


def plan_for_email(
    db: Session, email: Email, *, prefetch: PlanPrefetch | None = None
) -> AttachmentPlan:
    """Everything queued to travel with *email*, in send order. Never raises.

    Mirrors :func:`files_for_email` step for step — resume, letter, then the
    user's own files — because the preview endpoint resolves a file by the
    position this list gave it. A divergence between the two would silently open
    the wrong document.

    *prefetch* is a :class:`PlanPrefetch` from a caller planning a whole page of
    messages at once. It changes only how many queries this runs, never what it
    resolves; see that class for why that distinction is load-bearing.
    """
    suppressed = suppressed_kinds(email)
    files: list[PlannedFile] = []
    reason: str | None = None

    if RESUME not in suppressed:
        filename, reason = resume_plan_for_email(db, email, prefetch=prefetch)
        if filename:
            files.append(PlannedFile(filename=filename, kind=RESUME))

    if COVER_LETTER not in suppressed and email.cover_letter_id is not None:
        letter = db.get(CoverLetter, email.cover_letter_id)
        if letter is not None and (letter.body or "").strip() and resume_pdf.is_available():
            resume = db.get(Resume, letter.resume_id) if letter.resume_id else None
            files.append(
                PlannedFile(
                    filename=resume_pdf.letter_filename_for(letter, resume),
                    kind=COVER_LETTER,
                )
            )

    files.extend(
        PlannedFile(filename=row.filename, kind=UPLOAD, attachment_id=row.id)
        for row in user_attachments_for_email(db, email, prefetch=prefetch)
    )
    return AttachmentPlan(files=files, reason=reason)


class AttachmentUnavailable(RuntimeError):
    """An inbound attachment exists but its bytes could not be fetched.

    Distinct from "there is nothing at that position", which is a 404. This is
    a file the user can see listed and we failed to deliver — a disconnected
    mailbox, a revoked token, Gmail refusing the id. The message is written to
    be shown to the user, because "it just doesn't open" is the complaint this
    whole path exists to stop producing.
    """


def inbound_attachments_for(email: Email) -> list[gmail_service.InboundAttachment]:
    """Everything an inbound message arrived carrying, described but unfetched.

    Reads the description recorded at ingest rather than asking Gmail, so
    listing a mailbox costs no API calls. Rows that can't be redeemed are
    dropped rather than raising — see
    :meth:`gmail_service.InboundAttachment.from_dict`.
    """
    return [
        item
        for row in (email.inbound_attachments or [])
        if (item := gmail_service.InboundAttachment.from_dict(row)) is not None
    ]


def inbound_filenames_for(email: Email) -> list[str]:
    """The names to list against an inbound message, in the order they arrived."""
    return [item.filename for item in inbound_attachments_for(email)]


def scanned_filenames_for(rows: list | None) -> list[str]:
    """The names on :attr:`RecruiterEmail.attachments`, the scan's own record.

    A second store of the same descriptions, kept for a message the scanner saw
    before anything threaded it — see ``recruiter_inbox._recruiter_attachment_names``.
    Not read through :meth:`gmail_service.InboundAttachment.from_dict`, which
    drops a row whose ``attachment_id`` is missing: nothing opens a file from
    here, so a name with no id is still worth *saying*, where on the conversation
    row it would be a badge that opens nothing.

    Decoded on the way out for the reason that method gives: a row written before
    ``gmail_service.part_filename`` existed holds the encoded word Gmail handed
    over, and this is the only place it can be read as a name again.
    """
    return [
        name
        for row in (rows or [])
        if isinstance(row, dict)
        and (name := gmail_service.decoded_filename(row.get("filename")))
    ]


def _account_for_inbound(user: User, email: Email, *, db: Session | None = None):
    """The mailbox to redeem this message's attachment ids against.

    An ``attachmentId`` is only meaningful to the mailbox that holds the
    message, so a user with two connected accounts must not have a file
    fetched from the wrong one — it would 404 rather than return the wrong
    document, but it would 404 on mail that is sitting right there.

    The address the message was delivered to picks the account, and the primary
    is the fallback for older rows that never recorded one. That rule predates
    ``email_threads.gmail_account_id`` and is now the *second* thing consulted
    rather than the first: when the thread names its mailbox, that is a stored
    fact and this is an inference from a header.
    """
    delivered_to = (email.to_address or "").strip().lower()
    if delivered_to:
        for account in user.gmail_accounts:
            if account.email and account.email.lower() in delivered_to:
                return account
    if db is not None:
        thread = db.get(EmailThread, email.thread_id)
        return gmail_accounts.resolve_for_thread(db, thread, user=user)
    return user.primary_gmail


def inbound_attachment_at(db: Session, email: Email, index: int) -> Attachment | None:
    """The bytes of the *index*-th file on an inbound message.

    ``None`` when nothing is listed at that position. Raises
    :class:`AttachmentUnavailable` when something *is* listed and Gmail would
    not give it up — deliberately not swallowed, because the user is looking at
    a filename and pressing it, and silence there is the bug being fixed.
    """
    items = inbound_attachments_for(email)
    if index < 0 or index >= len(items):
        return None
    item = items[index]

    if not email.gmail_message_id:
        raise AttachmentUnavailable(
            "This message was recorded without its Gmail id, so its "
            "attachments can't be fetched."
        )

    thread = db.get(EmailThread, email.thread_id)
    application = (
        db.get(Application, thread.application_id) if thread is not None else None
    )
    user = db.get(User, application.user_id) if application is not None else None
    if user is None:  # pragma: no cover - the FK chain makes this unreachable
        raise AttachmentUnavailable("This message isn't attached to an account.")

    account = _account_for_inbound(user, email, db=db)
    if account is None:
        raise AttachmentUnavailable(
            "Reconnect your Gmail account to open attachments on received mail."
        )

    try:
        content = gmail_service.get_attachment(
            account, email.gmail_message_id, item.attachment_id
        )
    except gmail_service.GmailNotConfigured as exc:
        raise AttachmentUnavailable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
        logger.warning(
            "inbound attachment fetch failed for email %s (%s)",
            email.id,
            item.filename,
            exc_info=True,
        )
        raise AttachmentUnavailable(
            "Gmail wouldn't return this attachment. It may have been deleted "
            "from the mailbox."
        ) from exc

    if not content:
        raise AttachmentUnavailable("This attachment came back empty from Gmail.")

    return Attachment(
        filename=item.filename,
        content=content,
        # The sender's own declared type leads: Gmail knows what it is, and
        # guessing from the extension is only a fallback for a part that
        # arrived with a useless ``application/octet-stream``.
        mime_type=(
            item.mime_type
            if item.mime_type and item.mime_type != "application/octet-stream"
            else media_type_for(item.filename)
        ),
    )


def media_type_for(filename: str) -> str:
    """What a browser should treat this file as.

    Everything the pipeline produces today is a PDF, and the type is what
    decides whether a browser renders it in place or offers to save it — so it
    is guessed from the name rather than hardcoded, and falls back to a type no
    browser will try to interpret.
    """
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _planned_attachment(
    db: Session, email: Email, planned: PlannedFile
) -> Attachment | None:
    """The bytes behind one entry of an :class:`AttachmentPlan`.

    Addressed by *kind*, never by position. That is the whole point: the plan
    decided what sits at each index and this only produces the thing the plan
    already named, so a resolver that fails produces nothing at its own index
    rather than letting the next file slide up into it.

    ``None`` when the named document could not be produced — a render that
    failed, an upload row that holds no bytes. The caller distinguishes that
    from "nothing is listed there"; both are :data:`None` here because neither
    is a document.
    """
    if planned.kind == RESUME:
        return resume_attachment_for_email(db, email)
    if planned.kind == COVER_LETTER:
        return cover_letter_attachment_for_email(db, email)[0]

    row = (
        db.get(EmailAttachment, planned.attachment_id)
        if planned.attachment_id is not None
        else None
    )
    if row is None or row.email_id != email.id or not row.content:
        return None
    return Attachment(
        filename=row.filename,
        content=row.content,
        mime_type=row.content_type or media_type_for(row.filename),
    )


def sent_files_for_email(
    db: Session, email: Email, *, prefetch: PlanPrefetch | None = None
) -> list[PlannedFile]:
    """What travelled with a message that has already gone out, in send order.

    A record, not a resolution. :func:`plan_for_email` answers "what would go
    if you approved this now", which is the only honest answer for a draft and
    the wrong one for a message already delivered: the send writes down the
    resume filename it actually attached and the letter it actually rendered,
    and re-resolving answers with today's documents instead — a resume for a
    message that went out without one, which the sender logs and allows.

    Mirrors the send's own order — the recorded resume, the recorded letter,
    then the user's own files — for the same reason everything else here does:
    the screen and the endpoint address these by position.
    """
    files: list[PlannedFile] = []
    if email.attachment_filename:
        files.append(PlannedFile(filename=email.attachment_filename, kind=RESUME))

    if email.cover_letter_id is not None:
        letter = db.get(CoverLetter, email.cover_letter_id)
        if letter is not None:
            resume = db.get(Resume, letter.resume_id) if letter.resume_id else None
            files.append(
                PlannedFile(
                    filename=resume_pdf.letter_filename_for(letter, resume),
                    kind=COVER_LETTER,
                )
            )

    files.extend(
        PlannedFile(filename=row.filename, kind=UPLOAD, attachment_id=row.id)
        for row in user_attachments_for_email(db, email, prefetch=prefetch)
    )
    return files


def files_of_record(
    db: Session, email: Email, *, prefetch: PlanPrefetch | None = None
) -> list[PlannedFile]:
    """The files one outbound message carries — whichever question applies.

    Sent: what went, off the row. Anything else: what would go, off the
    resolvers. One function rather than one per screen, because the list a user
    reads and the endpoint that opens an entry of it address each other by
    position, and two lists built two ways is the drift bug in another costume.

    A failed send is not a record of anything, so it answers with the plan
    alongside drafts and queued mail — it carries what it would carry if it
    went, which is what a user looking at a retry wants to know.

    *prefetch* is passed straight down both branches. A caller listing a whole
    conversation — ``GET /inbox/threads/{id}``, which returns every message on
    it — asks this once per outbound message, and each ask read that message's
    hand-attached files on its own. See :class:`PlanPrefetch`: it changes how
    many queries run and never what is resolved.
    """
    if email.status == EmailStatus.SENT:
        return sent_files_for_email(db, email, prefetch=prefetch)
    return plan_for_email(db, email, prefetch=prefetch).files


def attachment_at(db: Session, email: Email, index: int) -> Attachment | None:
    """The file at *index* of everything queued to travel with *email*.

    Resolved *against the plan* rather than against a freshly built list of
    documents. Both lists are produced by walking the same three resolvers in
    the same order, so for a while it looked like either could answer this —
    but only one of them can fail. :func:`plan_for_email` names a resume it has
    not rendered; :func:`files_for_email` renders it, swallows the exception if
    the render dies, and returns a list one shorter. The user is looking at the
    plan's names, so a swallowed render moved every file after it up a place
    and pressing the resume opened the cover letter. Position is the plan's to
    assign; this only fetches the bytes for whatever the plan put there.

    Which plan depends on whether the message has gone. A draft is a promise
    and resolves live; a sent row is a record and resolves off itself, because
    re-resolving a delivered message answers with today's documents — see
    :func:`files_of_record`.

    The bytes are produced here rather than read back from storage because that
    is where they live: only tailored resumes are stored, and every other
    document is produced at send time. For a message that has already gone out,
    that makes this the document as it stands today — the same file unless the
    resume behind it has been edited since. The *position* is the row's either
    way, which is the part a user is pressing.

    Inbound mail is a different question with a different answer. Nothing is
    *queued* to travel with a message that has already arrived — what it
    carries is what the recruiter attached, and those bytes live in Gmail. The
    resolvers below would happily answer an inbound message with the
    candidate's own resume, which is why the direction is checked here rather
    than left to the caller.

    ``None`` when nothing is queued at that position, and also when the
    document queued there could not be produced. Raises
    :class:`AttachmentUnavailable` only on the inbound path, where a listed
    file failing to fetch is worth saying out loud; the outbound resolvers
    already swallow their own failures, and a preview must not be able to take
    the page down.
    """
    if index < 0:
        return None
    if email.direction == EmailDirection.RECEIVED:
        return inbound_attachment_at(db, email, index)
    files = files_of_record(db, email)
    if index >= len(files):
        return None
    return _planned_attachment(db, email, files[index])


@dataclass(frozen=True)
class OutboundFiles:
    """Everything that goes out with one email, and what to record about it."""

    attachments: list[Attachment] = field(default_factory=list)
    # The resume that travelled. Null on a sent row means the send genuinely
    # carried no resume — the distinction the pipeline had no way to express.
    resume_filename: str | None = None
    # The letter that travelled, or None. Written back over the composer's
    # intent so the row records what happened rather than what was planned.
    cover_letter_id: int | None = None


def files_for_email(db: Session, email: Email) -> OutboundFiles:
    """Resolve every attachment for one outbound email. Never raises.

    The resume leads: it is the document the recruiter opens first, and a client
    that shows only the first attachment should show that one. The user's own
    files come last, in the order they added them.

    A kind the user removed is not resolved at all — not resolved and dropped.
    Rendering a resume nobody is going to send is waste, and on the letter it
    would also be wrong: ``cover_letter_id`` is written back after a send to
    record what travelled, and resolving a suppressed letter would leave the row
    claiming one went.
    """
    suppressed = suppressed_kinds(email)

    resume = None if RESUME in suppressed else resume_attachment_for_email(db, email)
    if RESUME in suppressed:
        logger.info("email %s: resume removed by the user", email.id)
    elif resume is None:
        logger.warning("email %s is going out with no resume attached", email.id)

    letter: Attachment | None = None
    letter_id: int | None = None
    if COVER_LETTER not in suppressed:
        letter, letter_id = cover_letter_attachment_for_email(db, email)

    uploads = [
        Attachment(
            filename=row.filename,
            content=row.content,
            mime_type=row.content_type or media_type_for(row.filename),
        )
        for row in user_attachments_for_email(db, email)
        if row.content
    ]

    return OutboundFiles(
        attachments=[item for item in (resume, letter) if item is not None] + uploads,
        resume_filename=resume.filename if resume is not None else None,
        cover_letter_id=letter_id,
    )


__all__ = [
    "ATTACHMENT_CONTENT_SECURITY_POLICY",
    "COVER_LETTER",
    "OPAQUE_MEDIA_TYPE",
    "RESUME",
    "SUPPRESSIBLE",
    "UPLOAD",
    "AttachmentPlan",
    "AttachmentUnavailable",
    "OutboundFiles",
    "PlanPrefetch",
    "PlannedFile",
    "attachment_at",
    "attachment_content_disposition",
    "content_disposition_for",
    "served_media_type",
    "serving_headers",
    "prefetch_plans",
    "cover_letter_attachment_for_email",
    "filename_for",
    "files_for_email",
    "files_of_record",
    "inbound_attachment_at",
    "inbound_attachments_for",
    "inbound_filenames_for",
    "media_type_for",
    "plan_for_email",
    "resume_attachment_for_application",
    "resume_attachment_for_email",
    "resume_plan_for_email",
    "scanned_filenames_for",
    "sent_files_for_email",
    "suppressed_kinds",
    "user_attachments_for_email",
]
