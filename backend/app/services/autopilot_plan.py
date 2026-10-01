"""The autopilot dry run — what the agent will do next, before it does it.

Arming ``auto_send`` asks a user to trust software to email strangers as them,
from their own mailbox, under their own name. ``GET /dashboard/queue`` answers
"what is queued?" with a number, which is not the question. The question is
*"what will it actually do?"* — which companies, which contact at each, which
resume, at what time, and why that posting and not the forty others in the feed.

This module answers it by walking the same road :func:`run_user_autopilot` walks
and stopping short of every side effect. Three properties make that worth having
rather than merely reassuring:

* **Read-only.** Nothing here writes a row, scrapes a page, calls a model or
  sends anything. The gates are pure functions and are reused as-is; the contact,
  the mailbox and the fit reasoning are read from what is already on file. A dry
  run that created the thing it was describing would be a live run with a
  friendlier name.
* **Offline, therefore honest about ignorance.** The live pipeline resolves a
  contact by crawling the company's careers page
  (:func:`~app.services.recruiter_discovery.best_recruiter_for_company`), which
  costs a network round trip and writes rows. The plan refuses to do that, so for
  a company nobody has crawled yet it says *"no contact on file — autopilot will
  search for one"* instead of inventing an address it cannot know. That gap is
  information: it is exactly the set of sends that might not happen.
* **Deterministic.** The real scheduler jitters send times so fifty messages
  don't leave at 09:00:00 sharp. Jitter in a *plan* would mean two reads a second
  apart disagreed about when a message goes out, and a plan that changes when you
  refresh it teaches the user to distrust it. So the projected times use the
  midpoint of the spacing range and ``jitter=False``: the same shape, without the
  noise. They are forecasts to the minute, not commitments to the second.

What it deliberately does **not** promise: that these exact postings will go out.
The feed is refreshed at the start of every real run, so a better match found ten
minutes from now can displace the bottom of this list, and a contact crawl can
fail. The plan describes the current queue under current settings — the same
basis the user is being asked to approve.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.sql_text import escape_like
from app.models.autopilot import AutopilotPreference
from app.models.fit_score import FitScore
from app.models.job import JobPosting
from app.models.recruiter import DeliveryState, Recruiter
from app.models.recruiter_cache import RecruiterCache
from app.models.user import User
from app.services import (
    auto_apply_service,
    gmail_accounts,
    profile_service,
    send_policy,
    send_time,
)
from app.services.job_dedup import normalize_company
from app.services.profile_service import ScoringTarget

logger = logging.getLogger(__name__)

# How far ahead the plan looks. A day, because that is the horizon the daily
# application cap and the warm-up ramp are both denominated in — a plan over any
# other window would have to explain which of its numbers it had rescaled.
PLAN_WINDOW_HOURS = 24

# Postings considered beyond the budget, so the plan can say how deep the backlog
# is without walking a feed of thousands to count it.
_BACKLOG_SCAN_LIMIT = 200

# Where a planned contact came from. Ordered by how much the plan can promise.
SOURCE_KNOWN = "known"      # a Recruiter row this user already has
SOURCE_CACHED = "cached"    # a global careers-page crawl, not yet a row here
SOURCE_SEARCH = "search"    # nothing on file; the live run will go looking


@dataclass(frozen=True)
class PlannedContact:
    """The address a planned send would go to, and how confident that is."""

    email: str | None
    name: str | None = None
    title: str | None = None
    confidence: float | None = None
    source: str = SOURCE_SEARCH
    # Set when the contact exists but mail to it would be held back anyway.
    warning: str | None = None

    @property
    def resolved(self) -> bool:
        return bool(self.email) and self.warning is None

    def as_dict(self) -> dict:
        return {
            "email": self.email,
            "name": self.name,
            "title": self.title,
            "confidence": round(self.confidence, 2) if self.confidence is not None else None,
            "source": self.source,
            "warning": self.warning,
        }


@dataclass
class PlannedSend:
    """One outreach email autopilot intends to produce, fully explained."""

    job_posting_id: int
    title: str | None
    company: str | None
    location: str | None
    remote: bool | None
    url: str | None
    posted_at: datetime | None

    fit_score: float | None
    llm_fit_score: float | None
    recommendation: str | None

    profile_id: int | None
    profile_label: str
    resume_id: int
    resume_label: str

    contact: PlannedContact
    from_address: str | None
    # inline | attachment | None — how the cover letter would travel, if any.
    cover_letter: str | None

    scheduled_at: datetime | None
    timezone: str
    # False when this message would land in the review queue instead of sending.
    sends_unreviewed: bool
    # Why this posting, in the user's terms. The first line is the headline.
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "job_posting_id": self.job_posting_id,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "remote": self.remote,
            "url": self.url,
            "posted_at": self.posted_at,
            "fit_score": self.fit_score,
            "llm_fit_score": self.llm_fit_score,
            "recommendation": self.recommendation,
            "profile_id": self.profile_id,
            "profile_label": self.profile_label,
            "resume_id": self.resume_id,
            "resume_label": self.resume_label,
            "contact": self.contact.as_dict(),
            "from_address": self.from_address,
            "cover_letter": self.cover_letter,
            "scheduled_at": self.scheduled_at,
            "timezone": self.timezone,
            "sends_unreviewed": self.sends_unreviewed,
            "reasons": list(self.reasons),
        }


@dataclass
class SkippedPosting:
    """A posting the plan walked past, with the gate's own sentence."""

    job_posting_id: int
    title: str | None
    company: str | None
    reason: str

    def as_dict(self) -> dict:
        return {
            "job_posting_id": self.job_posting_id,
            "title": self.title,
            "company": self.company,
            "reason": self.reason,
        }


