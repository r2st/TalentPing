"""Subject-line A/B testing — generate, split, converge.

Subject lines used to be a by-product of body composition: ``compose_outreach``
asked for ``Subject: …`` on the first line and fell back to
``f"{name} — {role}"``, which is what every keyless deployment and every failed
LLM call produced — the same string for every recruiter that candidate ever
contacted. Nobody measured any of it.

Now each campaign gets two or three variants, assignment is random, and the open
rate (from :mod:`app.services.email_tracking`) picks a winner.

**The statistics are deliberately modest.** A campaign is tens of emails, not
thousands. At n=30 split three ways a ten-point difference is not significant by
any honest test, so:

* convergence needs :data:`MIN_SENDS_PER_VARIANT` **and** a :data:`MIN_LIFT` gap;
* even after converging, :data:`EXPLOIT_SHARE` keeps a tenth of sends exploring,
  so a winner picked from a thin sample can be overturned rather than locked in;
* the API reports ``confident: false`` until both thresholds are met.

The measurement inherits every bias in docs/features/email-tracking.md §2 — proxy
prefetch, image blocking. Those biases apply equally across variants of the same
campaign, which is why comparing them is defensible when quoting the absolute
number is not.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.campaign import Campaign
from app.models.subject_variant import SubjectVariant
from app.services import untrusted
from app.services.ai_composer import CandidateContext, RecruiterContext
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    extract_json_object,
    llm_is_configured,
    looks_like_reasoning,
)

logger = logging.getLogger(__name__)

LABELS = ("A", "B", "C", "D")

# Below this many sends on *every* arm, no winner is declared.
MIN_SENDS_PER_VARIANT = 10
# And the leader must beat the runner-up by this much, absolute, on open rate.
MIN_LIFT = 0.10
# Share of sends the winner takes once one exists. The remainder keeps exploring.
EXPLOIT_SHARE = 0.9

MAX_SUBJECT_LENGTH = 120


_SYSTEM_PROMPT = (
    "You write subject lines for a job seeker's cold email to a recruiter. "
    "Produce exactly {n} distinct options, each under 60 characters, each taking "
    "a DIFFERENT angle: (a) role plus the candidate's strongest proof point, "
    "(b) a specific, curiosity-opening question, (c) plain and direct with the "
    "candidate's name. No clickbait, no false urgency, no fake 'Re:' or 'Fwd:', "
    "no emoji, no ALL CAPS. Never invent facts about the candidate and never "
    "claim knowledge of a specific open role.\n\n"
    'Return ONLY a JSON object: {{"variants": ["...", "..."]}}'
)


def _clean(text: object) -> str | None:
    """Normalize one model-produced subject line, or reject it."""
    if not isinstance(text, str):
        return None
    line = text.strip().strip('"').strip()
    if line.lower().startswith("subject:"):
        line = line.split(":", 1)[1].strip()
    line = " ".join(line.split())
    if not line or looks_like_reasoning(line):
        return None
    return line[:MAX_SUBJECT_LENGTH]


def _template_variants(cand: CandidateContext, rec: RecruiterContext) -> list[str]:
    """Deterministic variants — the keyless path, and what the tests exercise.

    Variant A is the subject the product shipped before this feature, kept
    deliberately as the control.
    """
    role = (cand.target_roles or ["a new role"])[0]
    company = rec.company or "your team"
    seniority = (cand.seniority or "").strip()
    # Only prefix the seniority when the role doesn't already carry it —
    # otherwise a "senior" candidate targeting "Senior Backend Engineer" gets
    # "a senior Senior Backend Engineer".
    who = (
        f"{seniority} {role}"
        if seniority and seniority.lower() not in role.lower()
        else role
    )
    return [
        f"{cand.name} — {role}",
        f"{role} at {company}?",
        f"Quick question from a {who}",
    ]


def generate_variants(
    cand: CandidateContext,
    rec: RecruiterContext,
    *,
    count: int | None = None,
) -> tuple[list[str], str]:
    """Return ``(variants, generated_with)``, degrading to templates.

    Never fails: no API key, a dead provider chain, a chain-of-thought response
    or fewer than two usable lines all fall back to the deterministic set.
    """
    count = max(2, min(count or settings.subject_ab_variants, len(LABELS)))
    fallback = (_template_variants(cand, rec)[:count], "template")

    if not llm_is_configured():
        return fallback

    try:
        raw = chat_completion(
            [
                {
                    "role": "system",
                    "content": untrusted.guarded(_SYSTEM_PROMPT.format(n=count)),
                },
                {
                    "role": "user",
                    "content": "\n".join(
                        [
                            f"Candidate: {cand.name}",
                            f"Headline: {cand.headline or 'n/a'}",
                            f"Seniority: {cand.seniority or 'n/a'}",
                            f"Key skills: {', '.join((cand.skills or [])[:8]) or 'n/a'}",
                            f"Target role: {(cand.target_roles or ['n/a'])[0]}",
                            # The candidate's record ends there. The company and
                            # the recruiter's name came off a crawled page or a
                            # From header, exactly as they do in `ai_composer` —
                            # and what this call writes is the subject line of
                            # the mail that goes to them. See
                            # :mod:`app.services.untrusted`.
                            "The recruiter, as this product recorded them:",
                            untrusted.fence(
                                "\n".join(
                                    [
                                        f"Company: {rec.company or 'n/a'}",
                                        f"Recruiter: {rec.name or 'unknown'}",
                                    ]
                                ),
                                label="recruiter record",
                            ),
                        ]
                    ),
                },
            ],
            model=settings.openrouter_model,
            temperature=0.9,
            max_tokens=1500,
        )
    except OpenRouterError as exc:
        logger.info("subject variants fell back to templates: %s", exc)
        return fallback

    payload = extract_json_object(raw) or {}
    seen: set[str] = set()
    variants: list[str] = []
    for item in payload.get("variants") or []:
        line = _clean(item)
        if line is None or line.lower() in seen:
            continue
        seen.add(line.lower())
        variants.append(line)
        if len(variants) == count:
            break

    if len(variants) < 2:
        return fallback
    return variants, "llm"


def ensure_variants(
    db: Session,
    campaign: Campaign,
    cand: CandidateContext,
    rec: RecruiterContext,
) -> list[SubjectVariant]:
    """The campaign's variants, generating them once on first use.

    Idempotent: a campaign that already has rows gets them back untouched, so
    every email in the campaign is assigned from the same experiment.

    Idempotent *including against a concurrent caller*, which the read-then-
    insert alone was not. ``uq_subject_variant_campaign_label`` is what makes the
    experiment one experiment, and two runs of the same campaign reaching this
    at once — a resumed campaign racing a beat-launched one, a user who clicks
    Launch twice — both saw no rows and both inserted label ``A``. The loser
    raised ``IntegrityError`` out of the enclosing commit, and the caller is the
    per-recruiter loop inside ``generate_and_queue``, so one lost race marked the
    whole campaign FAILED and discarded every email already composed for it.

    The insert therefore runs in a savepoint: losing the race rolls back only
    the rows this call tried to write, and the winner's experiment is read back
    and used. Which of the two generated the variants does not matter — what
    matters is that every email in the campaign is assigned from one set.
    """

    def _existing() -> list[SubjectVariant]:
        return list(
            db.scalars(
                select(SubjectVariant)
                .where(SubjectVariant.campaign_id == campaign.id)
                .order_by(SubjectVariant.id)
            )
        )

    existing = _existing()
    if existing:
        return existing

    texts, generated_with = generate_variants(cand, rec)
    rows = [
        SubjectVariant(
            user_id=campaign.user_id,
            campaign_id=campaign.id,
            label=LABELS[i],
            text=text[:255],
            generated_with=generated_with,
        )
        for i, text in enumerate(texts)
    ]
    try:
        with db.begin_nested():
            db.add_all(rows)
            db.flush()
    except IntegrityError:
        logger.info(
            "campaign %s: subject variants were written by a concurrent run",
            campaign.id,
        )
        existing = _existing()
        if existing:
            return existing
        # The constraint fired for a reason we did not predict. Re-raise rather
        # than hand back an empty experiment, which `assign_variant` would read
        # as "no experiment" and quietly send the composer's subject to
        # everybody — the exact unmeasured behaviour this module replaced.
        raise
    return rows


def assign_variant(
    variants: list[SubjectVariant],
    *,
    rng: random.Random | None = None,
) -> SubjectVariant | None:
    """Pick the arm this email will use.

    Uniform over the active arms until a winner exists; afterwards the winner
    takes :data:`EXPLOIT_SHARE` and the rest keeps exploring, so a winner crowned
    on a thin sample can still be overturned.
    """
    if not variants:
        return None
    choice = rng.choice if rng is not None else random.choice
    uniform = rng.random if rng is not None else random.random

    active = [v for v in variants if v.is_active]
    if not active:
        active = variants

    winner = next((v for v in variants if v.is_winner), None)
    if winner is not None:
        if uniform() < EXPLOIT_SHARE:
            return winner
        others = [v for v in variants if v.id != winner.id]
        return choice(others) if others else winner

    return choice(active)


def record_send(db: Session, variant_id: int | None) -> None:
    """Book an impression. Called after Gmail accepts the message, not at compose.

    A draft that is never approved must not count as an impression.
    """
    variant = db.get(SubjectVariant, variant_id) if variant_id else None
    if variant is not None:
        variant.sends = (variant.sends or 0) + 1


def record_open(db: Session, variant_id: int | None) -> None:
    """Book a first, non-prefetch open (the tracking service enforces both)."""
    variant = db.get(SubjectVariant, variant_id) if variant_id else None
    if variant is not None:
        variant.opens = (variant.opens or 0) + 1


def record_reply(db: Session, variant_id: int | None) -> None:
    """Book the first inbound reply on the thread this variant's email started."""
    variant = db.get(SubjectVariant, variant_id) if variant_id else None
    if variant is not None:
        variant.replies = (variant.replies or 0) + 1


