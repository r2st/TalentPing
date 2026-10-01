"""Does the fit score predict a reply? — the report that grades the scorer.

Everything else in the product treats :mod:`app.services.fit_scorer`'s number as
true: the feed sorts by it, ``min_fit_score`` gates on it, and autopilot refuses
to email below it. Nothing has ever checked it against an outcome. This module
does exactly that and nothing else — bucket every send by the score it went out
with, then report the reply and interview rate per band.

Three decisions shape the numbers:

* **Sends, not applications.** A queued application that never left the outbox
  has no outcome to attribute, so it is excluded from both sides of the ratio.
  The denominator is applications with at least one delivered outbound email.
* **Unscored sends are reported, never dropped.** Outreach against a company
  rather than a posting carries no fit score at all. Hiding those would make the
  report look more complete than it is, so ``unscored_sends`` is part of the
  payload and the UI shows it.
* **A flat result is a result.** The honest failure mode of this report is that
  the bands do not separate — that high-scoring sends reply at the same rate as
  low-scoring ones. That finding is more valuable than any band table, so it is
  stated as a ``verdict`` rather than left for the reader to infer from six
  similar-looking percentages.

The verdict rests on the point-biserial correlation between the raw score and
the binary "did they write back", computed over individual sends rather than
over band averages. Band averages would let one thin band swing the conclusion;
the per-send version weights every send equally, which is what "is this score
predictive" actually asks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.application import (
    ENGAGED_STATUSES,
    INTERVIEWING_STATUSES,
    Application,
)
from app.models.autopilot import AutopilotPreference
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.fit_score import FitScore
from app.models.job import JobPosting

# (lower inclusive, upper exclusive, label). The top band closes at 101 so a
# perfect 100 lands somewhere. Sub-50 is one wide bucket on purpose: almost
# nothing is ever sent from down there, and splitting it would produce four
# empty rows that make the table look emptier than the data is.
BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 50, "Below 50"),
    (50, 60, "50–59"),
    (60, 70, "60–69"),
    (70, 80, "70–79"),
    (80, 90, "80–89"),
    (90, 101, "90–100"),
)

# Below this many sends a band's percentage is noise, exactly as in the ranked
# lists in routers/analytics. The band is still shown — flagged, not hidden.
MIN_BAND_SENDS = 3

# Below this many scored sends in total there is no verdict to give. Twenty is
# the point at which a correlation over a binary outcome stops being dominated
# by whichever way one or two replies happened to fall.
MIN_TOTAL_SENDS = 20

# A recommended threshold has to be backed by its own sample, not merely by the
# total. Eight sends at or above the line is the floor for suggesting the user
# move their gate there.
MIN_THRESHOLD_SENDS = 8

# How much better than the account's own baseline a band has to reply before it
# is worth telling someone to raise their floor. A 5% improvement is inside the
# noise of any sample this product will have for a year.
LIFT_REQUIRED = 1.25

# Correlations inside this band are read as "no relationship". Deliberately
# generous: with a few dozen sends an |r| of 0.1 is not evidence of anything,
# and the cost of calling a working scorer flat is far lower than the cost of
# telling someone a coin flip is predictive.
FLAT_CORRELATION = 0.10

VERDICT_SEPARATING = "separating"
VERDICT_FLAT = "flat"
VERDICT_INVERTED = "inverted"
VERDICT_INSUFFICIENT = "insufficient_data"


@dataclass
class Band:
    """One score range, and how the outreach sent from it fared."""

    label: str
    lower: int
    upper: int  # inclusive, for display — BANDS holds the exclusive bound
    sends: int = 0
    responses: int = 0
    interviews: int = 0

    @property
    def response_rate(self) -> float:
        return round(self.responses / self.sends, 3) if self.sends else 0.0

    @property
    def interview_rate(self) -> float:
        return round(self.interviews / self.sends, 3) if self.sends else 0.0

    @property
    def reliable(self) -> bool:
        return self.sends >= MIN_BAND_SENDS


@dataclass
class Calibration:
    """The whole report. Safe to render with zero sends — every field is set."""

    bands: list[Band] = field(default_factory=list)
    scored_sends: int = 0
    unscored_sends: int = 0
    responses: int = 0
    interviews: int = 0
    response_rate: float = 0.0
    correlation: float | None = None
    verdict: str = VERDICT_INSUFFICIENT
    summary: str = ""
    suggested_min_fit_score: int | None = None
    # What the user's autopilot gate is set to today, so the UI can show the
    # suggestion against the status quo instead of in a vacuum.
    current_min_fit_score: int | None = None
    min_band_sends: int = MIN_BAND_SENDS
    min_total_sends: int = MIN_TOTAL_SENDS


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation of *xs* with *ys*, or None when either has no spread.

    ``ys`` is 0/1 here, which makes this the point-biserial coefficient. Written
    out rather than pulled from scipy because it is six lines and scipy is not a
    dependency of this project.

    None is returned rather than 0.0 for the degenerate cases — every send
    scoring the same, or every send replying — because "no variation to measure"
    and "measured, found nothing" are different answers and the caller reports
    them differently.
    """
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    numerator = sum(a * b for a, b in zip(dx, dy, strict=True))
    denominator = (sum(a * a for a in dx) * sum(b * b for b in dy)) ** 0.5
    if denominator == 0:
        return None
    return round(numerator / denominator, 3)


