"""Which of the candidate's resumes should answer *this* recruiter.

:mod:`app.services.email_attachments` resolves a resume by *intent*: the matched
profile's document, else the campaign's, else the default. That already does real
work — a candidate with a backend profile and a nursing profile gets the right
one — but it is a decision about which career the candidate is pursuing, not
about the role in front of them. Someone with three resumes under one profile
(``platform-eng.pdf``, ``ml-infra.pdf``, ``staff-generalist.pdf``) always gets
the same one, whatever the recruiter is actually hiring for.

This module scores every resume the user owns against the role the recruiter
described, using the same deterministic scorer the rest of the product uses, plus
a small filename/headline affinity bonus. The bonus is what reads the naming
convention candidates actually use, and it is capped at ten points precisely so
a filename can never outvote the content: a file called ``ml-resume.pdf`` that
lists no ML experience should not win an ML role.

**The margin.** A challenger has to beat the incumbent — whatever the ordinary
resolution would have picked — by :attr:`settings.recruiter_resume_switch_margin`
before it displaces it. Two resumes for the same person share most of their text,
so their scores cluster within a couple of points, and a 0.4-point difference is
noise rather than a reason to send a different document than the user's own
settings imply.

**What this deliberately does not do.** It never picks a
:class:`~app.models.tailored_resume.TailoredResume` rendered for a different
posting. Those PDFs carry the posting's company in the header, and
:mod:`email_attachments` states the rule plainly: sending Acme's resume to
Initech is worse than sending a generic one. A tailoring run is still used for
the posting it was tailored to — that step is untouched and still runs first.
So "pick the best-matching resume" is answered from the candidate's base
documents, which is the version of the idea that cannot put the wrong company's
name on the page a recruiter opens.

Pure apart from reading the user's resumes: a session and a parsed job in, a
choice out. Nothing here renders, attaches or sends.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

# Aliased: this module's own public entry point is `select`, and the two would
# shadow each other.
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.resume import Resume
from app.models.user import User
from app.services import fit_scorer
from app.services.fit_scorer import Targeting
from app.services.jd_parser import ParsedJob
from app.services.places import fold_diacritics

logger = logging.getLogger(__name__)

#: Ceiling on the filename/headline bonus, in the scorer's own 0-100 points.
#: Low on purpose — see the module docstring.
MAX_AFFINITY_BONUS = 10.0

#: Words that appear in almost every resume filename and identify nothing.
_STOPWORDS = frozenset(
    {
        "resume",
        "cv",
        "curriculum",
        "vitae",
        "final",
        "latest",
        "new",
        "updated",
        "copy",
        "draft",
        "v1",
        "v2",
        "v3",
        "pdf",
        "docx",
        "doc",
        "the",
        "and",
        "for",
        "with",
        "2024",
        "2025",
        "2026",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9+#]+")


def _tokens(text: str | None) -> set[str]:
    """Meaningful lowercase tokens from a filename, headline or role list.

    Accents are folded *before* the split, because `_TOKEN_RE` is an ASCII
    character class and an accented letter is not in it: it is a token
    boundary. "Développeur" came out of here as ``{"veloppeur"}`` — the "d" is
    one character and dropped as noise — and "Ingénieur Sécurité" as
    ``{"ing", "nieur", "curit"}``.

    Shredding both sides the same way would at least have been consistent, and
    the two sides are not written by the same hand. A filename is ASCII far
    more often than not, because that is what a browser's download folder and a
    mail client encourage; the posting's title is written properly. So
    ``developpeur-backend.pdf`` against "Développeur Backend" overlapped on
    "backend" alone — half the label matched instead of all of it — and
    ``cv-securite-cloud.pdf`` against "Ingénieur Sécurité Cloud" on "cloud"
    alone.

    :func:`affinity_bonus` is proportional to that share, so the bonus came out
    at half or a third of what the resume had earned, on every candidate whose
    working language is not English. Ten points is small by design, but the
    switch margin it is weighed against is smaller still — a bonus that lands
    at 3.3 instead of 10 is the difference between sending the resume the
    candidate named for this role and sending whichever one their settings
    default to.

    Same helper, same bug, as :func:`app.services.fit_scorer.normalize_text`
    and :func:`app.services.places.place_tokens`: fold first, then split.
    """
    return {
        token
        for token in _TOKEN_RE.findall(fold_diacritics((text or "").lower()))
        if len(token) > 1 and token not in _STOPWORDS
    }


def _label_tokens(resume: Resume) -> set[str]:
    """Everything about a resume that names what it is *for*.

    The filename is the signal the ask names, and it is the one candidates
    actually curate — but a resume uploaded as ``resume-final-2.pdf`` names
    nothing, so the headline and target roles carry the same weight. All three
    are the candidate's own labelling of the document, as against its contents.
    """
    tokens = _tokens(resume.filename)
    tokens |= _tokens(resume.headline)
    for role in (resume.target_roles or [])[:8]:
        tokens |= _tokens(role)
    return tokens


def _job_tokens(job: ParsedJob) -> set[str]:
    tokens = _tokens(job.title)
    for skill in (job.required_skills or [])[:20]:
        tokens |= _tokens(skill)
    for skill in (job.preferred_skills or [])[:10]:
        tokens |= _tokens(skill)
    if job.seniority:
        tokens |= _tokens(job.seniority)
    return tokens


def affinity_bonus(resume: Resume, job: ParsedJob) -> float:
    """0..:data:`MAX_AFFINITY_BONUS` for how well a resume's *label* fits a role.

    Proportional to the share of the document's own label words the role uses,
    so a narrowly-named resume that matches gets the full bonus and a broadly
    named one gets little — which is the right way round: ``ml-infra`` matching
    an ML infrastructure role is strong evidence, ``senior-engineer`` matching a
    senior engineering role is almost none.
    """
    labels = _label_tokens(resume)
    if not labels:
        return 0.0
    overlap = labels & _job_tokens(job)
    if not overlap:
        return 0.0
    return round(MAX_AFFINITY_BONUS * (len(overlap) / len(labels)), 2)


@dataclass(frozen=True)
class ResumeChoice:
    """The document that won, what it scored, and why — in the UI's words."""

    resume: Resume
    score: float
    reason: str
    # True when this displaced what the ordinary resolution would have sent.
    switched: bool = False

    @property
    def resume_id(self) -> int:
        return self.resume.id