@dataclass
class DryRunPlan:
    """The next 24 hours of autopilot, as a report the user can read."""

    generated_at: datetime
    window_hours: int = PLAN_WINDOW_HOURS

    active: bool = False
    # Why there is nothing to plan — "autopilot is off", "no mailbox connected".
    blocked_reason: str | None = None

    # The auto-send picture, straight from send_policy: what the user asked for,
    # and what the policy permits this minute.
    auto_send: bool = False
    sends_unreviewed: bool = False
    send_policy_reason: str | None = None

    # The budget, decomposed. A single number can't distinguish "you capped it
    # here" from "your mailbox is still warming up", and the user's next action
    # differs completely between the two.
    budget: int = 0
    daily_application_limit: int = 0
    applied_today: int = 0
    send_headroom: int = 0
    warmup_day_limit: int = 0
    # Why the headroom is zero when a reputation guardrail — not the day's
    # sending — is what emptied it. Distinct from `blocked_reason`: the plan is
    # still computed and still worth reading, it just cannot leave yet.
    send_hold_reason: str | None = None

    sends: list[PlannedSend] = field(default_factory=list)
    skipped: list[SkippedPosting] = field(default_factory=list)
    # Postings that cleared every gate but sit past the budget — tomorrow's work.
    backlog: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """One sentence, phrased for someone deciding whether to arm auto-send."""
        if self.blocked_reason:
            return self.blocked_reason
        if not self.sends:
            if self.send_hold_reason:
                # Ahead of "budget is spent", which would be the wrong sentence
                # and the wrong next action: nothing the user does to their
                # daily cap moves a reputation hold.
                waiting = f", {self.backlog} matches waiting" if self.backlog else ""
                return f"{self.send_hold_reason}{waiting}"
            if self.backlog:
                return (
                    f"nothing goes out in the next {self.window_hours}h — "
                    f"today's budget is spent, with {self.backlog} matches waiting"
                )
            return f"nothing to send in the next {self.window_hours}h"

        companies = {s.company for s in self.sends if s.company}
        verb = "sends" if self.sends_unreviewed else "drafts"
        where = f" at {len(companies)} companies" if len(companies) > 1 else ""
        tail = "" if self.sends_unreviewed else ", each waiting for your approval"
        unresolved = sum(1 for s in self.sends if not s.contact.resolved)
        gap = (
            f"; {unresolved} still needs a contact found" if unresolved else ""
        )
        return f"{verb} {len(self.sends)} email(s){where}{tail}{gap}"

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "window_hours": self.window_hours,
            "active": self.active,
            "blocked_reason": self.blocked_reason,
            "auto_send": self.auto_send,
            "sends_unreviewed": self.sends_unreviewed,
            "send_policy_reason": self.send_policy_reason,
            "budget": self.budget,
            "daily_application_limit": self.daily_application_limit,
            "applied_today": self.applied_today,
            "send_headroom": self.send_headroom,
            "warmup_day_limit": self.warmup_day_limit,
            "send_hold_reason": self.send_hold_reason,
            "sends": [s.as_dict() for s in self.sends],
            "skipped": [s.as_dict() for s in self.skipped],
            "backlog": self.backlog,
            "notes": list(self.notes),
            "summary": self.summary,
        }


