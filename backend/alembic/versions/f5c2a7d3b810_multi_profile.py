"""Multiple job-search profiles per user

Creates ``profiles`` — the intent a resume argues for: a name, the roles and
places wanted, a salary band, a level, and the document that backs it. Then links
the three places that need to say *which* profile was involved:

* ``job_postings.matched_profile_id`` — which profile the posting scored best
  against, so the feed can label the card and auto-apply can pick the resume.
* ``fit_scores.profile_id`` — scores are per profile now, because two profiles
  sharing a resume can still disagree about a posting (different places,
  different pay). The cache key widens with it: keyed on the resume alone, the
  second profile's verdict would silently overwrite the first's.
* ``applications.profile_id`` — what actually went out, kept so "why was I
  pitched as a tech lead here?" has an answer months later.

The backfill turns every existing user into a one-profile user. Their autopilot
targeting (roles, industries, locations, remote flag, salary floor) plus their
chosen or default resume becomes a profile named after the first role they were
chasing — which is what the pipeline was already doing implicitly, now written
down where it can be seen and joined by a second. Users with neither preferences
nor a resume get nothing: an empty profile would be a row that claims an intent
they never expressed.

The downgrade is a clean reversal; it drops the profiles and restores the
narrower fit-score key.

Revision ID: f5c2a7d3b810
Revises: c8f1a3d75e02
Create Date: 2026-07-26
"""
import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5c2a7d3b810"
down_revision: str | None = "c8f1a3d75e02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _as_list(value) -> list[str]:
    """A JSON column's value as a list of strings, whatever the driver gave us.

    Postgres hands back a decoded list; SQLite hands back the raw text. Both
    turn up here because the test suite runs on one and production on the other.
    """
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float))]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return _as_list(parsed)
    return []


def _profile_name(roles: list[str], headline: str | None) -> str:
    """What to call the profile we are inventing on the user's behalf.

    Their own words, in preference order: the first role they said they wanted,
    then the headline their resume led with. "My profile" is the last resort —
    honest about being a placeholder, and one edit away from being right.
    """
    for candidate in (*roles[:1], headline):
        text = (candidate or "").strip()
        if text:
            return text[:120]
    return "My profile"