@dataclass(frozen=True)
class _Scored:
    resume: Resume
    fit: float
    bonus: float

    @property
    def total(self) -> float:
        return round(self.fit + self.bonus, 2)


def _score_all(
    db: Session, user: User, job: ParsedJob, targeting: Targeting | None
) -> list[_Scored]:
    resumes = list(
        db.scalars(
            sa_select(Resume)
            .where(Resume.user_id == user.id)
            .order_by(Resume.is_default.desc(), Resume.id.desc())
        )
    )
    scored: list[_Scored] = []
    for resume in resumes:
        try:
            fit = fit_scorer.score_fit(resume, job, targeting=targeting, explain=False)
        except Exception:  # noqa: BLE001 - one unscoreable resume must not lose the rest
            logger.warning("resume %s could not be scored", resume.id, exc_info=True)
            continue
        scored.append(
            _Scored(resume=resume, fit=fit.overall, bonus=affinity_bonus(resume, job))
        )
    return scored


def select(
    db: Session,
    user: User,
    job: ParsedJob,
    *,
    incumbent_id: int | None = None,
    targeting: Targeting | None = None,
) -> ResumeChoice | None:
    """The resume to send for *job*, or ``None`` when there is nothing to choose.

    *incumbent_id* is the resume the ordinary resolution would have picked — the
    matched profile's document. The winner must beat it by the configured margin
    to displace it, so this can only ever *improve* on that answer or leave it
    alone.

    ``None`` comes back when the user has no resumes, when the feature is off, or
    when there is only one document — in every one of those cases the existing
    resolver already has the only possible answer and running a scorer to confirm
    it would be waste.
    """
    if not settings.recruiter_smart_resume_enabled:
        return None

    scored = _score_all(db, user, job, targeting)
    if len(scored) < 2:
        return None

    scored.sort(key=lambda s: (s.total, s.resume.is_default, s.resume.id), reverse=True)
    winner = scored[0]

    incumbent = next((s for s in scored if s.resume.id == incumbent_id), None)
    if incumbent is None:
        # Nothing to beat — the caller had no opinion, so the best score wins
        # outright. Still reported as a switch: the choice was made here.
        return ResumeChoice(
            resume=winner.resume,
            score=winner.total,
            reason=(
                f"Matched this role at {winner.total:.0f} — the strongest of your "
                f"{len(scored)} resumes."
            ),
            switched=True,
        )

    if winner.resume.id == incumbent.resume.id:
        return ResumeChoice(
            resume=incumbent.resume,
            score=incumbent.total,
            reason=f"Matched this role at {incumbent.total:.0f} — your best fit.",
        )

    margin = winner.total - incumbent.total
    if margin < settings.recruiter_resume_switch_margin:
        # Close enough to be noise. The user's own profile setting breaks the tie.
        return ResumeChoice(
            resume=incumbent.resume,
            score=incumbent.total,
            reason=(
                f"Matched this role at {incumbent.total:.0f}; "
                f"{winner.resume.display_label} scored {winner.total:.0f}, too close "
                "to switch."
            ),
        )

    return ResumeChoice(
        resume=winner.resume,
        score=winner.total,
        reason=(
            f"Matched this role at {winner.total:.0f}, against "
            f"{incumbent.total:.0f} for {incumbent.resume.display_label}."
        ),
        switched=True,
    )


__all__ = [
    "MAX_AFFINITY_BONUS",
    "ResumeChoice",
    "affinity_bonus",
    "select",
]
