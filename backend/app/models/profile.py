"""Job-search profiles — one candidate, several jobs they'd take.

A resume is a document; a *profile* is an intent. The distinction only became
load-bearing once someone uploaded two resumes: the product could store both, but
everything downstream — the search, the scorer, the auto-apply pipeline — still
ran off exactly one of them plus one global set of preferences. A backend
engineer who would also take a DevOps role got a search shaped by whichever
resume happened to be the default, and outreach that never mentioned the other
half of what they wanted.

A profile bundles the intent with the document:

    name · resume · target roles · skills · locations · salary band · level

so "Backend Engineer, remote or Berlin, €90k+" and "Tech Lead, Berlin only,
€120k+" are two separate things to be matched against, each with the resume that
argues for it. Scoring evaluates a posting against every active profile and keeps
the best; auto-apply then writes with *that* profile's resume and letter. See
:mod:`app.services.profile_service` for the resolution rules.

``is_active`` is a switch rather than a delete: a candidate who stops chasing
tech-lead roles this month wants the profile back in three, and the applications
already filed under it must keep resolving. ``is_default`` marks the one a plain
"which profile?" question resolves to — exactly one per user, maintained the same
way :class:`~app.models.resume.Resume` maintains its own default.

Profiles supersede the targeting fields on
:class:`~app.models.autopilot.AutopilotPreference`, which stay as the fallback
for a user who has never made one and as the home of the settings that are
genuinely global (daily limit, send behaviour, follow-up cadence).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.resume import Resume
    from app.models.user import User


class Profile(Base, TimestampMixin):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The resume this profile argues with. SET NULL rather than CASCADE: deleting
    # a resume should cost the profile its document, not its existence — the
    # roles, locations and salary floor the user typed are still theirs.
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), index=True
    )
    # The mailbox outreach for this profile goes out from. Null means the user's
    # primary, which is what every profile means until someone connects a second
    # account. Profiles already answer "which kind of role am I going after"; for
    # a candidate keeping a consulting identity apart from a staff-role identity,
    # "from which address" is the same question.
    #
    # This routes *new* outreach only. A reply always goes out of the mailbox the
    # recruiter wrote to — see :mod:`app.services.gmail_accounts`.
    gmail_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="SET NULL"), index=True
    )

    # The user's own label — "Backend Engineer", "DevOps", "Tech Lead". Shown on
    # every job card the profile matched, so it is worth them choosing it.
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # ---- Targeting ----
    target_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    target_industries: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Skills this profile leads with. Not the resume's full list: a platform
    # profile and a backend profile can share a resume and still emphasise
    # different halves of it.
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Places this profile will take work in. The gate in auto_apply_service
    # treats these as binding for automatic outreach — see location_gate.
    location_preferences: Mapped[list[str]] = mapped_column(JSON, default=list)
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The salary range, as two ends of a band. Both optional: plenty of people
    # know their floor and nothing else, and a floor alone is the useful half.
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)

    # junior | mid | senior | lead | exec — the level this profile applies at,
    # which is not always the level the resume reads as. Someone stepping up to
    # lead has a senior resume and a lead intent.
    experience_level: Mapped[str | None] = mapped_column(String(50))

    # ---- Constraints (not weights) ----
    # The three criteria a weighted fit score cannot express, because they are
    # not preferences to be outvoted. Each is enforced as a gate in
    # :mod:`app.services.search_filters`, and each empty list means "no opinion".
    #
    # full_time | part_time | contract | internship | temporary. Read off the
    # posting's own words at gate time: no board we ingest from carries this in
    # a field, so a posting that never says which it is passes.
    employment_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    # startup | scaleup | midsize | enterprise — tiers, not headcount buckets.
    # Answered from the company research cache, which is frequently empty; an
    # unresearched employer passes rather than being rejected for our own gap.
    company_sizes: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Employers this profile will not apply to — a current employer, a former
    # one, an agency. Matched on the normalized name, never on a substring.
    excluded_companies: Mapped[list[str]] = mapped_column(JSON, default=list)

    # ---- State ----
    # Off means "not chasing this right now": excluded from scoring and
    # auto-apply, kept for its history and easy to switch back on.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="profiles")
    resume: Mapped[Resume | None] = relationship(back_populates="profiles")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Profile id={self.id} name={self.name!r} active={self.is_active}>"