# --------------------------------------------------------------------------- #
# Contact resolution, from what is already on file                             #
# --------------------------------------------------------------------------- #


def _cached_contact(db: Session, company: str) -> PlannedContact | None:
    """The best contact from a previous global crawl of *company*, if any.

    Reads :class:`~app.models.recruiter_cache.RecruiterCache` — the shared crawl
    cache — without touching the network. The live run would reuse the same row
    for anything still fresh, so this is the same answer, arrived at for free.

    Matched on the normalized company name rather than the domain, because
    resolving a name to a domain is itself a network call. A stale row is still
    reported: the plan's job is to say who autopilot has on file, and freshness
    is the live run's decision to make.
    """
    normalized = normalize_company(company)
    if not normalized:
        return None

    # `escape_like`: the needle is a company name the user typed, and this table
    # is the deployment-wide crawl cache. The `normalize_company` check below is
    # what decides the answer, so a wildcard cannot leak another employer's
    # contacts into the plan — but `%` matches every row, and loading the whole
    # shared cache to then discard it is a plan request that scales with how
    # long the deployment has been running.
    rows = db.scalars(
        select(RecruiterCache).where(
            RecruiterCache.company.ilike(
                f"%{escape_like(company.strip())}%", escape="\\"
            )
        )
    ).all()
    for row in rows:
        if normalize_company(row.company) != normalized:
            continue
        for raw in row.contacts or []:
            email = (raw.get("email") or "").strip().lower()
            if not email:
                continue
            confidence = raw.get("confidence")
            return PlannedContact(
                email=email,
                name=raw.get("name") or None,
                title=raw.get("title") or None,
                confidence=(
                    float(confidence) if isinstance(confidence, (int, float)) else None
                ),
                source=SOURCE_CACHED,
            )
    return None


def planned_contact(db: Session, user: User, company: str | None) -> PlannedContact:
    """Who a planned send would go to, using only what is already known.

    Order: a :class:`~app.models.recruiter.Recruiter` row this user already holds
    (best confidence first), then the global crawl cache, then an honest "we will
    have to go looking".

    Opted-out and hard-bounced contacts are not silently swapped for a worse one.
    The live pipeline would move on to the next address, but a plan that quietly
    substituted a different recipient would be describing a send the user did not
    read about — so the row is reported *with its warning*, which is also the
    prompt the user needs to fix it.
    """
    if not company or not company.strip():
        return PlannedContact(email=None, source=SOURCE_SEARCH)

    normalized = normalize_company(company)
    known = [
        row
        for row in db.scalars(
            select(Recruiter)
            .where(
                Recruiter.user_id == user.id,
                Recruiter.company.ilike(
                    f"%{escape_like(company.strip())}%", escape="\\"
                ),
            )
            .order_by(Recruiter.confidence.desc(), Recruiter.id)
        ).all()
        if normalize_company(row.company) == normalized
    ]

    for row in known:
        warning = None
        if row.opted_out:
            warning = "asked not to be contacted"
        elif row.is_excluded:
            warning = "you excluded this contact"
        elif row.delivery_state == DeliveryState.HARD_BOUNCED:
            warning = "this address hard-bounced"
        return PlannedContact(
            email=row.email,
            name=row.name,
            title=row.title,
            confidence=row.confidence,
            source=SOURCE_KNOWN,
            warning=warning,
        )

    cached = _cached_contact(db, company)
    if cached is not None:
        return cached
    return PlannedContact(email=None, source=SOURCE_SEARCH)


