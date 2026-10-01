"""One arm of a campaign's subject-line experiment.

Counters live on the row rather than being aggregated from ``emails`` on read:
assignment happens on every compose and convergence is checked on every send, so
both paths want the current totals in one cheap read.

The experiment's unit is the **campaign**. Pooling results across campaigns would
compare a subject written for a fintech backend role against one written for a
design role — and pooling across users would leak one candidate's data into
another's. Neither is done.
"""
from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class SubjectVariant(Base, TimestampMixin):
    __tablename__ = "subject_variants"
    __table_args__ = (
        UniqueConstraint("campaign_id", "label", name="uq_subject_variant_campaign_label"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False
    )

    label: Mapped[str] = mapped_column(String(2), nullable=False)  # A / B / C
    text: Mapped[str] = mapped_column(String(255), nullable=False)

    # False once convergence has picked a winner and retired this arm. Retired
    # arms keep their counters — the experiment's history is the evidence for
    # the choice.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    sends: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    opens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replies: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # llm | template — so the UI can say whether these were written or canned.
    generated_with: Mapped[str] = mapped_column(
        String(16), default="template", nullable=False
    )

    @property
    def open_rate(self) -> float:
        return round(self.opens / self.sends, 3) if self.sends else 0.0

    @property
    def reply_rate(self) -> float:
        return round(self.replies / self.sends, 3) if self.sends else 0.0

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<SubjectVariant {self.label} campaign={self.campaign_id}>"
