"""One row per thing a person did with this product.

The application already records what *happened to* a user — an email left, a
reply landed, an application changed stage — across a dozen tables, and
:mod:`app.core.events` writes the operational half of that to the log. None of
it answers the question the product team actually asks, which is what people
*use*: how many accounts have ever opened Smart Apply, how many opened it twice,
which filters on the job feed are worth the code that maintains them, and
whether the "Why this score" panel is read or scrolled past.

That question cannot be answered from the outcome tables. They record the
pipeline's own work, so a feature nobody touches and a feature everybody touches
but which produces nothing look identical in them — both are an absence of rows.
And it cannot be answered from the log, because a log is a stream with a
retention window measured in days and no way to ask "how many *distinct* users".

So: one narrow, append-only table, written from the handful of call sites that
represent a deliberate act, and read by exactly one screen. Deliberately not an
analytics platform. There is no session id, no funnel definition language and no
event router — the vocabulary is a frozen set in
:mod:`app.services.usage_events` and the report is one query.

Four decisions are load-bearing:

**No third party.** Nothing here leaves the deployment. A hosted analytics SDK
on the frontend would answer the same questions and would also hand a job
seeker's browsing of job postings to an ad network, which is not a trade this
product gets to make on its users' behalf.

**``user_id`` survives its user.** ``SET NULL`` rather than ``CASCADE``. Every
other table here cascades, because every other table describes work done *for*
one account and is meaningless without it. This one describes the product, and
deleting an account must not retroactively rewrite last quarter's adoption
figures. The row keeps its feature and its day; it loses only the ability to be
counted toward a distinct-user total, which is the correct thing to lose.

**``day`` is stored, not derived.** It is ``occurred_at``'s UTC date and nothing
more. Every read of this table groups by day, and there is no spelling of "the
date part of a timestamptz" that is both indexable and portable between
Postgres and the SQLite the suite runs on — ``date(x)`` means different things,
and ``CAST(x AS DATE)`` in SQLite is a numeric cast that silently yields 0. A
denormalised column is smaller than the bug that alternative ships.

**``props`` is small, typed loosely and clipped hard.** It carries the two or
three numbers that make an event answerable ("how many results did that search
return?"), never a document. :func:`app.services.usage_events.record` is the
only writer and it is what enforces that; see its docstring for the size rules
and for what is deliberately never put in here.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.database import Base


class FeatureEvent(Base):
    """A single use of a single feature. Written once, never updated."""

    __tablename__ = "feature_events"
    __table_args__ = (
        # The report's only shape: one feature, over a window of days. Feature
        # first because every query names one or filters to a set of them, and
        # the day range is a range scan inside that.
        Index("ix_feature_events_feature_day", "feature", "day"),
        # The prune, and the per-day totals across all features.
        Index("ix_feature_events_day", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Null once the account is gone. See the module docstring — this is the one
    # table here that outlives its user on purpose.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # A member of `usage_events.FEATURE_NAMES`. Stored as text rather than an
    # enum because the vocabulary grows every release and a migration per new
    # event name is the cost that stops anyone adding one.
    feature: Mapped[str] = mapped_column(String(48), nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: ``occurred_at``'s UTC date, denormalised for grouping. See the docstring.
    day: Mapped[date] = mapped_column(Date, nullable=False)

    #: A handful of small scalars. Never free-form user content beyond the
    #: search terms the user typed into this product's own filters.
    props: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<FeatureEvent {self.feature} user={self.user_id} on={self.day}>"


__all__ = ["FeatureEvent"]