# --------------------------------------------------------------------------- #
# Why this posting                                                             #
# --------------------------------------------------------------------------- #


def _fit_row(db: Session, posting: JobPosting, target: ScoringTarget) -> FitScore | None:
    """The stored breakdown behind this posting's score, for the winning profile.

    Prefers the row scored for the profile that would apply; falls back to any row
    for the posting, since a pre-profiles score still explains the number the feed
    is showing.
    """
    rows = db.scalars(
        select(FitScore)
        .where(FitScore.job_posting_id == posting.id, FitScore.user_id == posting.user_id)
        .order_by(FitScore.id.desc())
    ).all()
    if not rows:
        return None
    for row in rows:
        if row.profile_id == target.profile_id:
            return row
    return rows[0]


def _posting_age_days(posting: JobPosting, now: datetime) -> int | None:
    posted = posting.posted_at
    if posted is None:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return max(0, (now - posted).days)


# The dimensions worth quoting, and what to call them to a job seeker. Salary and
# industry are left out on purpose: they are the two that most often fall back to
# neutral for want of data, and "salary: not stated" is noise in a list whose job
# is to justify a decision.
_QUOTED_DIMENSIONS = (
    ("skills", "Skills"),
    ("role", "Role"),
    ("experience", "Experience"),
    ("location", "Location"),
)


def explain_choice(
    db: Session,
    posting: JobPosting,
    target: ScoringTarget,
    pref: AutopilotPreference,
    *,
    now: datetime,
) -> tuple[list[str], str | None]:
    """Why autopilot picked *posting*. Returns ``(reasons, recommendation)``.

    Built from what is already stored — the cached score, its per-dimension notes
    and Scout's re-rank — so the explanation costs a query rather than a model
    call, and says the same thing the feed says. An explanation that disagreed
    with the number next to it would be worse than none.
    """
    reasons: list[str] = []
    score = posting.fit_score
    fit = _fit_row(db, posting, target)
    recommendation = fit.recommendation if fit is not None else None

    if score is not None:
        headline = f"Fit {score:.0f}/100, at or above your {pref.min_fit_score} threshold"
        if recommendation:
            headline += f" — rated {recommendation}"
        reasons.append(headline)

    if fit is not None:
        notes = fit.notes or {}
        for key, label in _QUOTED_DIMENSIONS:
            note = (notes.get(key) or "").strip()
            if note:
                reasons.append(f"{label}: {note}")
        matched = [s for s in (fit.matched_skills or []) if isinstance(s, str)]
        if matched:
            reasons.append("Overlapping skills: " + ", ".join(matched[:6]))
        missing = [s for s in (fit.missing_skills or []) if isinstance(s, str)]
        if missing:
            reasons.append("Not evidenced in your resume: " + ", ".join(missing[:4]))

    if posting.llm_fit_score is not None:
        line = f"Second opinion scored it {posting.llm_fit_score:.0f}/100"
        rationale = (posting.llm_reasoning or "").strip()
        if rationale:
            line += f" — {rationale[:240]}"
        reasons.append(line)

    age = _posting_age_days(posting, now)
    if age is not None:
        reasons.append("Posted today" if age == 0 else f"Posted {age} day(s) ago")

    reasons.append(f"Matched under {target.label}")
    return reasons, recommendation


# --------------------------------------------------------------------------- #
# Timing                                                                       #
# --------------------------------------------------------------------------- #


