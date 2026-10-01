"""Job postings and saved job searches — the discovery half of Smart Apply.

A :class:`JobPosting` is one role the candidate might apply to. It arrives one of
two ways: pasted in by hand (a URL or the description text) or surfaced by the
monitoring beat task from a :class:`JobSearch`. Either way it is deduplicated per
user on ``fingerprint`` so the same role never shows up twice in the feed.

``fingerprint`` catches the *exact* repeat. It cannot catch the same role
reposted by a second board under a slightly different title, which is why rows
also carry ``duplicate_of_id`` — see :mod:`app.services.job_dedup` for the fuzzy
pass that sets it.

Scoring is cached on the row (``fit_score``) so the feed can be sorted without
re-running the scorer on every read; the full breakdown lives in
:class:`~app.models.fit_score.FitScore`. ``llm_fit_score`` is Scout's re-rank of
the shortlist and is deliberately a separate number, never a correction to the
deterministic one.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.core.enums import FilterEnum
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.profile import Profile
    from app.models.user import User

_NON_WORD = re.compile(r"[^a-z0-9]+")


def job_fingerprint(title: str | None, company: str | None, url: str | None) -> str:
    """Stable dedupe key for a posting.

    Built from title + company rather than the URL alone: aggregators rewrite
    URLs (tracking params, mirrored listings) but the pair is stable. The URL is
    only the fallback for postings with no company attached.
    """
    title_key = _NON_WORD.sub("-", (title or "").lower()).strip("-")
    company_key = _NON_WORD.sub("-", (company or "").lower()).strip("-")
    basis = f"{title_key}|{company_key}" if company_key else (url or title_key)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


class JobStatus(FilterEnum):
    NEW = "NEW"            # surfaced, not yet triaged
    SAVED = "SAVED"        # candidate wants to act on it
    TAILORED = "TAILORED"  # a tailored resume exists for it
    APPLIED = "APPLIED"
    DISMISSED = "DISMISSED"


class JobPosting(Base, TimestampMixin):
    __tablename__ = "job_postings"
    __table_args__ = (
        # The dedupe guarantee the feed relies on: one row per role per user.
        UniqueConstraint("user_id", "fingerprint", name="uq_job_posting_user_fingerprint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Set when the posting came from a saved search rather than a manual paste.
    search_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_searches.id", ondelete="SET NULL"), index=True
    )

    title: Mapped[str | None] = mapped_column(String(500))
    company: Mapped[str | None] = mapped_column(String(255), index=True)
    location: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    salary_text: Mapped[str | None] = mapped_column(String(255))
    # The advertised band, parsed out of ``salary_text`` at ingest so the feed
    # can be filtered in SQL. Null means *not published*, which is the common
    # case and is never the same thing as zero — see
    # :func:`app.services.salary_service.meets_floor`.
    # A single advertised figure sets both ends.
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    remote: Mapped[bool | None] = mapped_column(Boolean)
    # manual | remoteok | arbeitnow | serpapi | ...
    source: Mapped[str | None] = mapped_column(String(50))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, native_enum=False, length=16),
        default=JobStatus.NEW,
        nullable=False,
        index=True,
    )
    # Cached headline score so the feed sorts without re-running the scorer.
    fit_score: Mapped[float | None] = mapped_column(Float)
    # Which of the candidate's profiles this posting scored best against, and so
    # whose resume and cover letter an application would go out with. Null for
    # rows scored before profiles existed, or for a user who has none — both
    # cases fall back to the default resume, exactly as they did before.
    matched_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="SET NULL"), index=True
    )

    # ---- Scout's re-rank ----
    # A second opinion on the shortlist: the deterministic score decides what is
    # worth an LLM call, and this is what the LLM made of it. Kept in its own
    # column rather than folded into ``fit_score`` so the deterministic number
    # stays reproducible and the feed can still sort on it.
    llm_fit_score: Mapped[float | None] = mapped_column(Float)
    llm_reasoning: Mapped[str | None] = mapped_column(Text)

    # ---- Cross-board deduplication ----
    # Set when this row is the same role as another, surfaced by a second board.
    # The canonical row is the one with the most metadata; duplicates stay in the
    # table (so a re-scan doesn't resurrect them) but are hidden from the feed.
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), index=True
    )
    # Every board this role was seen on: [{"source": "remoteok", "url": "..."}].
    # The canonical row collects them so "also on" links survive the merge.
    source_urls: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)

    # ---- Ghost-job signals ----
    # How many times this role has resurfaced with a materially fresher
    # ``posted_at``. Counted on the canonical row across scans; one repost is
    # ordinary hiring, four is a requisition that never closes. See
    # :mod:`app.services.ghost_job`.
    repost_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 0-100 risk that this posting is a ghost, with the sentences that earned
    # it. Null means *never assessed* — rows stored before this shipped — which
    # is not the same as zero and must never be filtered as though it were.
    ghost_risk: Mapped[int | None] = mapped_column(Integer)
    ghost_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)

    # ---- Autopilot screening ----
    # Set when an autopilot gate passed this posting over, with the gate's own
    # sentence. Without it a rejected posting stays NEW forever and is re-fetched,
    # re-judged and re-rejected on every hourly run — in production the same 42
    # rows were re-scanned indefinitely, producing `scanned: 42, applied: 0` and
    # a run report made entirely of repeats.
    #
    # Deliberately not a `JobStatus`. Status is the *candidate's* pipeline
    # (new -> interested -> applied); being passed over by an automated gate is a
    # fact about our judgement, not a stage they moved the job to. Clearing these
    # columns is how changed criteria put a posting back in play.
    # Indexed: the autopilot's candidate query filters `screened_out_at IS NULL`
    # on every run, and without it that degrades to a scan of the whole feed as
    # the screened-out set grows. The index has existed since `c1f7b3a25e94`;
    # declaring it here is what stops it reading as an orphan to a schema diff.
    screened_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    screened_out_reason: Mapped[str | None] = mapped_column(Text)

    # ---- Career-page form auto-apply (Playwright) ----
    # null | filled | submitted | failed | unsupported — the outcome of the last
    # browser-agent attempt on this posting's own application form.
    form_apply_status: Mapped[str | None] = mapped_column(String(20))
    form_apply_note: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()
    matched_profile: Mapped[Profile | None] = relationship(lazy="selectin")
    duplicate_of: Mapped[JobPosting | None] = relationship(
        remote_side=[id], foreign_keys=[duplicate_of_id]
    )

    @property
    def matched_profile_name(self) -> str | None:
        """The label the feed shows on the card — "matched: DevOps Engineer"."""
        return self.matched_profile.name if self.matched_profile else None

    @property
    def ghost_level(self) -> str | None:
        """``ok`` / ``stale`` / ``ghost`` — the badge, derived from the risk.

        Null when the row was never assessed, so the UI can leave the badge off
        entirely rather than claim a posting is fine on no evidence.
        """
        from app.services.ghost_job import level_for

        return None if self.ghost_risk is None else level_for(self.ghost_risk)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<JobPosting id={self.id} {self.title!r} @ {self.company!r}>"


class JobSearch(Base, TimestampMixin):
    """Standing search criteria, re-run by the monitoring beat task."""

    __tablename__ = "job_searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), index=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    location: Mapped[str | None] = mapped_column(String(255))
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Postings below this score are scanned but never surfaced.
    min_fit_score: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    # The mirror of ``min_fit_score`` for ghost risk: postings scoring *above*
    # it are scanned, counted in the run report, and never stored. Defaulted
    # high on purpose — a posting needs several independent signals to get
    # there, because dropping a real job is the one failure worth avoiding.
    max_ghost_risk: Mapped[int] = mapped_column(Integer, default=85, nullable=False)
    # Annual salary floor. A posting whose *advertised* band tops out below this
    # is screened out; a posting that publishes no band is kept. Most employers
    # publish nothing, so the other rule would empty the feed for anyone who set
    # a floor — and the point of the floor is to remove the roles that are
    # provably too junior, not everything that failed to mention money.
    min_salary: Mapped[int | None] = mapped_column(Integer)
    # How often the beat task re-runs this search.
    interval_hours: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    jobs_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<JobSearch id={self.id} name={self.name!r}>"