def _clears_lift(leader: float, runner_up: float) -> bool:
    """Whether the leader's margin over the runner-up meets :data:`MIN_LIFT`.

    Rounded to the three decimals the rates themselves already carry. A leader
    on 30% against 20% is exactly ten points ahead, but ``0.3 - 0.2`` is
    ``0.09999999999999998`` in binary floating point, so the boundary case the
    threshold was written to admit failed it.
    """
    return round(leader - runner_up, 3) >= MIN_LIFT


def _crown(variants: list[SubjectVariant], leader: SubjectVariant) -> SubjectVariant:
    """Make *leader* the winner and every other arm a loser."""
    for variant in variants:
        variant.is_winner = variant.id == leader.id
        variant.is_active = variant.id == leader.id
    return leader


def _maybe_overturn(
    variants: list[SubjectVariant], winner: SubjectVariant
) -> SubjectVariant:
    """Hand the crown to a challenger that has since done better. Or don't.

    :data:`EXPLOIT_SHARE` keeps a tenth of sends on the other arms after a winner
    is crowned, and the module has always said why: "so a winner picked from a
    thin sample can be overturned rather than locked in". Nothing read that
    traffic. ``maybe_converge`` returned the standing winner the moment it found
    one and never looked at the numbers again, so every impression and every open
    the exploration bought was recorded and then ignored — a tenth of every
    converged campaign's sends spent on a subject line the product had already
    judged worse, in exchange for a correction that could not happen.

    The bar is the one that crowned the winner in the first place, asked the
    other way round: the challenger needs :data:`MIN_SENDS_PER_VARIANT` of its
    own and has to beat the *incumbent* by :data:`MIN_LIFT`. Beating it by less
    is not evidence, and at a tenth of the traffic a challenger takes a long time
    to gather even that — which is the conservatism this wants. Measuring against
    the incumbent rather than the field is also what stops two close arms trading
    the crown back and forth: overturning requires a clear gap in one direction,
    so it cannot immediately be reversed by a smaller one in the other.
    """
    challengers = [
        v
        for v in variants
        if v.id != winner.id and (v.sends or 0) >= MIN_SENDS_PER_VARIANT
    ]
    if not challengers:
        return winner
    best = max(challengers, key=lambda v: (v.open_rate, v.sends or 0))
    if not _clears_lift(best.open_rate, winner.open_rate):
        return winner

    logger.info(
        "subject variant %s overtakes %s at %.0f%% vs %.0f%% open rate",
        best.label,
        winner.label,
        best.open_rate * 100,
        winner.open_rate * 100,
    )
    return _crown(variants, best)


