"""Application model — the central pipeline entity (user × recruiter × campaign)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.enums import FilterEnum
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.campaign import Campaign
    from app.models.email_thread import EmailThread
    from app.models.follow_up import FollowUp
    from app.models.recruiter import Recruiter
    from app.models.status_event import ApplicationStatusEvent


class ApplicationStatus(FilterEnum):
    QUEUED = "QUEUED"
    OUTREACH_SENT = "OUTREACH_SENT"
    FOLLOW_UP = "FOLLOW_UP"
    REPLIED = "REPLIED"
    INTERESTED = "INTERESTED"
    SCHEDULING = "SCHEDULING"
    INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED"
    OFFER = "OFFER"
    NOT_INTERESTED = "NOT_INTERESTED"
    NO_RESPONSE = "NO_RESPONSE"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    CLOSED = "CLOSED"


# Reaching one of these means the recruiter engaged, wherever it ended up — a
# "not interested" is still a human writing back, and counting it as silence
# would flatter every response rate the product reports.
#
# These live on the model rather than in the one report that first needed them
# because "did this application get a reply?" is a fact about the pipeline, and
# two readers of it that disagree would be a bug nobody notices for months.
ENGAGED_STATUSES = frozenset(
    {
        ApplicationStatus.REPLIED,
        ApplicationStatus.INTERESTED,
        ApplicationStatus.SCHEDULING,
        ApplicationStatus.INTERVIEW_SCHEDULED,
        ApplicationStatus.OFFER,
        ApplicationStatus.NOT_INTERESTED,
    }
)
INTERVIEWING_STATUSES = frozenset(
    {
        ApplicationStatus.SCHEDULING,
        ApplicationStatus.INTERVIEW_SCHEDULED,
        ApplicationStatus.OFFER,
    }
)

# The conversation is finished: the recruiter passed, the contact opted out, the
# outreach was given up on, or the user closed it themselves. Nothing in this
# set is waiting on anybody.
#
# Here for the same reason as the two above. "Is this application still live?"
# is asked by the dashboard (which greys it out of the active count) and by the
# inbox (which must not tell a candidate to chase a company that already said
# no), and two readers that disagreed would put a nudge on a rejection.
CLOSED_STATUSES = frozenset(
    {
        ApplicationStatus.NOT_INTERESTED,
        ApplicationStatus.UNSUBSCRIBED,
        ApplicationStatus.NO_RESPONSE,
        ApplicationStatus.CLOSED,
    }
)


class Application(Base, TimestampMixin):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("campaign_id", "recruiter_id", name="uq_application_campaign_recruiter"),
        # Wider than the plain ``user_id`` index it replaces, and for one
        # reason: this table is the only route from a user to their mail.
        # ``email_threads`` carries no ``user_id`` — ownership is reachable
        # only as ``emails -> email_threads -> applications -> user_id`` — so
        # every inbox read starts by collecting this user's application ids and
        # joining outwards from them.
        #
        # With ``(user_id)`` alone that collection is an index scan followed by
        # a heap fetch per row, because the planner needs ``id`` and the index
        # does not carry it: 1,000 applications cost 209 buffers. With ``id``
        # appended the same lookup is an index-only scan — 6 buffers, no heap
        # fetches — which is also what makes the index-driven join plan
        # cheap enough for the planner to consider it at all. See
        # docs/SCALABILITY.md §2.
        #
        # ``(user_id)`` is a prefix of this index, so every query that used the
        # narrow one still uses this; nothing is left without cover.
        Index("ix_applications_user_id_id", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # No ``index=True``: covered by ``ix_applications_user_id_id`` above, which
    # leads with this column. A second index on ``(user_id)`` alone would be
    # redundant with it and pay write cost on every application row for nothing.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    recruiter_id: Mapped[int] = mapped_column(
        ForeignKey("recruiters.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Set when the outreach targets a specific discovered posting (the auto-apply
    # path). Null for plain company-targeted campaigns. SET NULL so pruning the
    # feed never deletes the application history.
    job_posting_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), index=True
    )
    # Which profile won this posting, and so whose resume and cover letter went
    # out. Recorded on the application because "why did I get pitched as a tech
    # lead here?" is a question the pipeline should be able to answer months
    # later. SET NULL: deleting a profile must not erase what was sent under it.
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[ApplicationStatus] = mapped_column(
        SAEnum(ApplicationStatus, native_enum=False, length=24),
        default=ApplicationStatus.QUEUED,
        nullable=False,
        index=True,
    )

    campaign: Mapped[Campaign] = relationship(back_populates="applications")
    recruiter: Mapped[Recruiter] = relationship(back_populates="applications")
    threads: Mapped[list[EmailThread]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    follow_ups: Mapped[list[FollowUp]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="FollowUp.step",
    )
    # Every stage this application has passed through, oldest first. Deleting
    # the application takes its history with it — the rows describe this
    # pipeline entry and mean nothing without it.
    status_events: Mapped[list[ApplicationStatusEvent]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationStatusEvent.created_at",
    )