def _band_for(score: float) -> tuple[int, int, str]:
    for lower, upper, label in BANDS:
        if lower <= score < upper:
            return lower, upper, label
    # Outside every band — a scorer change, a hand-written row, a migration that
    # wrote a sentinel. It belongs at the near end rather than nowhere, and
    # which end matters: falling through to ``BANDS[-1]`` unconditionally filed
    # a *negative* score in "90–100", so the one row that most needs explaining
    # would arrive counted as the account's best outreach and drag the top
    # band's reply rate — the number the verdict and the threshold advice are
    # both computed from — toward whatever that row did.
    return BANDS[-1] if score >= BANDS[-1][0] else BANDS[0]


def _scores_by_posting(db: Session, user_id: int, posting_ids: set[int]) -> dict[int, float]:
    """The fit score each posting was judged on, keyed by posting id.

    ``FitScore.overall`` is preferred over the ``JobPosting.fit_score`` cache
    because it is the row the UI showed the user and it carries the profile it
    was computed for. The cache is the fallback for postings scored before the
    breakdown was persisted, which is the only way this can come back empty for
    a posting that autopilot demonstrably gated on.

    When a posting has several scores — one per profile that looked at it — the
    highest wins. That is the one that would have cleared ``min_fit_score`` and
    so the one the send actually happened on.
    """
    if not posting_ids:
        return {}

    scores: dict[int, float] = {}
    for posting_id, overall in db.execute(
        select(FitScore.job_posting_id, FitScore.overall).where(
            FitScore.user_id == user_id,
            FitScore.job_posting_id.in_(posting_ids),
        )
    ):
        if posting_id is None:
            continue
        current = scores.get(posting_id)
        if current is None or overall > current:
            scores[posting_id] = float(overall)

    missing = posting_ids - scores.keys()
    if missing:
        for posting_id, cached in db.execute(
            select(JobPosting.id, JobPosting.fit_score).where(
                JobPosting.user_id == user_id,
                JobPosting.id.in_(missing),
                JobPosting.fit_score.is_not(None),
            )
        ):
            scores[posting_id] = float(cached)
    return scores


def _sent_application_ids(db: Session, application_ids: list[int]) -> set[int]:
    """Of *application_ids*, the ones with at least one delivered outbound mail."""
    if not application_ids:
        return set()
    return set(
        db.scalars(
            select(EmailThread.application_id)
            .join(Email, Email.thread_id == EmailThread.id)
            .where(
                EmailThread.application_id.in_(application_ids),
                Email.direction == EmailDirection.SENT,
                Email.status == EmailStatus.SENT,
            )
            .distinct()
        )
    )


def _summarize(report: Calibration) -> tuple[str, str]:
    """The verdict code and its sentence, from the numbers already tallied."""
    if report.scored_sends < MIN_TOTAL_SENDS:
        return (
            VERDICT_INSUFFICIENT,
            f"{report.scored_sends} scored sends so far — "
            f"{MIN_TOTAL_SENDS} is where these bands start meaning something.",
        )
    if report.correlation is None:
        return (
            VERDICT_INSUFFICIENT,
            "Every send so far scored the same, or every one of them got the "
            "same answer — there is no variation to read a pattern out of.",
        )
    if report.correlation <= -FLAT_CORRELATION:
        return (
            VERDICT_INVERTED,
            f"Lower-scoring outreach is replying *more* often than higher-scoring "
            f"outreach across {report.scored_sends} sends. Treat the fit score as "
            "unreliable for this account until the pattern reverses.",
        )
    if report.correlation < FLAT_CORRELATION:
        return (
            VERDICT_FLAT,
            f"Across {report.scored_sends} sends the fit score does not predict a "
            f"reply — every band lands near the {report.response_rate:.0%} account "
            "average. Raising your minimum would cut volume without lifting replies.",
        )
    return (
        VERDICT_SEPARATING,
        f"Higher-scoring outreach does reply more often across "
        f"{report.scored_sends} sends.",
    )