def maybe_converge(db: Session, campaign: Campaign) -> SubjectVariant | None:
    """Crown a winner once there is enough evidence. Returns it, or None.

    Both thresholds must be met: every arm needs :data:`MIN_SENDS_PER_VARIANT`
    impressions, and the leader must beat the runner-up by :data:`MIN_LIFT` on
    open rate. Below either, nothing is decided and the split stays uniform.

    A campaign that already has a winner is not finished with: the exploration
    share goes on running, and :func:`_maybe_overturn` is what makes that mean
    something.
    """
    variants = list(
        db.scalars(select(SubjectVariant).where(SubjectVariant.campaign_id == campaign.id))
    )
    if len(variants) < 2:
        return None
    winner = next((v for v in variants if v.is_winner), None)
    if winner is not None:
        return _maybe_overturn(variants, winner)
    if any((v.sends or 0) < MIN_SENDS_PER_VARIANT for v in variants):
        return None

    ranked = sorted(variants, key=lambda v: (v.open_rate, v.sends), reverse=True)
    leader, runner_up = ranked[0], ranked[1]
    if not _clears_lift(leader.open_rate, runner_up.open_rate):
        return None

    logger.info(
        "campaign %s: subject variant %s wins at %.0f%% open rate",
        campaign.id,
        leader.label,
        leader.open_rate * 100,
    )
    return _crown(variants, leader)