def _spacing_seconds() -> int:
    """The per-message spacing the plan assumes: the middle of the real range.

    ``enqueue_campaign_sends`` draws uniformly from
    ``[min_send_interval_seconds, max_send_interval_seconds]`` per message. The
    midpoint is the expected value of that draw, which is the right thing for a
    forecast and — unlike a fresh sample — the same on every read.
    """
    low = max(0, settings.min_send_interval_seconds)
    high = max(low, settings.max_send_interval_seconds)
    return (low + high) // 2


def projected_send_time(
    db: Session,
    recruiter_email: str | None,
    company: str | None,
    posting: JobPosting,
    *,
    cumulative_seconds: int,
    now: datetime,
) -> tuple[datetime, str]:
    """When this message would go out, and the zone that decided it.

    Mirrors ``email_tasks.send_countdown``: the accumulated spacing feeds the
    slot search rather than being replaced by it, so the throttle and the
    business-hours rule compose exactly as they do at send time.

    Resolution runs with ``cache=False``: a dry run must not write a resolved
    timezone back onto a recruiter row it merely looked at.
    """
    due = now + timedelta(seconds=cumulative_seconds)
    zone = _plan_timezone(db, recruiter_email, company, posting)
    if not settings.send_time_optimization_enabled:
        return due, str(zone)
    return send_time.next_slot(due, zone, jitter=False), str(zone)


def _plan_timezone(
    db: Session, recruiter_email: str | None, company: str | None, posting: JobPosting
):
    """The recipient's zone, resolved by the sender's own function.

    :func:`send_time.resolve_timezone` is handed a recruiter and the application
    the mail belongs to, and consults, in order: the cached column, the company's
    researched headquarters, the posting's location, the email's country TLD, the
    default. All five sources matter to a plan and none of them are re-derived
    here — the point is to predict the sender, not to have an opinion of its own.

    Two stand-ins make that possible before either row exists. A cached-crawl
    contact has no ``Recruiter`` row yet, and no application exists at all until
    the live run creates one; both stubs carry only the attributes the resolver
    reads, so neither can be written to by accident.
    """
    row = None
    if recruiter_email:
        row = db.scalar(
            select(Recruiter).where(
                Recruiter.user_id == posting.user_id,
                Recruiter.email == recruiter_email.lower(),
            )
        )
    if row is None:
        row = _ContactStub(company=company, email=recruiter_email)
    zone = send_time.resolve_timezone(
        db, row, application=_ApplicationStub(job_posting_id=posting.id), cache=False
    )
    if zone is None:  # pragma: no cover - resolve_timezone always returns a zone
        return send_time.default_timezone()
    return zone


@dataclass
class _ContactStub:
    """Just enough of a recruiter for :func:`send_time.resolve_timezone`."""

    company: str | None
    email: str | None
    timezone: str | None = None


@dataclass
class _ApplicationStub:
    """Just enough of an application to point the resolver at the posting."""

    job_posting_id: int


# --------------------------------------------------------------------------- #
# The plan                                                                     #
# --------------------------------------------------------------------------- #


