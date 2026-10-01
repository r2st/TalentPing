"""Render the documents an application travels with as PDFs.

Markdown is fine for previewing in the browser and useless for applying: what
gets attached to an application is a PDF, and what reads it first is an ATS
parser. So the layout here is deliberately plain — a single column, real text
(never outlines or images), standard fonts, no tables and no multi-column
tricks. Every one of those is a well-known way to make a resume unparseable,
and a beautiful CV that an ATS reads as empty is worse than a dull one it reads
correctly.

The cover letter (:func:`render_letter_pdf`) lives here rather than beside the
letter service for one reason: reportlab is an optional dependency, and one
module owning the import guard means one place where "no PDF today" is decided.
It reuses the resume's header so the two documents that arrive together look
like they came from the same person.

The bytes are stored on the row rather than re-rendered on each download, so
what the candidate sends is byte-for-byte what was reviewed. A re-render months
later would pick up whatever the template looks like then.

Import-safe without reportlab: :func:`is_available` reports it and callers fall
back to the Markdown download, exactly as they did before PDFs existed.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Any

from app.services.places import fold_diacritics

logger = logging.getLogger(__name__)

try:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable,
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    _REPORTLAB_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in slim envs
    _REPORTLAB_AVAILABLE = False


class PDFNotAvailable(RuntimeError):
    """reportlab isn't installed — callers should offer Markdown instead."""


def is_available() -> bool:
    return _REPORTLAB_AVAILABLE


def _escape(value: Any) -> str:
    """Text as reportlab's mini-markup wants it.

    Everything written into a Paragraph goes through here: an ampersand or an
    angle bracket in a company name would otherwise be parsed as markup and
    either vanish or raise.
    """
    text = "" if value is None else str(value)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Collapse whitespace; a bullet that wrapped in the source shouldn't keep
    # its newline here.
    return re.sub(r"\s+", " ", text).strip()


def _styles() -> dict[str, Any]:
    base = getSampleStyleSheet()
    return {
        "name": ParagraphStyle(
            "Name",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            spaceAfter=2,
            alignment=TA_LEFT,
        ),
        "headline": ParagraphStyle(
            "Headline",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=11,
            leading=14,
            textColor="#444444",
            spaceAfter=2,
        ),
        "contact": ParagraphStyle(
            "Contact",
            parent=base["Normal"],
            fontSize=9,
            leading=12,
            textColor="#555555",
            spaceAfter=8,
        ),
        "section": ParagraphStyle(
            "Section",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            spaceBefore=10,
            spaceAfter=4,
            textColor="#111111",
        ),
        "role": ParagraphStyle(
            "Role",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            spaceAfter=1,
        ),
        "meta": ParagraphStyle(
            "Meta",
            parent=base["Normal"],
            fontSize=9,
            leading=12,
            textColor="#666666",
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "Body", parent=base["Normal"], fontSize=9.5, leading=13, spaceAfter=4
        ),
        "bullet": ParagraphStyle(
            "Bullet", parent=base["Normal"], fontSize=9.5, leading=13, spaceAfter=2
        ),
    }


