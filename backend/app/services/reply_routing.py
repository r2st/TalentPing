"""The three-band decision: reply automatically, draft for review, or flag.

One pure function with no I/O, because this is the part of inbound handling that
decides whether a stranger receives an email in the candidate's name. Keeping it
free of the database, Gmail and the model chain means every branch is testable
in a line, and means the whole auto-reply capability can be removed by deleting
one enum member rather than by unpicking a pipeline.

**The two signals answer two different questions, and only one of them decides
whether we write.** The classifier says how sure it is the message is a recruiter
(0..1). The deterministic scorer says how well the opportunity fits the best
profile (0..100). They are *not* interchangeable:

* Whether a reply exists at all is a question about the **sender**. A real person
  wrote to this candidate about a job. They get an answer, and a mediocre fit is
  a thing to say in the answer ("what's the comp band?"), not a reason for
  silence.
* Whether that reply may go out **unread by the candidate** is a question about
  both. For that, and only that, the two are multiplied::

      confidence = 100 * classification_confidence * (match_score / 100)

  Averaging would be wrong in the case that matters: 95% sure of a 40 fit
  averages to 67.5, a hair under a 70 bar, for what is really a confident reading
  of a bad job. Multiplying gives 38. Both signals have to be strong before the
  product speaks without being read, which is exactly the property an unreviewed
  send needs.

Folding those two questions into one number is the bug this module shipped with.
The product multiplied, compared the result against a 70 "draft" bar, and flagged
everything underneath — so a confidently-identified recruiter offering a 60-point
role got silence. In production it was worse than that: with every LLM provider
rate-limited, the classifier fell back to rules capped at 0.6 confidence, and
``0.6 * 100 = 60`` is below 70 **at any match score at all**. Drafting was
arithmetically impossible and every message in the mailbox was flagged.

So the draft floor now defaults to zero: an actionable message from a real person
is always answered. ``FLAG`` is reserved for the two states where there is
genuinely nothing to write — a message we could not classify, and a candidate
with no profile to write from.

**Nothing degrades silently.** Every path that declines to auto-reply falls to
``DRAFT``, and only a message with nothing to say falls to ``FLAG``. There is no
branch that quietly does nothing, because "we saw a recruiter email and said
nothing" is the failure this feature exists to fix.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings
from app.models.recruiter_email import (
    ACTIONABLE_KINDS,
    RecruiterEmailKind,
    ReplyRoute,
)


@dataclass(frozen=True)
class RouteDecision:
    """Which band fired, the number it fired on, and why — in the user's words.

    ``route`` is ``None`` only for mail that is not an opportunity at all (a job
    alert, an ATS acknowledgement). Those are recorded and never routed; there is
    nothing to reply to and nothing to flag.
    """

    route: ReplyRoute | None
    confidence: float
    reason: str

    @property
    def replies(self) -> bool:
        """True when a reply should be generated at all."""
        return self.route in (ReplyRoute.AUTO, ReplyRoute.DRAFT)


def combine(classification_confidence: float, match_score: float) -> float:
    """The 0-100 number the thresholds compare against. See the module docstring."""
    classification = max(0.0, min(1.0, float(classification_confidence or 0.0)))
    match = max(0.0, min(100.0, float(match_score or 0.0)))
    return round(classification * match, 2)


def decide(
    kind: RecruiterEmailKind,
    classification_confidence: float,
    match_score: float | None,
    *,
    has_profile: bool,
    location_ok: bool = True,
    auto_enabled: bool = False,
    profile_label: str | None = None,
) -> RouteDecision:
    """Route one classified, matched message.

    *auto_enabled* is the caller's already-resolved answer to "may this user have
    unreviewed replies sent right now?" — the server switch, the user's switch and
    the daily cap folded into one boolean. This function does not read them
    itself, so the policy stays where it can see the database and the decision
    stays pure.
    """
    label = profile_label or "your profile"

    # Not an opportunity. Recorded so the counts are honest, never routed.
    if kind not in ACTIONABLE_KINDS:
        if kind is RecruiterEmailKind.UNKNOWN:
            # If the email matched a profile well enough, draft a template
            # reply instead of flagging silently.  A floor of 60 keeps bank
            # alerts, academic papers and event digests from getting replies
            # while letting genuine recruiter mail through.
            _UNKNOWN_MATCH_FLOOR = 60.0
            if (
                has_profile
                and match_score is not None
                and match_score >= _UNKNOWN_MATCH_FLOOR
            ):
                confidence = combine(classification_confidence, match_score)
                return RouteDecision(
                    ReplyRoute.DRAFT,
                    confidence,
                    f"Couldn't classify confidently, but matched {label} at "
                    f"{match_score:.0f} — drafted a template for your review.",
                )
            return RouteDecision(
                ReplyRoute.FLAG,
                0.0,
                "Couldn't classify this one confidently — worth a look.",
            )
        return RouteDecision(None, 0.0, _NOT_OPPORTUNITY[kind])

    if not has_profile or match_score is None:
        return RouteDecision(
            ReplyRoute.FLAG,
            0.0,
            "No active profile fits this role, so nothing was drafted.",
        )

    confidence = combine(classification_confidence, match_score)

    # The one remaining way an identified recruiter gets no reply, and it is off
    # by default. A deployment that would rather stay quiet below some fit can set
    # the floor above zero and get the old behaviour back.
    if confidence < settings.recruiter_reply_draft_threshold:
        return RouteDecision(
            ReplyRoute.FLAG,
            confidence,
            f"Matched {label} at {match_score:.0f}, which isn't a strong enough "
            "fit to answer for you.",
        )

    if confidence < settings.recruiter_reply_auto_threshold:
        return RouteDecision(
            ReplyRoute.DRAFT,
            confidence,
            f"Matched {label} at {match_score:.0f} — drafted for your review.",
        )

    # Above the auto bar. Two gates can still hold it back, and both fall to a
    # draft rather than to nothing.
    if not auto_enabled:
        return RouteDecision(
            ReplyRoute.DRAFT,
            confidence,
            f"Matched {label} at {match_score:.0f} — drafted for your review "
            "(automatic replies are off).",
        )
    if not location_ok:
        return RouteDecision(
            ReplyRoute.DRAFT,
            confidence,
            f"Matched {label} at {match_score:.0f}, but the location isn't one "
            "you listed — drafted rather than sent.",
        )

    return RouteDecision(
        ReplyRoute.AUTO,
        confidence,
        f"Matched {label} at {match_score:.0f} — replied automatically.",
    )


_NOT_OPPORTUNITY: dict[RecruiterEmailKind, str] = {
    RecruiterEmailKind.JOB_ALERT: "A job-board digest, not a person.",
    RecruiterEmailKind.ATS_AUTOMATED: "An automated application acknowledgement.",
    RecruiterEmailKind.NOT_RECRUITER: "Not about a job opportunity.",
}


__all__ = ["RouteDecision", "combine", "decide"]
