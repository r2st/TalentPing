"""Campaign model — one autopilot run: a resume aimed at a set of companies.

A campaign is the only thing the user configures, and even that is two fields:
which resume, and which companies (or industries). Everything after that —
finding recruiter contacts, writing the emails, spacing out the sends, watching
for replies — is driven off this row by the autopilot task.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.resume import Resume
    from app.models.user import User


class CampaignStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    DISCOVERING = "DISCOVERING"  # crawling careers pages for contacts
    GENERATING = "GENERATING"    # composing personalized emails
    ACTIVE = "ACTIVE"            # sends queued / in flight
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# Statuses a run may *begin* from, checked by ``outreach_service.run_autopilot``
# before it writes anything.
#
# The three excluded are excluded for three different reasons, and each one was
# a live bug:
#
# * **PAUSED** — the run used to write ``DISCOVERING`` over it unconditionally as
#   its first act, so a pause pressed between the launch and the worker actually
#   picking the task up was erased, ``_paused_mid_run`` then read the run's own
#   status and saw nothing wrong, and the whole batch went out. That is the same
#   failure ``_paused_mid_run`` exists to prevent, surviving in the one window it
#   does not cover: the window *before* the run starts, which under a busy worker
#   is the widest of the three.
# * **ACTIVE** and **COMPLETED** — a campaign that has already queued or finished
#   its sends. Nothing user-facing reaches ``run_autopilot`` in either status
#   (``resume`` answers 409 to both), but the broker does: ``task_acks_late`` and
#   the visibility timeout mean ``start_campaign`` is delivered *at least* once,
#   and a redelivery re-ran the entire pipeline — re-crawling every target and
#   re-composing against a campaign whose mail had already gone.
#
# DISCOVERING and GENERATING are in, and have to be: DISCOVERING is the status
# ``resume`` and ``create`` claim the row with before dispatching, so refusing it
# would refuse every legitimate run. GENERATING is how a run that died mid-flight
# is left, and re-running it is the recovery.
STARTABLE_STATUSES = frozenset(
    {
        CampaignStatus.DRAFT,
        CampaignStatus.DISCOVERING,
        CampaignStatus.GENERATING,
        CampaignStatus.FAILED,
    }
)

# The two campaigns the product creates and owns. Neither is a piece of outreach
# the user configured: they are containers, created on demand to give autopilot's
# applications and inbound recruiter replies somewhere to hang.
#
# Named here rather than in the two services that make them, because the thing
# that most needs to tell them apart is neither of those services — it is
# ``routers/campaigns``, which must not run the cold-outreach pipeline over a
# container. See :attr:`Campaign.is_system`.
AUTOPILOT_CAMPAIGN_NAME = "Autopilot"
INBOUND_CAMPAIGN_NAME = "Inbound recruiter replies"
SYSTEM_CAMPAIGN_NAMES = frozenset({AUTOPILOT_CAMPAIGN_NAME, INBOUND_CAMPAIGN_NAME})


class Campaign(Base, TimestampMixin):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), index=True
    )
    # Which connected mailbox this campaign's outreach argues from. Null means
    # "decide the usual way" — the matched profile's mailbox, else the user's
    # primary (:func:`app.services.gmail_accounts.resolve_for_new_outreach`).
    #
    # Profiles answer the same question for autopilot, where the user never sees
    # a campaign; this column is for the campaigns they *do* create by hand, and
    # it wins over the profile because it is the more specific statement.
    #
    # SET NULL, not CASCADE: disconnecting a mailbox must not delete the campaign
    # that sent from it, along with its applications and their whole history.
    # Resolution falls back on its own when the column empties out.
    gmail_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="SET NULL"), index=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # What the user picked: companies by name, and/or industries to expand into
    # a company list. Everything else is inferred from the resume.
    target_companies: Mapped[list[str]] = mapped_column(JSON, default=list)
    target_industries: Mapped[list[str]] = mapped_column(JSON, default=list)
    target_roles: Mapped[list[str]] = mapped_column(JSON, default=list)

    status: Mapped[CampaignStatus] = mapped_column(
        SAEnum(CampaignStatus, native_enum=False, length=20),
        default=CampaignStatus.DRAFT,
        nullable=False,
    )
    # Autopilot: send without a per-email approval step. False parks emails as
    # DRAFT so the user reviews before anything leaves their mailbox.
    auto_send: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ---- Follow-up sequence ----
    # After the first email, keep nudging on a schedule until the recruiter
    # replies or the sequence runs out. Defaults follow research §2.3: first
    # touch at day 3-5, second at 7-10, a final one around day 14.
    follow_up_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    follow_up_count: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # Days to the *first* nudge. Matches the first step of
    # ``settings.follow_up_step_days`` so the shipped default is one cadence
    # described twice rather than two that disagree — see
    # ``follow_up_service.default_offsets``.
    follow_up_interval_days: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # Explicit day offsets from the initial send, e.g. [3, 7]. Null falls back to
    # ``settings.follow_up_step_days``; ``follow_up_count`` caps how many are
    # used. The interval knob above cannot express "3 then 7" — no integer
    # interval yields that pair under the widening formula it drove, which is
    # why this column exists (docs/features/follow-up-sequences.md).
    follow_up_step_days: Mapped[list[int] | None] = mapped_column(JSON)
    # The one setting nobody should turn off casually: stop the moment they write
    # back. Exposed anyway because a few users run pure drip sequences.
    follow_up_stop_on_reply: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    # NB: the winning subject-line variant is *not* denormalized here. It lives
    # on ``SubjectVariant.is_winner`` alone — assignment already reads every arm
    # (it needs them for the exploring 10%), so a column here would buy no reads
    # while adding a circular FK between these two tables.

    # Live progress, surfaced in the tracker while the pipeline runs.
    companies_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    contacts_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    emails_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="campaigns")
    resume: Mapped[Resume | None] = relationship(back_populates="campaigns")
    applications: Mapped[list[Application]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )

    @property
    def is_system(self) -> bool:
        """True for the containers the product creates, not the user.

        Matched on the name, because that is already the key both containers are
        looked up by — ``recruiter_reply_service._inbound_campaign`` finds its row
        with a name query, and the autopilot one is created with a literal — so a
        column would be a second answer to a question that already has one, and
        one that every existing row would be missing.

        What it guards is a transition, not a field. A container holds no target
        companies and no target industries, so running the cold-outreach pipeline
        over it does not produce outreach: it produces "No target companies or
        industries were provided" and leaves the row FAILED, permanently, because
        nothing resets a container out of FAILED. That was reachable from the
        campaign strip in two clicks — pause the Autopilot row, then press the
        restart the strip offers a paused row — and it took autopilot's whole
        filing cabinet with it.
        """
        return self.name in SYSTEM_CAMPAIGN_NAMES

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Campaign id={self.id} name={self.name!r} status={self.status}>"
