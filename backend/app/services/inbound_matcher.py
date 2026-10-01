"""Which of the candidate's profiles does this inbound opportunity belong to?

A candidate holding "Backend Engineer, remote or Berlin" and "Tech Lead, Berlin
only" has two different answers to a recruiter's email, argued with two different
resumes. Picking the wrong one produces a reply that reads as a form letter,
which is precisely the failure the multi-profile work existed to fix.

The pick is **deterministic**, and reuses the shipped scorer rather than asking a
model. That is the same choice :mod:`app.services.fit_scorer` documents for job
postings and it holds for the same reasons: the number is comparable with every
other fit score in the product, it aggregates into the dashboard, and "why did
this drop nine points?" has an answer that isn't "the model felt different". The
model's contribution upstream is *reading* the email; choosing what to do about
it is arithmetic.

The route from message to score is short:

    classification.as_job_text() -> jd_parser.parse_job(use_llm=False)
                                 -> profile_service.score_against_targets()

``use_llm=False`` because the classifier already extracted the title, company,
location and salary in the same call that classified the message. Paying a second
model call to re-read text we have already parsed is the cost this pipeline is
most able to avoid.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User
from app.services import fit_scorer, profile_service
from app.services.jd_parser import ParsedJob, parse_job
from app.services.profile_service import ScoringTarget
from app.services.recruiter_classifier import Classification

logger = logging.getLogger(__name__)


@dataclass
class InboundMatch:
    """The winning profile for one inbound message, and why it won."""

    target: ScoringTarget
    parsed: ParsedJob
    score: float
    reason: str
    # None when the location gate would veto an automatic reply. The message can
    # still be drafted or flagged — see reply_routing.decide.
    location_ok: bool = True
    location_note: str | None = None

    @property
    def profile_id(self) -> int | None:
        return self.target.profile_id

    @property
    def label(self) -> str:
        return self.target.label


def _is_remote(parsed: ParsedJob, classification: Classification) -> bool:
    """Whether the opportunity is remote, from either source that could know."""
    if classification.remote is True:
        return True
    return bool(parsed.remote)


def location_gate(
    target: ScoringTarget, parsed: ParsedJob, classification: Classification
) -> str | None:
    """Veto an *automatic* reply about somewhere the candidate won't work.

    The :func:`app.services.auto_apply_service.location_gate` rules, applied to a
    parsed email instead of a stored posting: remote passes unconditionally,
    remote-only means remote-only, a stated location list is binding rather than
    merely weighted, and a candidate who named no locations has expressed no
    constraint to enforce.

    One rule is deliberately softer here. Outbound auto-apply refuses a posting
    that won't say where it is; a recruiter's first email very often doesn't say,
    and refusing on that basis would flag nearly everything. An unstated location
    is allowed through the gate — the reply asks about it, and the confidence band
    still has to be cleared.

    Returns a reason to hold back, or ``None`` to allow.
    """
    if not settings.autopilot_enforce_locations:
        return None
    if _is_remote(parsed, classification):
        return None

    targeting = target.targeting
    where = (classification.location or parsed.location or "").strip()

    if targeting.remote_only:
        return (
            f"you're only looking for remote work; '{where}' is on-site"
            if where
            else "you're only looking for remote work and this isn't marked remote"
        )

    wanted = [p for p in (targeting.locations or []) if isinstance(p, str) and p.strip()]
    if not wanted or not where:
        return None

    if fit_scorer.location_match(where, wanted) is not None:
        return None
    return f"'{where}' is not one of your locations ({', '.join(wanted[:3])})"


def match(
    db: Session,
    user: User,
    classification: Classification,
    *,
    body: str,
    subject: str | None = None,
) -> InboundMatch | None:
    """Score an inbound opportunity against every active profile; return the best.

    ``None`` when the user has nothing to score against at all — no profiles and
    no default resume. That is a real state (a brand-new account) and the caller
    turns it into a flag, not an error.
    """
    targets = profile_service.active_targets(db, user)
    if not targets:
        logger.debug("user %s has no scoring targets for inbound mail", user.id)
        return None

    parsed = parse_job(
        classification.as_job_text(body, subject),
        page_title=classification.role_title,
        use_llm=False,
    )

    best = profile_service.score_against_targets(targets, parsed, explain=True)
    if best is None:
        return None

    veto = location_gate(best.target, parsed, classification)
    reason = best.fit.summary or best.fit.recommendation
    return InboundMatch(
        target=best.target,
        parsed=parsed,
        score=round(best.fit.overall, 2),
        reason=reason,
        location_ok=veto is None,
        location_note=veto,
    )


__all__ = ["InboundMatch", "location_gate", "match"]
