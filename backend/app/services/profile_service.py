"""Resolving *which* profile a job belongs to, and what to score it with.

One candidate, several intents. A posting is scored against every active profile
and the best one wins; the winner then decides which resume gets tailored, which
letter gets written, and which name the feed shows on the card. That single rule
is what this module owns, so the scoring pipeline and the auto-apply pipeline
can't drift into disagreeing about who matched what.

Three things worth stating outright:

* **A user with no profiles still works.** :func:`active_targets` falls back to
  the pre-profile arrangement — the autopilot preferences plus the default
  resume, as one anonymous target. Nothing in the pipelines needs to branch on
  whether profiles exist, and the fallback is what the product did before this
  module was written.
* **A profile without a resume is not a scoring target.** There would be nothing
  to match skills against and nothing to send. It stays in the list for the UI to
  show and prompt about, and is skipped here.
* **Ties go to the default.** Two profiles scoring identically on a posting is
  common — they often share a resume and differ only in places or pay — and the
  candidate's own default is a better answer than whichever row the database
  happened to return first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.autopilot import AutopilotPreference
from app.models.job import JobPosting
from app.models.profile import Profile
from app.models.resume import Resume
from app.models.user import User
from app.services.fit_scorer import FitResult, Targeting, score_fit
from app.services.jd_parser import ParsedJob

logger = logging.getLogger(__name__)


@dataclass
class ScoringTarget:
    """One thing a posting can be scored against: an intent plus its document.

    ``profile`` is ``None`` for the fallback target of a user who has no
    profiles, which is why every consumer reads ``profile_id`` rather than
    assuming there is a row.
    """

    resume: Resume
    targeting: Targeting
    profile: Profile | None = None

    @property
    def profile_id(self) -> int | None:
        return self.profile.id if self.profile is not None else None

    @property
    def label(self) -> str:
        return self.profile.name if self.profile is not None else "your default search"


@dataclass
class ProfileMatch:
    """The winning target for one posting, with the score that won it."""

    target: ScoringTarget
    fit: FitResult
    # Every target's score, keyed by profile id (``None`` for the fallback), so
    # callers can persist the full picture rather than only the winner.
    all_scores: dict[int | None, FitResult]


def _default_resume(db: Session, user: User) -> Resume | None:
    """The resume a request falls back to: the default, else the newest."""
    return db.scalar(
        select(Resume)
        .where(Resume.user_id == user.id)
        .order_by(Resume.is_default.desc(), Resume.id.desc())
    )


def list_profiles(db: Session, user: User) -> list[Profile]:
    """Every profile the user owns, oldest first — the order the UI lists them."""
    return list(
        db.scalars(select(Profile).where(Profile.user_id == user.id).order_by(Profile.id))
    )


def resume_for(db: Session, user: User, profile: Profile) -> Resume | None:
    """The document a profile argues with, falling back to the user's default.

    A profile whose resume was deleted keeps working rather than silently
    dropping out of the pipeline — losing a file should not cost the candidate an
    intent they still hold.
    """
    if profile.resume_id is not None:
        resume = db.get(Resume, profile.resume_id)
        if resume is not None and resume.user_id == user.id:
            return resume
    return _default_resume(db, user)


def active_targets(db: Session, user: User) -> list[ScoringTarget]:
    """Everything this user's postings should be scored against, best-first-ish.

    Active profiles that resolve to a resume, in list order. When there are none
    — no profiles at all, all switched off, or no resume anywhere — the single
    legacy target built from the autopilot preferences stands in, so the
    pipelines behave exactly as they did before profiles existed.
    """
    targets: list[ScoringTarget] = []
    for profile in list_profiles(db, user):
        if not profile.is_active:
            continue
        resume = resume_for(db, user, profile)
        if resume is None:
            logger.debug("profile %s has no resume to score with — skipped", profile.id)
            continue
        targets.append(
            ScoringTarget(
                resume=resume, targeting=Targeting.from_profile(profile), profile=profile
            )
        )
    if targets:
        return targets

    resume = _default_resume(db, user)
    if resume is None:
        return []
    pref = user.autopilot
    targeting = (
        Targeting.from_preference(pref) if pref is not None else Targeting()
    )
    return [ScoringTarget(resume=resume, targeting=targeting)]


def target_for_posting(
    db: Session, user: User, posting: JobPosting, targets: list[ScoringTarget] | None = None
) -> ScoringTarget | None:
    """The target a posting was matched to at scan time, if it still applies.

    Falls back to the first available target when the recorded profile has since
    been deleted or switched off — the posting is still worth applying to, it
    just needs a live intent to apply under.
    """
    available = active_targets(db, user) if targets is None else targets
    if not available:
        return None
    if posting.matched_profile_id is not None:
        for target in available:
            if target.profile_id == posting.matched_profile_id:
                return target
    return available[0]


def score_against_targets(
    targets: list[ScoringTarget], job: ParsedJob, *, explain: bool = False
) -> ProfileMatch | None:
    """Score *job* against every target and return the winner.

    The winner is the highest ``overall``; the candidate's default profile breaks
    a tie, and failing that the first target in the list. Returns ``None`` only
    when there is nothing to score against at all.
    """
    if not targets:
        return None

    scores: dict[int | None, FitResult] = {}
    ranked: list[tuple[tuple[float, int, int], ScoringTarget, FitResult]] = []
    for index, target in enumerate(targets):
        fit = score_fit(target.resume, job, targeting=target.targeting, explain=explain)
        scores[target.profile_id] = fit
        # Score first; then the candidate's default profile; then list order.
        # The negated index makes "earlier" the larger value, so one `max` reads
        # the whole rule.
        is_default = int(target.profile is not None and target.profile.is_default)
        ranked.append(((fit.overall, is_default, -index), target, fit))

    _rank, target, fit = max(ranked, key=lambda row: row[0])
    return ProfileMatch(target=target, fit=fit, all_scores=scores)


# Preference field -> the profile field it means the same thing as.
_SYNCED_FIELDS = {
    "target_roles": "target_roles",
    "target_industries": "target_industries",
    "locations": "location_preferences",
    "remote_only": "remote_only",
    "salary_min": "salary_min",
    "resume_id": "resume_id",
}


def synced_profile_fields(edited: set[str]) -> set[str]:
    """The *profile* columns a preference edit writes through to.

    The two forms use different names for the same intent — the preference row
    calls it ``locations``, the profile calls it ``location_preferences`` — so a
    caller asking "did this edit change anything the scorer reads" has to ask
    about the profile's names, not the form's. Public because that caller is the
    autopilot router and the mapping is not its business to know.
    """
    return {_SYNCED_FIELDS[name] for name in edited if name in _SYNCED_FIELDS}


def sync_default_profile(
    db: Session, user: User, pref: AutopilotPreference, edited: set[str] | None = None
) -> Profile | None:
    """Write preference-form targeting through to a single-profile user's profile.

    Two screens can edit the same intent: the setup wizard's preferences form and
    the profile manager. That is one source of truth too many, and the failure it
    produces is silent — the user narrows their roles on the screen they happen
    to be on, the pipeline reads the other one, and the feed keeps serving the
    jobs they just rejected.

    The rule that resolves it: **with one profile, the preferences form is that
    profile.** Editing either edits the same intent, which is what the user
    believes anyway. Once they make a second profile the forms stop meaning the
    same thing — "my roles" is now ambiguous — so the per-profile screen becomes
    the authority and this does nothing. The preference row keeps its own copy
    either way; it is still the fallback for a user who has no profiles at all,
    and still the home of the genuinely global knobs (daily cap, send behaviour,
    follow-up cadence) that were never per-profile.

    *edited* is the set of preference fields this request actually changed, and
    only those are copied. Copying everything looks equivalent and isn't: the
    preferences form PATCHes one knob at a time, so a request that only flips
    ``auto_send`` would carry the row's empty ``locations`` over a profile that
    had places in it. Syncing what was edited writes through a real edit and
    stays out of the way of everything else.

    Returns the profile it wrote to, or ``None`` when it left well alone.
    """
    profiles = list_profiles(db, user)
    if len(profiles) != 1:
        return None

    fields = _SYNCED_FIELDS if edited is None else {
        name: target for name, target in _SYNCED_FIELDS.items() if name in edited
    }
    if not fields:
        return None

    profile = profiles[0]
    for name, target in fields.items():
        value = getattr(pref, name)
        if name == "resume_id":
            # Only ever repointed at a resume that exists and is theirs; a stale
            # id on the preference row must not blank the profile's document.
            resume = db.get(Resume, value) if value is not None else None
            if resume is None or resume.user_id != user.id:
                continue
            value = resume.id
        elif isinstance(value, list):
            value = list(value)
        setattr(profile, target, value)
    db.commit()
    return profile


def set_default(db: Session, user: User, profile: Profile) -> None:
    """Promote *profile* to the user's default, demoting any previous one."""
    db.execute(
        update(Profile)
        .where(Profile.user_id == user.id, Profile.id != profile.id)
        .values(is_default=False)
    )
    profile.is_default = True