@dataclass
class VariantStats:
    """One arm, as the API reports it."""

    id: int
    label: str
    text: str
    sends: int
    opens: int
    replies: int
    open_rate: float
    reply_rate: float
    is_winner: bool
    is_active: bool
    generated_with: str


def is_conclusive(rows: list[VariantStats]) -> bool:
    """Whether this experiment has actually told us anything yet.

    The same two thresholds :func:`maybe_converge` crowns a winner on, asked as
    a question instead of an action — because "enough evidence to declare a
    winner" and "enough evidence to *show* the user a result" have to be the
    same bar, and they were not. This answered on impressions alone, so three
    arms sitting at 10 sends and 0 opens each reported ``confident: true``: the
    UI dropped its "early — not enough data yet" caveat and presented a dead
    heat as a settled finding, on an experiment the service itself would refuse
    to converge.

    A single arm is never conclusive whatever its volume — there is nothing to
    compare it against. An already-crowned winner always is: it cleared both
    bars when it was crowned, and the exploration share that keeps running
    afterwards deliberately narrows the gap, which must not read as the result
    coming apart.
    """
    if len(rows) < 2:
        return False
    if any(r.is_winner for r in rows):
        return True
    if any(r.sends < MIN_SENDS_PER_VARIANT for r in rows):
        return False
    ranked = sorted((r.open_rate for r in rows), reverse=True)
    return _clears_lift(ranked[0], ranked[1])


def _row(variant: SubjectVariant) -> VariantStats:
    return VariantStats(
        id=variant.id,
        label=variant.label,
        text=variant.text,
        sends=variant.sends or 0,
        opens=variant.opens or 0,
        replies=variant.replies or 0,
        open_rate=variant.open_rate,
        reply_rate=variant.reply_rate,
        is_winner=variant.is_winner,
        is_active=variant.is_active,
        generated_with=variant.generated_with,
    )


def stats_for_campaign(db: Session, campaign_id: int) -> tuple[list[VariantStats], bool]:
    """``(rows, confident)`` for one campaign's experiment.

    ``confident`` is :func:`is_conclusive` — both thresholds, not just volume.
    """
    variants = list(
        db.scalars(
            select(SubjectVariant)
            .where(SubjectVariant.campaign_id == campaign_id)
            .order_by(SubjectVariant.label)
        )
    )
    rows = [_row(v) for v in variants]
    return rows, is_conclusive(rows)


def stats_for_user(
    db: Session, user_id: int, *, campaign_id: int | None = None
) -> dict[int, tuple[list[VariantStats], bool]]:
    """Every campaign's experiment for one user, keyed by campaign id, in one read.

    The same answer as calling :func:`stats_for_campaign` in a loop, at one
    query instead of one per campaign. The loop was invisible on a developer's
    two campaigns and was the whole cost of the page for anyone running a
    search for six months.

    Scoped by joining to ``Campaign.user_id`` rather than by an ``IN`` list of
    ids the caller already loaded. Both are one query, but the join cannot grow
    a parameter list — a user with more campaigns than SQLite's variable limit
    would have turned an N+1 into a hard error, which is a worse bug than the
    one being fixed.

    Campaigns with no variants are absent rather than present-and-empty: the
    only caller skips them, and a key whose value is "nothing happened here"
    invites a reader to render it.
    """
    stmt = (
        select(SubjectVariant)
        .join(Campaign, SubjectVariant.campaign_id == Campaign.id)
        .where(Campaign.user_id == user_id)
        # Label order is what the single-campaign version returns and what the
        # page's A/B/C columns are read in; the campaign key only groups.
        .order_by(SubjectVariant.campaign_id, SubjectVariant.label)
    )
    if campaign_id is not None:
        stmt = stmt.where(SubjectVariant.campaign_id == campaign_id)

    grouped: dict[int, list[VariantStats]] = {}
    for variant in db.scalars(stmt):
        grouped.setdefault(variant.campaign_id, []).append(_row(variant))
    return {
        key: (rows, is_conclusive(rows)) for key, rows in grouped.items()
    }


__all__ = [
    "EXPLOIT_SHARE",
    "LABELS",
    "MIN_LIFT",
    "MIN_SENDS_PER_VARIANT",
    "VariantStats",
    "assign_variant",
    "ensure_variants",
    "generate_variants",
    "is_conclusive",
    "maybe_converge",
    "record_open",
    "record_reply",
    "record_send",
    "stats_for_campaign",
    "stats_for_user",
]
