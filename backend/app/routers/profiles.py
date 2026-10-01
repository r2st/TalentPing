"""Profile endpoints — the several jobs one candidate would take.

    GET    /profiles              -> list (creating the first one lazily)
    POST   /profiles              -> create
    POST   /profiles/from-resume  -> create, pre-filled from one resume's parse
    GET    /profiles/{id}
    PATCH  /profiles/{id}         -> update any subset, including the switches
    DELETE /profiles/{id}

A profile is intent: what this candidate wants *this* search to find, and which
resume argues for it. The scoring pipeline runs a posting past every active one
and keeps the best; see :mod:`app.services.profile_service`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.patching import reject_nulls
from app.models.gmail_account import GmailAccount
from app.models.profile import Profile
from app.models.resume import Resume
from app.models.user import User
from app.schemas.profile import (
    ProfileCreate,
    ProfileFromResume,
    ProfileOut,
    ProfileUpdate,
)
from app.services import fit_refresh, profile_service, usage_events
from app.services.preference_suggester import suggest_preferences
from app.tasks import job_tasks

router = APIRouter(prefix="/profiles", tags=["profiles"])

# A ceiling, not a product opinion. Every active profile costs a scoring pass per
# posting and can cost an LLM re-rank call per scan, so an unbounded list is a
# way to make one user's scan expensive for everyone. Ten is far past what a real
# job search needs.
_MAX_PROFILES = 10


def _get_owned(db: Session, user: User, profile_id: int) -> Profile:
    profile = db.get(Profile, profile_id)
    if profile is None or profile.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found"
        )
    return profile


def _owned_resume(db: Session, user: User, resume_id: int) -> Resume:
    resume = db.get(Resume, resume_id)
    if resume is None or resume.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found"
        )
    return resume


def _owned_mailbox(db: Session, user: User, account_id: int) -> GmailAccount:
    """The mailbox a profile may be pointed at.

    A revoked grant is refused rather than stored: outreach for this profile
    would silently fall back to the primary, and the screen would go on showing
    an address that never sends.
    """
    account = db.get(GmailAccount, account_id)
    if account is None or account.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gmail account not found"
        )
    if account.status != "connected":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{account.email} needs reconnecting before it can send",
        )
    return account


def _check_room(db: Session, user: User) -> None:
    if len(profile_service.list_profiles(db, user)) >= _MAX_PROFILES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"At most {_MAX_PROFILES} profiles — switch one off instead",
        )


def _ensure_one_default(db: Session, user: User, *, avoid: int | None = None) -> None:
    """Keep exactly one default alive, so "which profile?" always has an answer.

    Called after anything that can remove the current default — a delete, or
    switching the default profile off. Promotes the first remaining profile
    rather than asking the user to choose in the middle of another action.

    *avoid* is the profile the user just demoted. Without it, clearing the
    default on a two-profile account promotes the same row straight back and the
    switch reads as broken. It is only honoured while another profile exists:
    the last profile standing is the default whether it likes it or not, because
    the alternative is a user with profiles and no answer to which one runs.
    """
    profiles = profile_service.list_profiles(db, user)
    if not profiles or any(p.is_default for p in profiles):
        return
    candidates = [p for p in profiles if p.id != avoid] or profiles
    live = [p for p in candidates if p.is_active] or candidates
    live[0].is_default = True


@router.get("", response_model=list[ProfileOut])
def list_profiles(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ProfileOut]:
    """Every profile the user owns.

    A user who has never made one gets their existing search written down as a
    profile here rather than an empty list — the management UI should open on
    what they already have, not on a blank slate and a question. Nothing is
    invented: a user with no resume and no stated targets still gets an empty
    list, because there is nothing yet to describe.

    The only list endpoint that takes no ``limit``, because it is the only one
    with a real ceiling already: ``_check_room`` refuses the write past
    ``MAX_PROFILES``, so the row count is bounded by a rule rather than by a
    page size. It still reports ``X-Total-Count`` so a client can treat every
    list here the same way.
    """
    profile_service.ensure_default_profile(db, user)
    rows = [
        ProfileOut.from_profile(p) for p in profile_service.list_profiles(db, user)
    ]
    response.headers["X-Total-Count"] = str(len(rows))
    response.headers["X-Has-More"] = "false"
    return rows


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: ProfileCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProfileOut:
    _check_room(db, user)
    if payload.resume_id is not None:
        _owned_resume(db, user, payload.resume_id)
    if payload.gmail_account_id is not None:
        _owned_mailbox(db, user, payload.gmail_account_id)

    profile = Profile(
        user_id=user.id, **payload.model_dump(exclude={"is_default"})
    )
    db.add(profile)
    db.flush()
    # The first profile is the default whether or not the caller asked: with one
    # profile, "the default" and "the only one" are the same row, and leaving it
    # unset would strand every fallback path.
    if payload.is_default or len(profile_service.list_profiles(db, user)) == 1:
        profile_service.set_default(db, user, profile)
    usage_events.record(
        db, "profile.created", user_id=user.id, source="blank", profile_id=profile.id
    )
    db.commit()
    # A new intent is a new competitor for every posting already in the feed —
    # see `app.services.fit_refresh`. Without this the profile only affects jobs
    # discovered *after* it was made, which is not what "add a profile" means to
    # anyone who has just made one and gone to look at their feed.
    job_tasks.dispatch_rescore(db, user)
    db.refresh(profile)
    return ProfileOut.from_profile(profile)


@router.post(
    "/from-resume", response_model=ProfileOut, status_code=status.HTTP_201_CREATED
)
def create_from_resume(
    payload: ProfileFromResume,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProfileOut:
    """Create a profile pre-filled from one resume's parse.

    The same extraction the setup wizard pre-fills preferences from, aimed at a
    single document. Uploading a DevOps resume and pressing one button should
    produce a working DevOps profile — the roles, skills, level and location are
    all already in the file, and asking the user to retype them would be asking
    them to do the parser's job.
    """
    _check_room(db, user)
    resume = _owned_resume(db, user, payload.resume_id)
    suggested = suggest_preferences(resume)
    values = suggested.values

    profile = Profile(
        user_id=user.id,
        resume_id=resume.id,
        name=(payload.name or resume.display_label)[:120],
        target_roles=list(values.get("target_roles") or []),
        target_industries=list(values.get("target_industries") or []),
        skills=list(resume.skills or []),
        location_preferences=list(values.get("locations") or []),
        remote_only=bool(values.get("remote_only")),
        salary_min=values.get("salary_min"),
        experience_level=resume.seniority,
        is_active=payload.is_active,
    )
    db.add(profile)
    db.flush()
    if len(profile_service.list_profiles(db, user)) == 1:
        profile_service.set_default(db, user, profile)
    # Same event as the blank create, with `source` telling them apart. Two
    # names would make "how many profiles get made" a sum the report has to know
    # to compute; one name with a prop keeps both the total and the split.
    usage_events.record(
        db, "profile.created", user_id=user.id, source="resume", profile_id=profile.id
    )
    db.commit()
    job_tasks.dispatch_rescore(db, user)
    db.refresh(profile)
    return ProfileOut.from_profile(profile)


@router.get("/{profile_id}", response_model=ProfileOut)
def get_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProfileOut:
    return ProfileOut.from_profile(_get_owned(db, user, profile_id))


@router.patch("/{profile_id}", response_model=ProfileOut)
def patch_profile(
    profile_id: int,
    payload: ProfileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProfileOut:
    profile = _get_owned(db, user, profile_id)
    fields = payload.model_dump(exclude_unset=True)
    # Captured before the pops and mutations below take names out of `fields`.
    edited = set(fields)
    # Every field here is optional so one switch can be PATCHed at a time, which
    # also makes `{"name": null}` and `{"target_roles": null}` expressible. They
    # are not edits — see `app.core.patching` for what they used to cost.
    reject_nulls(Profile, fields)

    if fields.get("resume_id") is not None:
        _owned_resume(db, user, fields["resume_id"])
    # Explicit null is allowed through — it means "go back to the primary".
    if fields.get("gmail_account_id") is not None:
        _owned_mailbox(db, user, fields["gmail_account_id"])

    # Validate the band against whichever ends survive the patch, not just the
    # ones in it: raising only the floor past a stored ceiling is exactly the
    # inversion the create path refuses.
    low = fields.get("salary_min", profile.salary_min)
    high = fields.get("salary_max", profile.salary_max)
    if low is not None and high is not None and high < low:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="salary_max must be at or above salary_min",
        )

    make_default = fields.pop("is_default", None)
    for name, value in fields.items():
        setattr(profile, name, value)
    if make_default:
        profile_service.set_default(db, user, profile)
    elif make_default is False:
        profile.is_default = False

    db.flush()
    _ensure_one_default(db, user, avoid=profile.id if make_default is False else None)
    # *Which* sections get edited is the whole question here — "profile updated"
    # on its own says nothing about whether the salary band, the locations or
    # the mailbox picker are what people come back to change. The field names
    # are the product's own, not the user's text, so this is a list of knobs
    # rather than a copy of the profile.
    usage_events.record(
        db,
        "profile.updated",
        user_id=user.id,
        profile_id=profile.id,
        sections=sorted(edited),
        fields=len(edited),
    )
    db.commit()
    # Only when the edit can actually move a number. `fit_refresh.SCORING_FIELDS`
    # is the list, and renaming a profile is deliberately not on it.
    if fit_refresh.affects_scoring(edited):
        job_tasks.dispatch_rescore(db, user)
    db.refresh(profile)
    return ProfileOut.from_profile(profile)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    profile = _get_owned(db, user, profile_id)
    db.delete(profile)
    db.flush()
    _ensure_one_default(db, user)
    db.commit()
    # Every posting this profile had won is now matched to a profile that does
    # not exist, and scored under targeting nobody holds. Re-run the comparison
    # against what is left.
    job_tasks.dispatch_rescore(db, user)