def upgrade() -> None:
    # ---- profiles ------------------------------------------------------------
    op.create_table(
        "profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("resume_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("target_roles", sa.JSON(), nullable=True),
        sa.Column("target_industries", sa.JSON(), nullable=True),
        sa.Column("skills", sa.JSON(), nullable=True),
        sa.Column("location_preferences", sa.JSON(), nullable=True),
        sa.Column(
            "remote_only", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("experience_level", sa.String(length=50), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"], ondelete="SET NULL"),
    )
    op.create_index(op.f("ix_profiles_user_id"), "profiles", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_profiles_resume_id"), "profiles", ["resume_id"], unique=False
    )

    # ---- links ---------------------------------------------------------------
    op.add_column(
        "job_postings", sa.Column("matched_profile_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        op.f("ix_job_postings_matched_profile_id"),
        "job_postings",
        ["matched_profile_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_job_postings_matched_profile_id",
        "job_postings",
        "profiles",
        ["matched_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("applications", sa.Column("profile_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_applications_profile_id"), "applications", ["profile_id"], unique=False
    )
    op.create_foreign_key(
        "fk_applications_profile_id",
        "applications",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("fit_scores", sa.Column("profile_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_fit_scores_profile_id"), "fit_scores", ["profile_id"], unique=False
    )
    op.create_foreign_key(
        "fk_fit_scores_profile_id",
        "fit_scores",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Widen the cache key so a second profile's verdict gets its own row.
    with op.batch_alter_table("fit_scores") as batch:
        batch.drop_constraint("uq_fit_score_resume_jd", type_="unique")
        batch.create_unique_constraint(
            "uq_fit_score_resume_jd_profile", ["resume_id", "jd_hash", "profile_id"]
        )

    _backfill_default_profiles()


def _backfill_default_profiles() -> None:
    """Give every existing user the profile they were already implicitly running.

    One row per user, built from their autopilot targeting and whichever resume
    the pipeline would have picked anyway (the one named on the preferences, else
    their default, else their newest). Nothing is invented: a user with no
    preferences and no resume is skipped, and the app creates a profile for them
    the first time they have something to put in it.
    """
    bind = op.get_bind()

    prefs = {
        row.user_id: row
        for row in bind.execute(
            sa.text(
                "SELECT user_id, resume_id, target_roles, target_industries, "
                "locations, remote_only, salary_min "
                "FROM autopilot_preferences"
            )
        )
    }
    resumes: dict[int, list] = {}
    for row in bind.execute(
        sa.text(
            "SELECT id, user_id, headline, skills, target_roles, target_industries, "
            "seniority, is_default FROM resumes ORDER BY user_id, id DESC"
        )
    ):
        resumes.setdefault(row.user_id, []).append(row)

    user_ids = sorted({*prefs, *resumes})
    for user_id in user_ids:
        pref = prefs.get(user_id)
        owned = resumes.get(user_id, [])

        resume = None
        if pref is not None and pref.resume_id is not None:
            resume = next((r for r in owned if r.id == pref.resume_id), None)
        if resume is None:
            resume = next((r for r in owned if r.is_default), None) or (
                owned[0] if owned else None
            )

        roles = _as_list(pref.target_roles) if pref is not None else []
        industries = _as_list(pref.target_industries) if pref is not None else []
        locations = _as_list(pref.locations) if pref is not None else []
        remote_only = bool(pref.remote_only) if pref is not None else False
        salary_min = pref.salary_min if pref is not None else None

        if resume is not None:
            roles = roles or _as_list(resume.target_roles)
            industries = industries or _as_list(resume.target_industries)

        # Nothing to say about this user yet — no resume, no stated targets.
        # A row here would be an intent they never expressed.
        if resume is None and not (roles or industries or locations):
            continue

        bind.execute(
            sa.text(
                "INSERT INTO profiles (user_id, resume_id, name, target_roles, "
                "target_industries, skills, location_preferences, remote_only, "
                "salary_min, salary_max, experience_level, is_active, is_default) "
                "VALUES (:user_id, :resume_id, :name, :target_roles, "
                ":target_industries, :skills, :location_preferences, :remote_only, "
                ":salary_min, NULL, :experience_level, :is_active, :is_default)"
            ),
            {
                "user_id": user_id,
                "resume_id": resume.id if resume is not None else None,
                "name": _profile_name(
                    roles, resume.headline if resume is not None else None
                ),
                "target_roles": json.dumps(roles),
                "target_industries": json.dumps(industries),
                "skills": json.dumps(
                    _as_list(resume.skills) if resume is not None else []
                ),
                "location_preferences": json.dumps(locations),
                "remote_only": remote_only,
                "salary_min": salary_min,
                "experience_level": resume.seniority if resume is not None else None,
                "is_active": True,
                "is_default": True,
            },
        )


def downgrade() -> None:
    with op.batch_alter_table("fit_scores") as batch:
        batch.drop_constraint("uq_fit_score_resume_jd_profile", type_="unique")
        batch.create_unique_constraint("uq_fit_score_resume_jd", ["resume_id", "jd_hash"])

    op.drop_constraint("fk_fit_scores_profile_id", "fit_scores", type_="foreignkey")
    op.drop_index(op.f("ix_fit_scores_profile_id"), table_name="fit_scores")
    op.drop_column("fit_scores", "profile_id")

    op.drop_constraint("fk_applications_profile_id", "applications", type_="foreignkey")
    op.drop_index(op.f("ix_applications_profile_id"), table_name="applications")
    op.drop_column("applications", "profile_id")

    op.drop_constraint(
        "fk_job_postings_matched_profile_id", "job_postings", type_="foreignkey"
    )
    op.drop_index(
        op.f("ix_job_postings_matched_profile_id"), table_name="job_postings"
    )
    op.drop_column("job_postings", "matched_profile_id")

    op.drop_index(op.f("ix_profiles_resume_id"), table_name="profiles")
    op.drop_index(op.f("ix_profiles_user_id"), table_name="profiles")
    op.drop_table("profiles")