def build_plan(db: Session, user: User, *, limit: int | None = None) -> DryRunPlan:
    """The next :data:`PLAN_WINDOW_HOURS` of autopilot for *user*, explained.

    Safe to call from a request handler and safe to poll: no network, no model
    calls, no writes, and the same inputs give the same answer.

    *limit* caps the planned sends below the computed budget, for a UI that wants
    a preview rather than the whole day. The budget itself is still reported in
    full, so a truncated list never reads as a smaller day's work.
    """
    now = datetime.now(UTC)
    plan = DryRunPlan(generated_at=now)

    pref = user.autopilot
    if pref is None or not pref.is_active:
        plan.blocked_reason = "Autopilot is off — nothing is scheduled."
        return plan
    if not user.gmail_connected:
        plan.blocked_reason = "No mailbox connected — autopilot cannot send."
        return plan

    plan.active = True
    plan.daily_application_limit = pref.daily_application_limit

    decision = send_policy.evaluate(db, user, pref, now=now)
    plan.auto_send = bool(pref.auto_send)
    plan.sends_unreviewed = decision.enabled
    plan.send_policy_reason = decision.reason

    headroom, day_limit = auto_apply_service.send_headroom(db, user, now=now)
    used = auto_apply_service.applied_today(db, user.id)
    plan.send_headroom = headroom
    plan.warmup_day_limit = day_limit
    plan.applied_today = used
    budget = min(headroom, max(0, pref.daily_application_limit - used))
    plan.budget = budget
    # A reputation hold empties the headroom too, and left unsaid the panel
    # reports it as an ordinary spent day — "today's budget is spent" to someone
    # whose mailbox was stopped for a spam complaint an hour ago. It is not a
    # `blocked_reason`: the backlog below is still worth computing and is the
    # answer to "what happens when the hold lifts". So it is said, not returned
    # on.
    plan.send_hold_reason = auto_apply_service.pause_reason(user, now=now)

    targets = profile_service.active_targets(db, user)
    if not targets:
        plan.blocked_reason = "No resume to score and tailor against."
        return plan

    campaign_id = pref.campaign_id
    # Contacts this plan has already spoken for. The live run rejects a second
    # application to the same recruiter, and without this the plan would happily
    # list one company three times for three of its open roles.
    claimed: set[str] = set()

    cap = budget if limit is None else min(budget, max(0, limit))
    cumulative = 0
    spacing = _spacing_seconds()

    considered = 0
    for posting in auto_apply_service.candidate_postings(db, user, pref):
        considered += 1
        if considered > _BACKLOG_SCAN_LIMIT:
            plan.notes.append(
                f"stopped counting after {_BACKLOG_SCAN_LIMIT} matching postings"
            )
            break

        target = profile_service.target_for_posting(db, user, posting, targets)
        if target is None:  # pragma: no cover - targets is non-empty here
            continue

        reason, contact = _gate_reason(
            db, user, posting, target, campaign_id, claimed
        )
        if reason is not None:
            plan.skipped.append(
                SkippedPosting(
                    job_posting_id=posting.id,
                    title=posting.title,
                    company=posting.company,
                    reason=reason,
                )
            )
            continue

        # Cleared every gate. Inside the budget it is a planned send; past it, it
        # is the backlog — which is the honest answer to "why isn't autopilot
        # doing more?" and the number that makes raising the cap a real choice.
        if len(plan.sends) >= cap:
            plan.backlog += 1
            continue

        if contact is None:  # pragma: no cover - a survivor always carries one
            contact = planned_contact(db, user, posting.company)
        if contact.email:
            claimed.add(contact.email.lower())
        cumulative += spacing
        scheduled_at, zone = projected_send_time(
            db,
            contact.email,
            posting.company,
            posting,
            cumulative_seconds=cumulative,
            now=now,
        )
        reasons, recommendation = explain_choice(db, posting, target, pref, now=now)
        account = gmail_accounts.resolve_for_new_outreach(
            db, user, profile_id=target.profile_id
        )

        plan.sends.append(
            PlannedSend(
                job_posting_id=posting.id,
                title=posting.title,
                company=posting.company,
                location=posting.location,
                remote=posting.remote,
                url=posting.url,
                posted_at=posting.posted_at,
                fit_score=posting.fit_score,
                llm_fit_score=posting.llm_fit_score,
                recommendation=recommendation,
                profile_id=target.profile_id,
                profile_label=target.label,
                resume_id=target.resume.id,
                resume_label=_resume_label(target),
                contact=contact,
                from_address=account.email if account else None,
                cover_letter=(
                    (pref.cover_letter_delivery or "inline")
                    if pref.cover_letter_enabled
                    else None
                ),
                scheduled_at=scheduled_at,
                timezone=zone,
                sends_unreviewed=decision.enabled,
                reasons=reasons,
            )
        )

    if budget <= 0:
        plan.notes.append(
            f"today's budget is spent — your cap is {pref.daily_application_limit}/day "
            f"and the mailbox warm-up allows {day_limit}/day"
        )
    if limit is not None and plan.backlog and len(plan.sends) >= cap:
        plan.notes.append(f"showing the first {cap} of today's {budget}")
    if not decision.enabled and plan.sends:
        plan.notes.append(
            "these land in your review queue, not a recruiter's inbox, until "
            "auto-send is clear to run"
        )

    return plan


