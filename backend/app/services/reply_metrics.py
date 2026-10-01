"""How long a thread took to come back.

Lives in a service rather than in either router because both the dashboard and
the analytics overview publish the number under the same name, and the module
docstring of ``routers/analytics`` promises the two can never disagree. Two
copies of this arithmetic is exactly how they would.
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.models.email import Email, EmailDirection


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; the subtraction below needs tz-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def reply_latency_days(emails: list[Email]) -> float | None:
    """Days from a thread's first outbound message to its *first* reply.

    ``None`` when the thread was never sent or never answered — the caller
    collects these into a median, and a thread with no reply has no latency to
    contribute rather than a zero or an infinity.

    The first reply, emphatically not the last. Taking the newest inbound
    message measures the length of the whole correspondence: a recruiter who
    answers in two days and then exchanges scheduling mail for six weeks was
    recorded as a 44-day reply. The error only grows with how well the
    conversation went, so the metric was worst exactly where the candidate was
    doing best, and the median it fed drifted upward as an account aged.

    Only replies at or after the first send count. A thread whose history was
    imported can hold inbound mail that predates the outreach — that is not a
    reply to it, and reading it as one produces a negative latency.
    """
    first_sent = _aware(
        min(
            (
                e.sent_at
                for e in emails
                if e.direction == EmailDirection.SENT and e.sent_at is not None
            ),
            default=None,
        )
    )
    if first_sent is None:
        return None

    replied_at = min(
        (
            at
            for e in emails
            if e.direction == EmailDirection.RECEIVED
            and (at := _aware(e.sent_at)) is not None
            and at >= first_sent
        ),
        default=None,
    )
    if replied_at is None:
        return None
    return (replied_at - first_sent).total_seconds() / 86400