def render_pdf(resume, tailored) -> bytes:
    """The tailored resume as PDF bytes.

    Takes the persisted :class:`~app.models.tailored_resume.TailoredResume` (or
    anything with the same attributes) plus the base resume it derives from —
    contact details live on the base row and are never rewritten by tailoring.
    """
    if not _REPORTLAB_AVAILABLE:
        raise PDFNotAvailable("reportlab is not installed")

    style = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title=f"{resume.full_name or 'Resume'} — {tailored.job_title or ''}".strip(" —"),
        author=resume.full_name or "",
    )

    story: list[Any] = [
        Paragraph(_escape(resume.full_name or "Candidate"), style["name"])
    ]

    headline = tailored.job_title or resume.headline
    if headline:
        story.append(Paragraph(_escape(headline), style["headline"]))

    contact_parts = [
        resume.email,
        resume.phone,
        resume.location,
        *(resume.links or [])[:2],
    ]
    contact = " · ".join(_escape(p) for p in contact_parts if p)
    if contact:
        story.append(Paragraph(contact, style["contact"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color="#cccccc"))

    if tailored.tailored_summary:
        story.append(Paragraph("Summary", style["section"]))
        story.append(Paragraph(_escape(tailored.tailored_summary), style["body"]))

    if tailored.ordered_skills:
        story.append(Paragraph("Skills", style["section"]))
        story.append(
            Paragraph(", ".join(_escape(s) for s in tailored.ordered_skills), style["body"])
        )

    experience = tailored.highlighted_experience or []
    if experience:
        story.append(Paragraph("Experience", style["section"]))
        for entry in experience:
            header = " — ".join(
                _escape(p) for p in (entry.get("title"), entry.get("company")) if p
            )
            if header:
                story.append(Paragraph(header, style["role"]))

            period = " – ".join(
                _escape(p) for p in (entry.get("start"), entry.get("end")) if p
            )
            if period:
                story.append(Paragraph(period, style["meta"]))

            # The re-angled bullets are the point of tailoring, so they lead.
            # A role with none falls back to the computed rationale, which is
            # still specific to this posting.
            bullets = entry.get("bullets") or entry.get("original_bullets") or []
            if bullets:
                story.append(
                    ListFlowable(
                        [
                            ListItem(
                                Paragraph(_escape(b), style["bullet"]), leftIndent=12
                            )
                            for b in bullets
                        ],
                        bulletType="bullet",
                        start="•",
                        leftIndent=12,
                    )
                )
            elif entry.get("why_relevant"):
                story.append(Paragraph(_escape(entry["why_relevant"]), style["body"]))
            story.append(Spacer(1, 4))

    if resume.education:
        story.append(Paragraph("Education", style["section"]))
        for entry in resume.education:
            line = ", ".join(
                _escape(p)
                for p in (
                    entry.get("degree"),
                    entry.get("field"),
                    entry.get("school"),
                    entry.get("year"),
                )
                if p
            )
            if line:
                story.append(Paragraph(line, style["body"]))

    doc.build(story)
    return buffer.getvalue()


def filename_stem(*parts: Any) -> str:
    """A hyphenated ASCII stem from *parts*, keeping accented letters as letters.

    The strip is `[^A-Za-z0-9]+` and an accented letter is not in that class, so
    without the fold it is punctuation: "José Muñoz" left here as
    ``jos-mu-oz``, "François Lefèvre" as ``fran-ois-lef-vre``, and "Łukasz
    Żółć" as ``ukasz`` — the surname gone entirely, the given name missing its
    first letter. The stem is the candidate's own name, on the document a
    recruiter downloads and keeps; a name mangled into initials and gaps is
    read as a broken export, and it is the applicant it reflects on.

    An ASCII stem is still the thing to send — a filename crosses MIME headers,
    ATS uploads and filesystems that each disagree about Unicode — so the fold
    onto ASCII stays. It is *where* it happens that was wrong: fold first, then
    strip, exactly as in :func:`app.services.places.place_tokens` and
    :func:`app.services.resume_selector._tokens`.

    A name in a script with no ASCII spelling at all (Cyrillic, CJK) still
    folds away to nothing, and the caller's ``or "resume"`` fallback is the
    honest answer there: a generic name rather than a wrong one.
    """
    joined = " ".join(str(part) for part in parts if part)
    return re.sub(r"[^A-Za-z0-9]+", "-", fold_diacritics(joined)).strip("-").lower()


#: How long the stem may be before it is cut. ``tailored_resumes.pdf_filename``
#: is ``String(255)``, and the two fields this name is built from are
#: ``String(200)`` and ``String(500)`` — so a posting with a long title, which
#: is an ordinary thing for a scraped posting to have, produced a name Postgres
#: refused outright. Well under the column either way: nothing is served by a
#: 200-character download name, and a recruiter reads the first few words.
_MAX_STEM_CHARS = 120


def filename_for(resume, tailored) -> str:
    """A download name an ATS and a human both read cleanly.

    Bounded, because neither half of it is a length this code chooses: the
    candidate's name comes out of a parsed PDF and the company and title come
    off a scraped posting. Cut on a hyphen where there is one within reach, so
    the name ends at a word rather than mid-syllable.
    """
    stem = filename_stem(
        resume.full_name or "resume", tailored.job_company or tailored.job_title or ""
    )
    if len(stem) > _MAX_STEM_CHARS:
        stem = stem[:_MAX_STEM_CHARS]
        cut = stem.rfind("-")
        # Only when the last word boundary leaves most of the name intact. A
        # single unbroken 200-character token has no boundary worth honouring.
        if cut > _MAX_STEM_CHARS // 2:
            stem = stem[:cut]
        stem = stem.strip("-")
    return f"{stem or 'resume'}.pdf"


def render_letter_pdf(letter, resume=None) -> bytes:
    """One cover letter as PDF bytes.

    Takes the persisted :class:`~app.models.cover_letter.CoverLetter` and,
    optionally, the resume it was written from — the candidate's contact details
    live there, and a letter that arrives without them makes the recruiter go
    back to the resume to find out how to reply.

    The letter's own paragraphs are printed verbatim, one Paragraph per blank-line
    block, so the prose the user reviewed in the UI is the prose that goes out.
    """
    if not _REPORTLAB_AVAILABLE:
        raise PDFNotAvailable("reportlab is not installed")

    style = _styles()
    buffer = io.BytesIO()
    name = (resume.full_name if resume else None) or "Cover letter"
    heading = " — ".join(p for p in (letter.job_title, letter.job_company) if p)
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.8 * inch,
        bottomMargin=0.8 * inch,
        title=f"Cover letter{f' — {heading}' if heading else ''}",
        author=(resume.full_name if resume else None) or "",
    )

    story: list[Any] = [Paragraph(_escape(name), style["name"])]

    if resume is not None:
        contact = " · ".join(
            _escape(p)
            for p in (resume.email, resume.phone, resume.location, *(resume.links or [])[:2])
            if p
        )
        if contact:
            story.append(Paragraph(contact, style["contact"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color="#cccccc"))
    story.append(Spacer(1, 12))

    # Greeting, body, sign-off — each blank-line block is its own paragraph. The
    # sign-off keeps its line breaks ("Best regards,\nJordan"), which a plain
    # Paragraph would otherwise collapse into one line.
    if letter.greeting:
        story.append(Paragraph(_escape(letter.greeting), style["body"]))
        story.append(Spacer(1, 8))

    for block in re.split(r"\n\s*\n", (letter.body or "").strip()):
        if block.strip():
            story.append(Paragraph(_escape(block), style["body"]))
            story.append(Spacer(1, 6))

    if letter.sign_off:
        sign_off = "<br/>".join(
            _escape(line) for line in letter.sign_off.splitlines() if line.strip()
        )
        story.append(Spacer(1, 6))
        story.append(Paragraph(sign_off, style["body"]))

    doc.build(story)
    return buffer.getvalue()


def letter_filename_for(letter, resume=None) -> str:
    """A download name that says whose letter it is and who it's for."""
    stem = filename_stem(
        (resume.full_name if resume else None) or "",
        letter.job_company or letter.job_title or "",
        "cover letter",
    )
    return f"{stem or 'cover-letter'}.pdf"


__all__ = [
    "PDFNotAvailable",
    "filename_for",
    "filename_stem",
    "is_available",
    "letter_filename_for",
    "render_letter_pdf",
    "render_pdf",
]