def ensure_default_profile(db: Session, user: User) -> Profile | None:
    """Give a user their first profile, built from what they've already told us.

    The same thing the multi-profile migration did for existing accounts, applied
    to accounts created since: their autopilot targeting plus their default
    resume, which together are the search the pipeline was already running. It is
    created the first time anything asks for their profiles, so the management UI
    opens on the search they have rather than on an empty list and a question.

    Returns ``None`` — and creates nothing — when there is nothing to describe
    yet. An empty profile would be a claim the user never made.
    """
    if any(True for _ in list_profiles(db, user)):
        return None

    resume = _default_resume(db, user)
    pref: AutopilotPreference | None = user.autopilot
    roles = list((pref.target_roles if pref else None) or [])
    industries = list((pref.target_industries if pref else None) or [])
    locations = list((pref.locations if pref else None) or [])

    if resume is not None:
        roles = roles or list(resume.target_roles or [])
        industries = industries or list(resume.target_industries or [])
    if resume is None and not (roles or industries or locations):
        return None

    name = next(
        (
            text.strip()[:120]
            for text in (*roles[:1], resume.headline if resume else None)
            if isinstance(text, str) and text.strip()
        ),
        "My profile",
    )
    profile = Profile(
        user_id=user.id,
        resume_id=resume.id if resume is not None else None,
        name=name,
        target_roles=roles,
        target_industries=industries,
        skills=list(resume.skills or []) if resume is not None else [],
        location_preferences=locations,
        remote_only=bool(pref.remote_only) if pref is not None else False,
        salary_min=pref.salary_min if pref is not None else None,
        experience_level=resume.seniority if resume is not None else None,
        is_active=True,
        is_default=True,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


__all__ = [
    "ProfileMatch",
    "ScoringTarget",
    "active_targets",
    "ensure_default_profile",
    "list_profiles",
    "resume_for",
    "score_against_targets",
    "set_default",
    "sync_default_profile",
    "synced_profile_fields",
    "target_for_posting",
]