def _suggest_threshold(
    pairs: list[tuple[float, bool]], baseline: float
) -> tuple[int | None, str]:
    """The lowest band floor whose sends beat *baseline* by ``LIFT_REQUIRED``.

    Lowest rather than best on purpose. This number becomes someone's
    ``min_fit_score``, and every point it is raised is outreach that never
    happens — so the recommendation is the least restrictive line that still
    clears the bar, not the line with the prettiest percentage above it.
    """
    if baseline <= 0:
        return None, ""
    for lower, _upper, _label in BANDS:
        at_or_above = [replied for score, replied in pairs if score >= lower]
        if len(at_or_above) < MIN_THRESHOLD_SENDS:
            continue
        rate = sum(at_or_above) / len(at_or_above)
        if rate >= baseline * LIFT_REQUIRED:
            return lower, (
                f" Sends at {lower}+ reply {rate:.0%} of the time against a "
                f"{baseline:.0%} account average ({len(at_or_above)} sends) — "
                f"that is where a minimum fit score earns its keep."
            )
    return None, ""


def report(
    db: Session, user_id: int, *, days: int | None = None, now: datetime | None = None
) -> Calibration:
    """Bucket *user_id*'s sent outreach by fit score and grade the scorer.

    *days* narrows to outreach created in the window, cohorting on the
    application's creation date for the same reason the trend chart does: a
    reply belongs to the week its outreach went out, not the week it arrived.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days) if days else None

    stmt = select(Application).where(Application.user_id == user_id)
    if since is not None:
        stmt = stmt.where(Application.created_at >= since)
    applications = list(db.scalars(stmt))

    result = Calibration(
        bands=[Band(label=label, lower=lo, upper=hi - 1) for lo, hi, label in BANDS]
    )
    pref = db.scalar(
        select(AutopilotPreference).where(AutopilotPreference.user_id == user_id)
    )
    result.current_min_fit_score = pref.min_fit_score if pref else None

    if not applications:
        result.verdict, result.summary = _summarize(result)
        return result

    sent = _sent_application_ids(db, [a.id for a in applications])
    scores = _scores_by_posting(
        db,
        user_id,
        {a.job_posting_id for a in applications if a.job_posting_id is not None},
    )
    by_band = {lo: band for band, (lo, _, _) in zip(result.bands, BANDS, strict=True)}

    pairs: list[tuple[float, bool]] = []
    for application in applications:
        if application.id not in sent:
            continue
        score = (
            scores.get(application.job_posting_id)
            if application.job_posting_id is not None
            else None
        )
        if score is None:
            result.unscored_sends += 1
            continue

        replied = application.status in ENGAGED_STATUSES
        interviewed = application.status in INTERVIEWING_STATUSES
        pairs.append((score, replied))

        lower, _upper, _label = _band_for(score)
        band = by_band[lower]
        band.sends += 1
        band.responses += int(replied)
        band.interviews += int(interviewed)

        result.scored_sends += 1
        result.responses += int(replied)
        result.interviews += int(interviewed)

    result.response_rate = (
        round(result.responses / result.scored_sends, 3) if result.scored_sends else 0.0
    )
    result.correlation = _pearson(
        [score for score, _ in pairs], [float(replied) for _, replied in pairs]
    )
    result.verdict, result.summary = _summarize(result)

    if result.verdict == VERDICT_SEPARATING:
        threshold, sentence = _suggest_threshold(pairs, result.response_rate)
        result.suggested_min_fit_score = threshold
        result.summary += sentence

    return result


__all__ = [
    "BANDS",
    "FLAT_CORRELATION",
    "LIFT_REQUIRED",
    "MIN_BAND_SENDS",
    "MIN_THRESHOLD_SENDS",
    "MIN_TOTAL_SENDS",
    "VERDICT_FLAT",
    "VERDICT_INSUFFICIENT",
    "VERDICT_INVERTED",
    "VERDICT_SEPARATING",
    "Band",
    "Calibration",
    "report",
]