def _gate_reason(
    db: Session,
    user: User,
    posting: JobPosting,
    target: ScoringTarget,
    campaign_id: int | None,
    claimed: set[str],
) -> tuple[str | None, PlannedContact | None]:
    """Why this posting would be passed over, plus the contact it resolved.

    Returns ``(reason, contact)``. ``reason`` is ``None`` when the posting
    survives; ``contact`` is ``None`` when a gate fired before one was looked up,
    so the caller can reuse the lookup instead of repeating it.

    The gates are the live pipeline's own functions, called with the same
    arguments in the same order — a dry run that reimplemented them would drift
    from the thing it claims to predict on the first edit to any of them.

    That drift had already happened once. ``ghost_gate`` was added to the live
    run and not to this list, so a plan promised applications to postings the
    run then refused, and the user's evidence for what autopilot was about to do
    disagreed with what it did. Every gate the run enforces is enforced here, in
    the same order, and ``test_autopilot_dry_run`` asserts the two lists match.

    The contact checks come last, and only they are new here: the live run
    discovers a contact and *then* finds out it has already written to them, which
    is a wasted crawl it can afford and a plan cannot. Same three refusals, in a
    cheaper order.
    """
    if not posting.company:
        return "posting has no company to find a recruiter for", None

    off_target = auto_apply_service.relevance_gate(
        target.resume, target.targeting, posting
    )
    if off_target is not None:
        return off_target, None
    wrong_place = auto_apply_service.location_gate(target.targeting, posting)
    if wrong_place is not None:
        return wrong_place, None
    not_real = auto_apply_service.ghost_gate(posting)
    if not_real is not None:
        return not_real, None
    constrained = auto_apply_service.constraint_gate(db, target.targeting, posting)
    if constrained is not None:
        return constrained, None

    contact = planned_contact(db, user, posting.company)
    if contact.email and contact.email.lower() in claimed:
        return f"{contact.email} is already getting one of today's emails", contact
    if contact.warning is not None:
        return f"{contact.email}: {contact.warning}", contact
    if (
        contact.email
        and campaign_id is not None
        and _already_contacted_by_email(db, campaign_id, contact.email)
    ):
        return f"{contact.email} already contacted under autopilot", contact
    return None, contact


def _already_contacted_by_email(db: Session, campaign_id: int, email: str) -> bool:
    """Whether the autopilot campaign already has an application to *email*.

    The address rather than the recruiter id, because the plan resolves contacts
    from the cache too — where there is no row to hold an id yet.
    """
    from app.models.application import Application

    return (
        db.scalar(
            select(Application.id)
            .join(Recruiter, Application.recruiter_id == Recruiter.id)
            .where(Application.campaign_id == campaign_id, Recruiter.email == email.lower())
        )
        is not None
    )


def _resume_label(target: ScoringTarget) -> str:
    """What to call this resume in the plan.

    The headline first — "Senior Backend Engineer" identifies a document to its
    owner far better than ``resume-final-v3.pdf`` does — then the filename, then
    the id, so there is always something to point at.
    """
    resume = target.resume
    for value in (resume.headline, resume.filename):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"resume #{resume.id}"


__all__ = [
    "PLAN_WINDOW_HOURS",
    "SOURCE_CACHED",
    "SOURCE_KNOWN",
    "SOURCE_SEARCH",
    "DryRunPlan",
    "PlannedContact",
    "PlannedSend",
    "SkippedPosting",
    "build_plan",
    "explain_choice",
    "planned_contact",
    "projected_send_time",
]
