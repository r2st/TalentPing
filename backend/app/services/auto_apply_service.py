"""The end-to-end auto-apply pipeline — the autopilot spine.

This is what turns TalentPing from a set of tools into an agent. Given a user who
has switched autopilot on (:class:`~app.models.autopilot.AutopilotPreference`), one
run does the whole loop, per roadmap improvement 1:

    discover jobs → score fit → keep only strong matches → tailor the resume →
    write the cover letter → find the best recruiter at that company → write a
    role-specific email → queue the send → schedule follow-ups
    (→ optionally auto-fill the job's form)

Every step reuses an existing, already-tested service, so this module is mostly
orchestration and guardrails:

* **Selectivity** — only postings at/above ``min_fit_score`` are applied to.
* **Scout's veto** — a posting the re-ranker scored below
  ``autopilot_min_llm_fit_score`` is dropped even if the keyword score liked it.
  The second opinion exists to be acted on when it disagrees; for a while it was
  computed, stored, shown in the UI and then ignored by the one code path that
  spends the user's reputation.
* **Relevance** — :func:`relevance_gate` re-checks the posting's title against
  the roles the user actually asked for, immediately before an email is written.
  The fit score is a weighted average, so a strong showing on location, salary
  and seniority can carry an off-target role over the line; this gate cannot be
  outvoted. It is the last thing standing between a bad match and a stranger's
  inbox, which is why it duplicates work the scorer already did.
* **Location** — :func:`location_gate` applies the same logic to *where*. The
  average carries a wrong-continent role just as happily as a wrong-title one,
  and an application to a job the candidate cannot physically take costs them a
  send, a recruiter's time and some of their mailbox's reputation. Remote roles
  pass unconditionally: a remote job is in everyone's preferred location.
* **Which profile** — a posting is applied to under the profile it scored best
  against (:mod:`app.services.profile_service`), and that profile decides the
  resume that gets tailored, the letter that gets written and the roles and
  places both gates are checked against. A candidate holding a backend profile
  and a DevOps profile gets the backend resume on backend roles without having
  to run the product twice.
* **Budget** — the number of new applications per run is the *minimum* of the
  user's daily cap and what the mailbox's warm-up ramp allows today, so autopilot
  can never outrun the reputation guardrails.
* **No double-contact** — a recruiter already emailed under the autopilot campaign
  is skipped; a posting already applied to is skipped; cross-board duplicates of
  a posting are never applied to separately.
* **The letter is delivered the way the user asked** — ``cover_letter_enabled``
  and ``cover_letter_delivery`` are read here, which is the only place the two
  can be honoured: inline means folding the letter into the body the user
  reviews, attachment means recording it on the email for the sender to render
  and attach. Only the first outreach carries one; a follow-up re-attaching the
  same letter is the same letter twice.

Network- and LLM-bound: run it from a Celery worker (see
:mod:`app.tasks.auto_apply_tasks`), never inline in a request.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.application import Application, ApplicationStatus
from app.models.autopilot import AutopilotPreference
from app.models.campaign import AUTOPILOT_CAMPAIGN_NAME, Campaign, CampaignStatus
from app.models.cover_letter import CoverLetter
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.job import JobPosting, JobSearch, JobStatus
from app.models.resume import Resume
from app.models.user import User
from app.services import (
    cover_letter_service,
    fit_scorer,
    ghost_job,
    gmail_accounts,
    profile_service,
    reputation_service,
    role_expansion,
    search_filters,
    send_policy,
    spam_risk,
)
from app.services.ai_composer import (
    CandidateContext,
    JobContext,
    Personalization,
    RecruiterContext,
    compose_job_outreach,
)
from app.services.fit_scorer import Targeting
from app.services.job_search_service import run_search
from app.services.profile_service import ScoringTarget
from app.services.recruiter_discovery import best_recruiter_for_company
from app.services.smart_apply_service import resolve_job, tailor_and_save

logger = logging.getLogger(__name__)


@dataclass
class AutoApplyResult:
    status: str = "ok"
    scanned: int = 0            # postings considered this run
    applied: int = 0            # new applications created
    skipped_low_fit: int = 0
    skipped_irrelevant: int = 0  # right score, wrong line of work
    skipped_location: int = 0    # right work, somewhere they won't take it
    skipped_no_contact: int = 0
    skipped_duplicate: int = 0
    failed: int = 0            # postings that raised and were stepped over
    budget: int = 0            # applications the run was allowed to make
    # Applications made per profile name, so the run can report which of the
    # candidate's several searches actually produced anything.
    applied_by_profile: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """One sentence a user can act on.

        ``notes`` is one line per skipped posting, which for a polluted feed is
        forty near-identical rejections — enough to fill a log line and tell
        nobody anything. This says what happened to the run as a whole.
        """
        if self.status != "ok":
            return self.notes[0] if self.notes else self.status.replace("_", " ")

        parts: list[str] = []
        if self.applied:
            by = ", ".join(f"{n} as {label}" for label, n in self.applied_by_profile.items())
            parts.append(f"applied to {self.applied}" + (f" ({by})" if by else ""))
        for count, phrase in (
            (self.skipped_irrelevant, "weren't in your target roles"),
            (self.skipped_location, "were somewhere you don't work"),
            (self.skipped_low_fit, "scored below your fit threshold"),
            (self.skipped_no_contact, "had no contact to write to"),
            (self.skipped_duplicate, "were companies already contacted"),
            (self.failed, "couldn't be processed"),
        ):
            if count:
                parts.append(f"{count} {phrase}")

        if not parts:
            return (
                "no new postings to consider"
                if not self.scanned
                else f"considered {self.scanned}, nothing new to act on"
            )
        return "; ".join(parts)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "scanned": self.scanned,
            "applied": self.applied,
            "skipped_low_fit": self.skipped_low_fit,
            "skipped_irrelevant": self.skipped_irrelevant,
            "skipped_location": self.skipped_location,
            "skipped_no_contact": self.skipped_no_contact,
            "skipped_duplicate": self.skipped_duplicate,
            "failed": self.failed,
            "budget": self.budget,
            "applied_by_profile": dict(self.applied_by_profile),
            "summary": self.summary,
            # Trimmed hard: the aggregate above is the answer, and these are
            # examples of it rather than the report itself.
            "notes": self.notes[:5],
        }


def _sent_in_last_24h(
    db: Session, user_id: int, account=None, *, now: datetime | None = None
) -> int:
    """Sends in the last 24h, optionally for one mailbox.

    Delegates to :func:`app.services.gmail_accounts.sent_in_last_24h`, which is
    the one place that knows a mailbox is identified by its *address* as well as
    by its row. This used to be a second, independent copy of that query keyed on
    the thread stamp alone, so it over-counted a reconnected mailbox's remaining
    headroom by a full day's allowance — the stamps are blanked when the old row
    is deleted. Only the count is wanted here; the window's oldest send is what
    the per-send gate uses to time its retry.
    """
    count, _oldest = gmail_accounts.sent_in_last_24h(db, user_id, account, now=now)
    return count


def send_headroom(
    db: Session, user: User, *, now: datetime | None = None
) -> tuple[int, int]:
    """How much more outreach this user's mailboxes can carry today, and the cap.

    Each mailbox warms up on its own ledger, so the headroom is the sum of what
    every connected mailbox has left — not the primary's allowance measured
    against every mailbox's sends, which is what made two fresh accounts share a
    single 5/day budget and each blame the other.

    This is **not** rotation for volume. Nothing spreads a campaign across
    mailboxes to send more; routing is per-profile and deterministic, and this
    number only has to describe the mailboxes the run will actually use. The
    per-send reputation gate remains the authority — an over-optimistic budget
    here delays sends, it does not release them.

    "It only delays sends" held while the ramp was the only thing being counted.
    It stopped holding once :mod:`app.services.reputation_service` grew pauses:
    a mailbox held for a spam complaint is stopped for *three days*, and this
    function still handed back its whole warm-up allowance as though the day
    were free. The autopilot run sized its budget off that number and did the
    work — scoring, tailoring a resume, writing a cover letter, each an LLM call
    that costs money — for twenty applications whose messages then sat QUEUED
    until the hold expired. The delay was never the price; the price was a day's
    paid work banked against a mailbox that had just been told to stop sending,
    and a plan panel promising a day of outreach that could not happen.

    So the whole gate is asked, not just the ramp half of it. ``evaluate`` is the
    single authority on whether one more message may leave this mailbox, and a
    mailbox it refuses contributes nothing — which is what a reader of
    "0 headroom" already believed it meant.

    The reported *cap* still sums every mailbox's ramp ceiling, pause or no
    pause. It answers "how big is a full day here?", which a temporary hold does
    not change, and it is what the run's own note quotes back. Naming the pause
    is :func:`pause_reason` 's job, so that "budget used" and "sending is held"
    do not arrive in the same words.
    """
    now = now or datetime.now(UTC)
    total = limit = 0
    for account in gmail_accounts.live_accounts(user):
        day_limit = reputation_service.warmup_day_limit(account, now=now)
        limit += day_limit
        sent = _sent_in_last_24h(db, user.id, account)
        if reputation_service.evaluate(account, sent, now=now).allowed:
            total += max(0, day_limit - sent)
    return total, limit


def pause_reason(user: User, *, now: datetime | None = None) -> str | None:
    """Why sending is held, when a reputation pause is what emptied the budget.

    Only when *every* connected mailbox is paused. One held mailbox out of two
    leaves real headroom on the other, and the run is not blocked — reporting a
    pause then would explain a stoppage that isn't happening.

    Returns the mailbox's own recorded reason, which already names the guardrail
    and the length of the hold ("Paused 3 days — a recipient reported outreach
    as spam"), so the user learns what to stop doing rather than only that
    something stopped.
    """
    accounts = gmail_accounts.live_accounts(user)
    if not accounts:
        return None
    reasons = []
    for account in accounts:
        paused_until = account.paused_until
        if paused_until is None:
            return None
        if paused_until.tzinfo is None:
            paused_until = paused_until.replace(tzinfo=UTC)
        if paused_until <= (now or datetime.now(UTC)):
            return None
        reasons.append(
            account.pause_reason or "Sending is paused to protect your reputation"
        )
    return reasons[0]


def applied_today(db: Session, user_id: int) -> int:
    """Applications created for this user in the last 24h (any source)."""
    since = datetime.now(UTC) - timedelta(hours=24)
    return (
        db.scalar(
            select(func.count(Application.id)).where(
                Application.user_id == user_id, Application.created_at >= since
            )
        )
        or 0
    )


def get_or_create_autopilot_campaign(
    db: Session, user: User, pref: AutopilotPreference, resume: Resume | None
) -> Campaign:
    """The single campaign every auto-applied outreach is filed under.

    Created lazily on the first run and remembered on the preference row, so the
    tracker and dashboard group all autopilot activity together. Reset to ACTIVE
    each run — it may have flipped to COMPLETED when a previous batch drained.
    """
    campaign = db.get(Campaign, pref.campaign_id) if pref.campaign_id else None
    if campaign is None:
        campaign = Campaign(
            user_id=user.id,
            resume_id=resume.id if resume else None,
            name=AUTOPILOT_CAMPAIGN_NAME,
            target_companies=[],
            target_industries=list(pref.target_industries or []),
            target_roles=list(pref.target_roles or []),
            status=CampaignStatus.ACTIVE,
            auto_send=pref.auto_send,
            started_at=datetime.now(UTC),
            follow_up_enabled=pref.follow_up_count > 0,
            follow_up_count=pref.follow_up_count,
            follow_up_interval_days=pref.follow_up_interval_days,
            follow_up_stop_on_reply=pref.follow_up_stop_on_reply,
            follow_up_step_days=(
                list(pref.follow_up_step_days) if pref.follow_up_step_days else None
            ),
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        pref.campaign_id = campaign.id
        db.commit()
    else:
        # Keep the container's send behaviour in sync with the live preferences.
        campaign.auto_send = pref.auto_send
        campaign.resume_id = resume.id if resume else campaign.resume_id
        campaign.follow_up_enabled = pref.follow_up_count > 0
        campaign.follow_up_count = pref.follow_up_count
        campaign.follow_up_interval_days = pref.follow_up_interval_days
        campaign.follow_up_stop_on_reply = pref.follow_up_stop_on_reply
        # Copied every cycle like the rest of the cadence, which is why the
        # column has to exist on the preference row at all: a value set only on
        # the container would be overwritten here by the row that had none.
        campaign.follow_up_step_days = (
            list(pref.follow_up_step_days) if pref.follow_up_step_days else None
        )
        # Anything but a pause, rather than the two statuses this used to name.
        # A container has no failure of its own to report and no run to be
        # mid-way through: every status other than PAUSED is something a *past*
        # batch left behind, and leaving it there is how a container that had
        # once read FAILED — which `routers/campaigns` could produce, and which
        # nothing here reset — stayed failed for every batch after it, never
        # completing and rendering as a failed campaign in the tracker forever.
        #
        # PAUSED is the one status that is a live instruction from the user, so
        # it is the one this must not overwrite.
        if campaign.status is not CampaignStatus.PAUSED:
            campaign.status = CampaignStatus.ACTIVE
            # And the completion date it may be carrying: the container is
            # marked COMPLETED whenever a batch drains, so every re-arm past
            # the first one starts from a row that claims to have finished.
            campaign.completed_at = None
        db.commit()
    return campaign


AUTOPILOT_SEARCH_NAME = "Autopilot search"


def _union(values: list[list[str]], limit: int = 20) -> list[str]:
    """Flatten several lists into one, keeping order and dropping repeats."""
    out: list[str] = []
    seen: set[str] = set()
    for group in values:
        for item in group or []:
            if not isinstance(item, str) or not item.strip():
                continue
            key = item.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(item.strip())
            if len(out) >= limit:
                return out
    return out


def _search_criteria(
    pref: AutopilotPreference, targets: list[ScoringTarget]
) -> tuple[list[str], list[str], str | None, bool]:
    """What the standing search should look for: (roles, keywords, place, remote).

    Every active profile contributes, because the scan is a single sweep feeding
    all of them. Narrowing it to one profile's roles would starve the others of
    postings to be scored against — the feed has to be wide enough for the
    scorer to be selective inside it. Selectivity happens after, per profile.

    The profiles are the authority and the preference row is only the fallback,
    rather than the two being merged. Merging sounds harmless and isn't: a user
    who narrows from "Backend Engineer" to "Data Engineer" would keep being fed
    backend roles forever, because the discarded value survives in whichever
    source wasn't edited. Whoever owns the answer owns it alone.

    ``remote_only`` is only set when *every* profile wants remote work. One
    profile that will take an on-site role in Berlin is a reason to keep on-site
    Berlin postings in the feed; the remote-only profile simply won't win them.
    """
    stated = [t.targeting for t in targets]
    roles = (
        _union([t.roles for t in stated])
        or _union([list(pref.target_roles or [])])
        or _union([list(t.resume.target_roles or []) for t in targets])
    )
    keywords = _union([t.industries for t in stated]) or _union(
        [list(pref.target_industries or [])]
    )
    places = _union([t.locations for t in stated]) or _union([list(pref.locations or [])])
    remote_only = (
        all(t.remote_only for t in stated) if stated else bool(pref.remote_only)
    )
    return roles, keywords, (places[0] if places else None), remote_only


def _expanded_roles(stated: list[str], stored: list[str] | None) -> list[str]:
    """The stated roles plus the other names employers use for them.

    Boards advertise "AI Engineer" as "Machine Learning Engineer" and "LLM
    Engineer"; searching the literal string finds a fraction of the market. The
    expansion widens the **feed** only — :func:`relevance_gate` still judges
    postings against ``targeting.roles``, the words the candidate actually wrote.

    *stored* is the previous expansion. When the stated roles still lead it
    unchanged, that expansion is reused as-is: ``ensure_search`` runs on every
    autopilot tick, and re-deriving synonyms hourly would spend a model call an
    hour to reach the same answer. A profile edit changes the lead and the
    expansion is redone.
    """
    if stored and list(stored[: len(stated)]) == list(stated) and len(stored) > len(stated):
        return list(stored)
    return role_expansion.expand_roles(stated)


def ensure_search(
    db: Session, user: User, pref: AutopilotPreference, targets: list[ScoringTarget]
) -> JobSearch | None:
    """Guarantee the user has a standing search derived from their profiles.

    Autopilot's promise is that the user configures *preferences*, not searches —
    so if they have no active search we synthesise one covering every active
    profile (falling back to their resumes' target roles).

    The search autopilot owns is re-synced on every run. Without that it was
    written once and never touched again: a user who narrowed their target
    roles, raised their threshold, added a profile or switched to remote-only
    kept being fed the old criteria forever, and wondered why the feed still had
    the jobs they had just said they didn't want. A search the *user* built is
    left exactly as they built it — it is their feed, and autopilot's
    selectivity is enforced separately at apply time.
    """
    roles, keywords, place, remote_only = _search_criteria(pref, targets)
    owned = db.scalar(
        select(JobSearch).where(
            JobSearch.user_id == user.id,
            JobSearch.is_active.is_(True),
            JobSearch.name == AUTOPILOT_SEARCH_NAME,
        )
    )
    if owned is not None:
        if roles:
            owned.roles = _expanded_roles(roles, owned.roles)
        owned.keywords = keywords
        owned.location = place
        owned.remote_only = remote_only
        owned.min_fit_score = pref.min_fit_score
        # Deliberately left unpinned: a search pinned to one resume is scored
        # against that resume alone (see job_search_service._scoring_targets),
        # which is the opposite of what autopilot wants from its own feed.
        owned.resume_id = None
        db.commit()
        return owned

    existing = db.scalar(
        select(JobSearch).where(
            JobSearch.user_id == user.id, JobSearch.is_active.is_(True)
        )
    )
    if existing is not None:
        return existing
    if not roles:
        return None

    search = JobSearch(
        user_id=user.id,
        resume_id=None,
        name=AUTOPILOT_SEARCH_NAME,
        roles=_expanded_roles(roles, None),
        keywords=keywords,
        location=place,
        remote_only=remote_only,
        min_fit_score=pref.min_fit_score,
        interval_hours=6,
    )
    db.add(search)
    db.commit()
    db.refresh(search)
    return search


def candidate_postings(
    db: Session, user: User, pref: AutopilotPreference
) -> list[JobPosting]:
    """New, un-applied postings that cleared both scores, best fit first.

    Both scores, because they answer different questions. The deterministic
    ``fit_score`` is the keyword filter; ``llm_fit_score`` is Scout's read on
    trajectory and transferability, and a posting Scout marked down is one it had
    a specific reason to mark down. A null there means Scout never saw the
    posting — the re-rank covers a top-N shortlist per scan — and is not treated
    as a failure.

    Duplicate rows are excluded outright. They are the same role on a second
    board, kept only so a re-scan doesn't resurrect them; applying to one is
    applying to a job we already applied to under a different URL.
    """
    return list(
        db.scalars(
            select(JobPosting)
            .where(
                JobPosting.user_id == user.id,
                JobPosting.status == JobStatus.NEW,
                JobPosting.fit_score.is_not(None),
                JobPosting.fit_score >= pref.min_fit_score,
                JobPosting.applied_at.is_(None),
                JobPosting.duplicate_of_id.is_(None),
                # Already judged and passed over. Re-judging it every hour costs
                # a scan and buys the same answer; the reason is on the row for
                # the user, and clearing it is how changed criteria reopen it.
                JobPosting.screened_out_at.is_(None),
                or_(
                    JobPosting.llm_fit_score.is_(None),
                    JobPosting.llm_fit_score >= settings.autopilot_min_llm_fit_score,
                ),
            )
            .order_by(JobPosting.fit_score.desc(), JobPosting.id.desc())
        )
    )


def relevance_gate(
    resume: Resume, targeting: Targeting, posting: JobPosting
) -> str | None:
    """Veto an auto-application whose title isn't in the candidate's line of work.

    Returns a skip reason, or ``None`` to let the posting through.

    The user's own roles come first — when someone has written down what they
    want, an automated system emailing about something else is the failure mode
    this gate exists for. Falling back to the resume keeps autopilot working for
    users who never filled the field in.

    An untitled posting is refused rather than waved through. Everywhere else in
    this pipeline the safe default is to continue; here it is to stop, because
    the cost of a wrong call is a stranger receiving a misdirected application
    from a real person's mailbox.

    A *blank* stated role is not a stated role. ``target_roles`` is a free-text
    list with no per-item validation — ``[""]`` and ``["  "]`` are both accepted
    by the profile schema — and reading it with a bare truthiness test counted a
    list of blanks as an opinion. Nothing then matched it, because
    :func:`~app.services.fit_scorer.title_relevance` tokenises a blank target to
    nothing and returns 0.0 against every title on earth, so this gate vetoed
    *every* posting under the sentence "'X' is not one of your target roles ()"
    — a reason naming no role, on an autopilot that had silently stopped
    applying to anything.

    It is also a disagreement inside one subsystem.
    :func:`~app.services.fit_scorer.role_targets` drops blanks and falls back to
    the resume, so ``score_role`` scored the same posting 1.0, "is the role
    you're targeting", while this refused to send on it.
    :func:`location_gate` below already reads its list the careful way, and
    :func:`app.services.search_filters.stated` is the shared spelling of the
    question — its own docstring is about this exact trap.
    """
    targets = search_filters.stated(targeting.roles) or fit_scorer.role_targets(resume)
    if not targets:
        return None  # nothing to judge against; the fit score is the only gate

    if not posting.title or not posting.title.strip():
        return "posting has no title to check against your target roles"

    relevance, matched = fit_scorer.best_role_match(posting.title, targets)
    if relevance >= settings.autopilot_min_role_relevance:
        return None
    return (
        f"'{posting.title}' is not one of your target roles "
        f"({', '.join(targets[:3])})"
        + (f"; closest was {matched}" if matched else "")
    )


def ghost_gate(posting: JobPosting) -> str | None:
    """Veto an auto-application to a posting that reads as a ghost.

    Returns a skip reason, or ``None`` to let the posting through.

    The search's own ceiling already keeps high-risk postings out of the feed,
    so this only catches two cases: a posting whose risk climbed *after* it was
    stored — reposted three more times since — and a posting the candidate saved
    by hand, which never passed an ingest gate at all.

    Refuses only at ``LEVEL_GHOST``, not at ``stale``. A stale posting is one a
    candidate might still reasonably choose to chase, and autopilot declining to
    send is a much smaller loss than autopilot declining to *show*.

    An unassessed posting passes. Null risk means we never judged it, and this
    gate is not the place to invent a judgement.
    """
    if posting.ghost_risk is None or posting.ghost_risk < ghost_job.GHOST_AT:
        return None
    why = "; ".join(posting.ghost_reasons or []) or "several ghost-posting signals"
    return f"reads as a ghost posting ({why.lower()})"


def _is_remote(posting: JobPosting) -> bool:
    """Whether the posting is remote, by its flag or by its own location text.

    Boards disagree about which field carries this: some set a remote flag, some
    only ever write "Remote" where the city goes. Both count, because a role
    advertised as remote is one the candidate can take from where they are.
    """
    return posting.remote is True or fit_scorer.looks_remote(posting.location)


def location_gate(targeting: Targeting, posting: JobPosting) -> str | None:
    """Veto an auto-application to somewhere the candidate won't work.

    Returns a skip reason, or ``None`` to let the posting through. The rules, in
    the order they are checked:

    * **Remote passes, always.** A remote role is in everyone's preferred
      location, and this single exemption is what keeps a remote-only candidate's
      pipeline from emptying itself.
    * **Remote-only means remote-only.** Someone who has said they only want
      remote work is not applied to an on-site role on their behalf, whatever
      else the posting has going for it.
    * **A stated location list is binding.** Not a weight, not a nudge — the fit
      score already gave it one, and a weighted average can be outvoted. This
      cannot.
    * **A posting that won't say where it is, isn't applied to.** The same
      posture as the untitled case above: with locations on file, "unknown" is
      not evidence of "acceptable", and the cheap failure is a missed
      application rather than a real send to a role in the wrong country.

    A candidate who has named no locations and hasn't asked for remote-only is
    unaffected — they haven't expressed a constraint, so there is none to
    enforce.
    """
    if not settings.autopilot_enforce_locations:
        return None
    if _is_remote(posting):
        return None

    where = (posting.location or "").strip()
    if targeting.remote_only:
        return (
            "you're only looking for remote work; "
            + (f"'{where}' is on-site" if where else "this posting isn't marked remote")
        )

    wanted = [p for p in (targeting.locations or []) if isinstance(p, str) and p.strip()]
    if not wanted:
        return None  # no stated locations; the fit score is the only gate

    if not where:
        return (
            "the posting doesn't say where it is, and you've set locations "
            f"({', '.join(wanted[:3])})"
        )
    if fit_scorer.location_match(where, wanted) is not None:
        return None
    return f"'{where}' is not one of your locations ({', '.join(wanted[:3])})"


def constraint_gate(db: Session, targeting: Targeting, posting: JobPosting) -> str | None:
    """The three stated constraints, checked in the order they cost least.

    Returns a skip reason, or ``None`` to let the posting through. Wraps
    :mod:`app.services.search_filters` so callers have one call to make and one
    ordering to reason about, rather than three that could be forgotten
    individually — which is exactly how ``ghost_gate`` came to be enforced by
    the live run and not by the plan that claims to predict it.

    Cheapest first: the exclusion list is a set comparison on a name we already
    have, the employment type is a scan of text we already have, and the size
    tier is the only one that touches the database.
    """
    excluded = search_filters.excluded_company_gate(targeting, posting)
    if excluded is not None:
        return excluded
    wrong_kind = search_filters.employment_gate(targeting, posting)
    if wrong_kind is not None:
        return wrong_kind
    if search_filters.stated(targeting.company_sizes):
        bucket = search_filters.company_size(db, posting.company)
        wrong_size = search_filters.company_size_gate(targeting, bucket)
        if wrong_size is not None:
            return wrong_size
    return None


def cover_letter_for_posting(
    db: Session,
    user: User,
    resume: Resume,
    parsed,
    posting: JobPosting,
    tailored: object | None,
    pref: AutopilotPreference | None,
) -> CoverLetter | None:
    """Write (or reuse) the letter this application should go out with.

    The two preference knobs finally meet the pipeline here.
    ``cover_letter_enabled`` decides whether a letter is written at all;
    ``cover_letter_delivery`` decides how it travels, and is stamped onto the row
    so the artifact records how it was actually sent. That stamp matters for an
    edited letter in particular: :func:`~app.services.cover_letter_service.upsert_letter`
    hands those back untouched to protect the user's words, so its stored
    ``delivery`` can predate a preference change, and the live preference is what
    should govern this send.

    Best-effort: a letter that can't be written is a note in the log and an
    application that goes out without one, never a lost application.
    """
    if pref is None or not pref.cover_letter_enabled:
        return None

    delivery = pref.cover_letter_delivery or cover_letter_service.DELIVERY_INLINE
    if delivery not in cover_letter_service.VALID_DELIVERY:
        delivery = cover_letter_service.DELIVERY_INLINE

    try:
        letter = cover_letter_service.upsert_letter(
            db,
            user_id=user.id,
            resume=resume,
            job=parsed,
            job_posting_id=posting.id,
            tailored_resume_id=getattr(tailored, "id", None),
            profile=cover_letter_service.profile_for_posting(db, posting),
            delivery=delivery,
        )
    except Exception:  # noqa: BLE001 - the application matters more than the letter
        logger.warning(
            "cover letter failed for posting %s — applying without one",
            posting.id,
            exc_info=True,
        )
        return None

    letter.delivery = delivery
    return letter


def already_contacted(db: Session, campaign_id: int, recruiter_id: int) -> bool:
    return (
        db.scalar(
            select(Application.id).where(
                Application.campaign_id == campaign_id,
                Application.recruiter_id == recruiter_id,
            )
        )
        is not None
    )


def apply_to_posting(
    db: Session,
    user: User,
    target: ScoringTarget,
    campaign: Campaign,
    posting: JobPosting,
    *,
    auto_send: bool,
    pref: AutopilotPreference | None = None,
    form_autofill: bool = False,
    enforce_gates: bool = True,
) -> tuple[Application | None, str | None]:
    """Run one posting end to end. Returns ``(application, skip_reason)``.

    *target* is the profile this posting matched, with the resume that argues for
    it — everything downstream (tailoring, the letter, the outreach itself) runs
    off that pair rather than off whichever resume happened to be the default.

    Idempotent per posting: a posting that already has an application, or whose
    only contact is one we've already emailed, is skipped with a reason rather
    than double-applied.

    Both gates run first — before the recruiter crawl and before the two LLM
    calls that tailor the resume and write the email. A posting that isn't going
    to be applied to shouldn't cost anything to reject, and putting the checks
    here rather than only in the query means every caller gets them.
    """
    if not posting.company:
        return None, "posting has no company to find a recruiter for"

    resume = target.resume
    if enforce_gates:
        off_target = relevance_gate(resume, target.targeting, posting)
        if off_target is not None:
            return None, off_target
        wrong_place = location_gate(target.targeting, posting)
        if wrong_place is not None:
            return None, wrong_place
        not_real = ghost_gate(posting)
        if not_real is not None:
            return None, not_real
        constrained = constraint_gate(db, target.targeting, posting)
        if constrained is not None:
            return None, constrained

    recruiter, reason = best_recruiter_for_company(db, user, posting.company)
    if recruiter is None:
        return None, reason or "no recruiter found"

    if already_contacted(db, campaign.id, recruiter.id):
        return None, f"{recruiter.email} already contacted under autopilot"

    # Tailor the resume to this posting (heuristic JD parse — a scan can't afford
    # an LLM call per job; the tailorer still runs the LLM for its own prose).
    parsed, _ = resolve_job(db, user, job_posting_id=posting.id, use_llm=False)
    tailored, _ = tailor_and_save(db, user, resume, parsed, posting)

    letter = cover_letter_for_posting(db, user, resume, parsed, posting, tailored, pref)

    cand = CandidateContext.from_resume(
        resume, fallback_name=user.full_name or user.email.split("@")[0]
    )
    composed = compose_job_outreach(
        cand,
        RecruiterContext.from_recruiter(recruiter),
        JobContext(
            title=posting.title,
            company=posting.company,
            location=posting.location,
            remote=posting.remote,
        ),
        Personalization.from_preference(pref),
    )

    # Inline letters are folded in now, not at send time: the body is what the
    # user reviews in the draft queue, and a letter that only appeared on the way
    # out would be a letter nobody approved. Attached letters leave the body
    # alone and ride along as a file — recorded on the email so the sender knows
    # to render one, and so the row says which letter went with which message.
    inline = letter is not None and letter.delivery == cover_letter_service.DELIVERY_INLINE
    body_text = (
        cover_letter_service.fold_into_body(composed.body, letter)
        if inline and letter is not None
        else composed.body
    )

    # What a receiving filter will make of the words, asked before the message is
    # allowed to skip a human. Same rule and same treatment as the campaign
    # pipeline — held for review, never dropped — and the review queue scores the
    # text live, so the user reads the reason next to the draft without anything
    # needing to be stored on the row. See :func:`app.services.spam_risk.screen`
    # for why this path in particular had to stop being the exception.
    auto_send, content = spam_risk.screen(
        composed.subject, body_text, auto_send=auto_send
    )
    if content.should_review:
        logger.info(
            "auto-apply held outreach for review on content risk %d (posting %s)",
            content.risk,
            posting.id,
        )

    application = Application(
        user_id=user.id,
        campaign_id=campaign.id,
        recruiter_id=recruiter.id,
        job_posting_id=posting.id,
        # Recorded so the pipeline can answer "which of my searches sent this?"
        # long after the run that sent it.
        profile_id=target.profile_id,
        status=ApplicationStatus.QUEUED,
    )
    db.add(application)
    db.flush()

    # The profile that matched this posting also decides which address argues for
    # it — a consulting identity and a staff-role identity are the same axis the
    # profile already models. Falls back to the primary, which is what every
    # profile resolves to until a second mailbox exists.
    sending_account = gmail_accounts.resolve_for_new_outreach(
        db, user, profile_id=target.profile_id
    )
    thread = EmailThread(
        application_id=application.id,
        subject=composed.subject,
        gmail_account_id=sending_account.id if sending_account else None,
    )
    db.add(thread)
    db.flush()

    db.add(
        Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.QUEUED if auto_send else EmailStatus.DRAFT,
            to_address=recruiter.email,
            subject=composed.subject,
            body_text=body_text,
            cover_letter_id=None if inline or letter is None else letter.id,
            # See send_policy: the daily ceiling counts unreviewed sends, and
            # after the fact nothing distinguishes one from an approved send.
            auto_sent=auto_send,
        )
    )
    thread.message_count = 1

    if letter is not None:
        # Close the loop the letter model always had a column for: which outreach
        # this letter went out with.
        letter.application_id = application.id

    posting.status = JobStatus.APPLIED
    posting.applied_at = datetime.now(UTC)

    # Best-effort: also try the posting's own application form when enabled and a
    # browser agent is installed. Never fails the outreach — the email is the
    # primary channel; the form is a bonus. Only fills (never auto-submits) here.
    if form_autofill and posting.url:
        _try_form_autofill(posting, resume)

    db.commit()
    db.refresh(application)
    return application, None


def _try_form_autofill(posting: JobPosting, resume: Resume) -> None:
    """Fill the posting's application form if a browser agent is available."""
    from app.services.career_apply_service import ApplicantProfile, autofill_application

    try:
        result = autofill_application(
            posting.url, ApplicantProfile.from_resume(resume), submit=False
        )
        posting.form_apply_status = result.status
        posting.form_apply_note = result.note
    except Exception as exc:  # noqa: BLE001 - the form is a bonus, never a blocker
        logger.debug("form autofill skipped for %s: %s", posting.url, exc)


#: How long a cycle's lease is honoured before it is assumed to belong to a dead
#: process. Derived from the beat interval rather than written as a literal, so
#: the two cannot drift: a run is given two whole sweeps to finish before another
#: is allowed to start over the top of it. A cycle releases its own lease in a
#: ``finally``, so this bound is only ever reached by a worker that was killed
#: rather than stopped — which is not hypothetical here, since the send storm had
#: systemd SIGKILLing the worker every few hours.
_CYCLE_LEASE_SECONDS = 2 * settings.autopilot_scan_interval_seconds


def claim_cycle(
    db: Session, user_id: int, *, now: datetime | None = None
) -> AutopilotPreference | None:
    """Take the one-cycle-per-user lease, or return ``None`` if it is held.

    Two cycles for the same user must never run at once, and three dispatchers
    aim at one: the hourly ``run_all_autopilots`` beat sweep, switching autopilot
    on (``routers.autopilot.update_autopilot``), and ``POST /autopilot/run``
    — which a user can press twice, or press while the beat tick's cycle is
    still crawling.

    Overlapping runs do not merely duplicate work, they double the two numbers
    the user set themselves. ``run_user_autopilot`` reads ``send_headroom`` and
    ``send_policy.remaining_allowance`` once and then applies up to that many
    postings; neither has committed anything when the other reads, so both see a
    full budget and each spends it. A candidate who set the daily cap to 5
    because that is how much unsupervised outreach they were willing to trust
    gets 10. The same reasoning already forced the per-message ceiling inside the
    loop — see the ``allowance`` comment there — and this is the same bug one
    level up, where the loop's own care cannot see it.

    A lease rather than a held row lock: a cycle is minutes of career-page
    crawling and model calls, and a transaction open that long would sit on a
    pooled connection the API needs (``settings.db_pool_size`` and anyio's
    threadpool are sized against each other). The row lock is taken only for the
    read-modify-write of the lease itself, which is what makes *claiming*
    atomic — two dispatchers arriving together cannot both find it free.

    ``SKIP LOCKED`` returns ``None`` rather than blocking: whoever holds the row
    is in the middle of claiming it, so there is nothing useful to wait for.
    Postgres enforces this; SQLite ignores row locking entirely, which is why the
    tests assert on the statement as well as the outcome.
    """
    now = now or datetime.now(UTC)
    pref = db.scalar(
        select(AutopilotPreference)
        .where(AutopilotPreference.user_id == user_id)
        .with_for_update(skip_locked=True)
    )
    if pref is None:
        return None
    # One predicate for "is a cycle running", shared with the read-only caller,
    # so the claim and whatever the UI is told can never disagree about it.
    if cycle_is_running(pref, now=now):
        return None
    if pref.running_since is not None:
        logger.warning(
            "autopilot lease for user %s expired (held since %s); taking it over",
            user_id,
            pref.running_since.isoformat(),
        )
    pref.running_since = now
    db.commit()
    return pref


def cycle_is_running(
    pref: AutopilotPreference | None, *, now: datetime | None = None
) -> bool:
    """Whether a live cycle holds this user's lease.

    Read-only and takes no lock — it answers "should the UI say a run is in
    progress?", not "may I start one?". Only :func:`claim_cycle` can answer the
    second, because only a locked read-modify-write can.

    An expired stamp reads as *not* running, which is the same rule the claim
    applies: past :data:`_CYCLE_LEASE_SECONDS` the process that set it is assumed
    dead, and telling the user a run is still going would be a lie that never
    resolves.
    """
    if pref is None or pref.running_since is None:
        return False
    held = pref.running_since
    if held.tzinfo is None:
        held = held.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - held).total_seconds() < _CYCLE_LEASE_SECONDS


def release_cycle(db: Session, user_id: int) -> None:
    """Give the lease back. Safe to call when it was never taken.

    Best-effort by design: this runs in a ``finally``, and a cycle that already
    failed must not be turned into a second failure by its own cleanup. A lease
    left set because even this could not commit expires on its own after
    :data:`_CYCLE_LEASE_SECONDS`.
    """
    try:
        db.rollback()
        pref = db.scalar(
            select(AutopilotPreference).where(AutopilotPreference.user_id == user_id)
        )
        if pref is not None and pref.running_since is not None:
            pref.running_since = None
            db.commit()
    except Exception:  # noqa: BLE001 - the lease expires on its own
        logger.exception("autopilot lease for user %s could not be released", user_id)


def run_user_autopilot(db: Session, user: User) -> AutoApplyResult:
    """Run one full autopilot cycle for *user*.

    Refreshes their feed, then applies to the strongest new matches up to the
    per-run budget, each under the profile it matched. Records progress and any
    failure on the preference row so the UI can show what the agent last did.

    Serialized per user by :func:`claim_cycle`; a caller that arrives while a
    cycle is already running is told so rather than being allowed to spend the
    same budget a second time.
    """
    if user.autopilot is not None and user.autopilot.is_active:
        if claim_cycle(db, user.id) is None:
            return AutoApplyResult(
                status="already_running",
                notes=["A run is already in progress — this one was skipped"],
            )
        try:
            return _run_user_autopilot(db, user)
        finally:
            release_cycle(db, user.id)
    return _run_user_autopilot(db, user)


def _run_user_autopilot(db: Session, user: User) -> AutoApplyResult:
    """The cycle itself. See :func:`run_user_autopilot` for the lease around it."""
    pref = user.autopilot
    if pref is None or not pref.is_active:
        return AutoApplyResult(status="inactive")
    if not user.gmail_connected:
        return AutoApplyResult(status="no_gmail", notes=["Connect a Gmail account"])

    # A user who has never opened the profile UI still runs on one: their
    # preferences and default resume, written down as the profile the pipeline
    # was already implicitly using.
    profile_service.ensure_default_profile(db, user)
    targets = profile_service.active_targets(db, user)
    if not targets:
        pref.last_run_at = datetime.now(UTC)
        pref.last_error = "No resume to score and tailor against"
        db.commit()
        return AutoApplyResult(status="no_resume", notes=[pref.last_error])

    result = AutoApplyResult()
    pref.last_error = None

    # 1. Refresh the feed from the user's standing search(es). The search is
    # synthesised from every active profile, not just one — a candidate chasing
    # two kinds of work needs both kinds in the feed before either can be scored.
    search = ensure_search(db, user, pref, targets)
    if search is not None:
        try:
            run_search(db, search)
        except Exception as exc:  # noqa: BLE001 - a failed scan still lets us apply to the backlog
            logger.warning(
                "autopilot scan failed for user %s: %s", user.id, exc, exc_info=True
            )
            result.notes.append(f"scan failed: {exc}")

    # 2. Budget: the tighter of the user's daily cap and the warm-up allowance,
    # the latter summed across every connected mailbox — each has its own ramp.
    headroom, day_limit = send_headroom(db, user)
    apply_headroom = max(0, pref.daily_application_limit - applied_today(db, user.id))
    budget = min(headroom, apply_headroom)
    result.budget = budget

    if budget <= 0:
        pref.last_run_at = datetime.now(UTC)
        db.commit()
        result.status = "budget_exhausted"
        # A reputation hold and a spent allowance both end the run with nothing
        # to do, and they ask opposite things of the user: one is "you have used
        # today's sending", the other is "your mailbox is in trouble and you
        # should find out why". Quoting the ramp at someone whose mailbox is
        # paused for a spam complaint tells them the wrong story entirely.
        held = pause_reason(user)
        result.notes.append(
            held
            if held is not None
            else (
                f"daily budget used (cap {pref.daily_application_limit}, "
                f"warm-up {day_limit}/day)"
            )
        )
        return result

    campaign = get_or_create_autopilot_campaign(db, user, pref, targets[0].resume)

    # Whether this run may send unreviewed at all — a pause, an unfinished
    # trial, a ceiling already spent — asked once, because none of those change
    # mid-loop.
    policy = send_policy.evaluate(db, user, pref)
    if not policy.enabled and policy.reason:
        result.notes.append(policy.reason)
    # ...and *how many*, which the yes/no answer cannot say. This used to be the
    # yes/no alone, on the reasoning that nothing in the loop sends so the daily
    # count is fixed until dispatch. That is true and it is exactly the problem:
    # the count being fixed is what let one answer stand for the whole batch, so
    # a user who set the dial to 5 because that is how much unsupervised sending
    # they were willing to trust got a message per posting instead. The ceiling
    # is per message. `outreach_service` reached the same conclusion first and
    # this is the same mechanism, deliberately.
    #
    # None means no ceiling configured, which is the default.
    allowance = send_policy.remaining_allowance(db, user, pref) if policy.enabled else 0
    spent_allowance = False

    # 3. Apply to the strongest matches until the budget runs out, each under the
    # profile that won it at scan time.
    for posting in candidate_postings(db, user, pref):
        if result.applied >= budget:
            break
        result.scanned += 1
        target = profile_service.target_for_posting(db, user, posting, targets)
        if target is None:  # pragma: no cover - targets is non-empty here
            continue
        posting_id = posting.id
        # Decided per posting, and the note is emitted once: the reason is the
        # same for every message past the ceiling, and a copy per posting buries
        # every other note the run produced.
        may_auto_send = policy.enabled and (allowance is None or allowance > 0)
        if policy.enabled and not may_auto_send and not spent_allowance:
            spent_allowance = True
            result.notes.append(
                "Auto-send has used today's allowance — the rest are waiting "
                "for you in the review queue."
            )
        try:
            # One posting must not end the run. `apply_to_posting` crawls a
            # careers page and makes three LLM calls — a company whose site times
            # out, a provider that answers with something unparseable, a job
            # description that breaks the parser — and every one of those used to
            # propagate out of this loop and be caught by the task, which marked
            # the whole run failed. The budget was still unspent and the queue
            # still held better-scoring postings than the one that broke; they
            # simply never got looked at, and the next run started from the same
            # feed and reached the same posting.
            application, reason = apply_to_posting(
                db,
                user,
                target,
                campaign,
                posting,
                auto_send=may_auto_send,
                pref=pref,
                form_autofill=pref.form_autofill_enabled,
            )
        except Exception as exc:  # noqa: BLE001 - one posting must not end the run
            logger.exception("autopilot: posting %s could not be processed", posting_id)
            result.failed += 1
            # Rolled back before anything else touches the session, for two
            # reasons. `apply_to_posting` flushes an application, a thread and an
            # email before it reaches the calls that can fail, so an unwound
            # failure otherwise leaves a half-built outreach that the next commit
            # in this loop would write out — an application with no message, or a
            # message with no body. And a *database* error leaves the session
            # unusable, so without this every remaining posting raises on contact
            # and the run dies anyway, one iteration later.
            db.rollback()
            posting = db.get(JobPosting, posting_id)
            if posting is None:  # pragma: no cover - it was read a moment ago
                continue
            result.notes.append(f"{posting.company}: {exc}"[:1000])
            # Screened out with its reason, exactly as a refusal is. Otherwise a
            # posting that raises deterministically — a description the parser
            # cannot read is the same description tomorrow — is retried by every
            # run forever, paying for the crawl and the LLM calls each time to
            # arrive back here. The reason is on the row, and clearing it is how
            # the user asks for another go.
            posting.screened_out_at = datetime.now(UTC)
            posting.screened_out_reason = f"could not be processed: {exc}"[:1000]
            db.commit()
            continue

        if application is None:
            if reason and "already contacted" in reason:
                result.skipped_duplicate += 1
            elif reason and "target roles" in reason:
                result.skipped_irrelevant += 1
            elif reason and ("your locations" in reason or "remote" in reason):
                result.skipped_location += 1
            else:
                result.skipped_no_contact += 1
            if reason:
                result.notes.append(f"{posting.company}: {reason}")
                # Judged and passed over: record it so the next run spends its
                # scan on postings it hasn't seen, and so the user can be told
                # why this one was left alone.
                posting.screened_out_at = datetime.now(UTC)
                posting.screened_out_reason = reason[:1000]
                db.commit()
            continue

        result.applied += 1
        if may_auto_send and allowance is not None:
            # Only a message that actually went out unreviewed spends the
            # ceiling. A posting parked for review has not, and counting it
            # would make the dial mean "drafts per day".
            allowance -= 1
        result.applied_by_profile[target.label] = (
            result.applied_by_profile.get(target.label, 0) + 1
        )
        # The sequence is deliberately *not* written here. It is anchored on the
        # moment the first touch lands, which the sender knows and this loop does
        # not: `send_outreach_email` calls `schedule_for_application` with the
        # real `sent_at`, and that call is idempotent. Writing it here anchored
        # day 3 to the moment the row was created — which under a review-mode
        # policy is a draft that may sit unapproved for a week, and which then
        # made the whole sequence overdue the instant it was approved.
        db.commit()

    pref.applications_created = (pref.applications_created or 0) + result.applied
    pref.last_run_at = datetime.now(UTC)
    db.commit()

    # 4. Hand the freshly-queued sends to the throttled sender.
    if result.applied and policy.enabled:
        _dispatch_sends(db, campaign.id)

    return result


def _dispatch_sends(db: Session, campaign_id: int) -> None:
    """Queue throttled send tasks for the campaign's pending emails.

    Local import so the service stays testable without a live broker; the
    enqueue helper marks every email QUEUED whether or not dispatch succeeds.
    """
    try:
        from app.tasks.email_tasks import enqueue_campaign_sends

        enqueue_campaign_sends(db, campaign_id)
    except Exception as exc:  # noqa: BLE001 - broker down; emails stay QUEUED
        logger.warning("autopilot send dispatch failed for campaign %s: %s", campaign_id, exc)


__all__ = [
    "AUTOPILOT_SEARCH_NAME",
    "AutoApplyResult",
    "already_contacted",
    "applied_today",
    "apply_to_posting",
    "candidate_postings",
    "cover_letter_for_posting",
    "ensure_search",
    "get_or_create_autopilot_campaign",
    "constraint_gate",
    "ghost_gate",
    "location_gate",
    "pause_reason",
    "relevance_gate",
    "run_user_autopilot",
    "send_headroom",
]
