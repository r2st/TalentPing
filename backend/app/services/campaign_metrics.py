"""Per-campaign effectiveness: did this batch of outreach work, and how fast.

``/analytics/overview`` answers "is my outreach working?" for the whole account
and ranks the *dimensions* underneath it — resume, profile, company, industry,
role, source. The one dimension it has never ranked is the campaign itself,
which is the unit the user actually creates, names, pauses and resumes. So the
question "was the Series-B fintech push worth doing?" was the one question the
analytics page could not answer about the object the product is organised
around.

Three numbers per campaign, and each is here rather than computed in the router
because the router already promises that its figures and the dashboard's cannot
disagree:

**Response rate.** Applications in ``ENGAGED_STATUSES`` over applications sent.
The same numerator and denominator as every other rate on the analytics page —
including a "not interested", because a human writing back is a response and
excluding it would flatter every rate the product reports. Applications that
never got a message out are excluded from the denominator: outreach that was
never sent has not failed to get a reply, it has not been tried, and counting it
turns a paused campaign into a badly-performing one.

**Time to first response.** The median of
:func:`app.services.reply_metrics.reply_latency_days` over the campaign's
threads — first send to *first* reply, never the last, for the reasons that
function's docstring gives. A median rather than a mean because one recruiter
who answers after six weeks should not move the number the user plans around.

**Success rate after outreach.** Applications that reached an interviewing
stage, over applications sent. This is the one the response rate cannot stand in
for: a campaign can be answered by everybody and convert nobody, and that is a
message-content problem rather than a targeting problem. Kept as its own figure
so the two cannot be confused for each other.

Every figure carries the count it was computed from, and rates over fewer than
``MIN_SAMPLE`` applications are marked rather than hidden — same rule, and the
same reason, as the ranked lists in ``routers/analytics``: one application that
happened to reply is a 100% response rate and tells the user nothing, but
removing the row entirely tells them less than showing it captioned.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.application import (
    ENGAGED_STATUSES,
    INTERVIEWING_STATUSES,
    Application,
)
from app.models.campaign import Campaign, CampaignStatus
from app.models.email import EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.services import reply_metrics

#: Below this many *sent* applications, a percentage is noise. Deliberately the
#: same number as ``routers.analytics.MIN_SAMPLE``; imported from here by that
#: module would be a cycle, so it is asserted equal in the tests instead.
MIN_SAMPLE = 3


@dataclass(slots=True)
class CampaignEffect:
    """One campaign's line."""

    campaign_id: int
    name: str
    status: CampaignStatus
    created_at: datetime | None
    #: Applications the campaign created, whether or not anything left.
    applications: int = 0
    #: Of those, the ones with at least one message actually sent. Every rate
    #: below is over this, not over `applications`.
    contacted: int = 0
    responses: int = 0
    interviews: int = 0
    emails_sent: int = 0
    #: Median days from first send to first reply, over threads that got one.
    days_to_first_response: float | None = None

    @property
    def response_rate(self) -> float:
        return _rate(self.responses, self.contacted)

    @property
    def success_rate(self) -> float:
        """Reached an interview stage, of everyone contacted."""
        return _rate(self.interviews, self.contacted)

    @property
    def reliable(self) -> bool:
        """Whether the rates above are worth reading. See MIN_SAMPLE."""
        return self.contacted >= MIN_SAMPLE


def _rate(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def effectiveness(db: Session, user_id: int) -> list[CampaignEffect]:
    """Every campaign this user owns, newest first.

    Two queries and one pass, not a query per campaign: this is rendered beside
    the rest of the analytics page, which is polled.
    """
    campaigns = list(
        db.scalars(
            select(Campaign)
            .where(Campaign.user_id == user_id)
            .order_by(Campaign.id.desc())
        )
    )
    if not campaigns:
        return []

    effects = {
        c.id: CampaignEffect(
            campaign_id=c.id,
            name=c.name,
            status=c.status,
            created_at=_aware(c.created_at),
        )
        for c in campaigns
    }

    applications = list(
        db.scalars(
            select(Application).where(Application.campaign_id.in_(effects))
        )
    )
    if not applications:
        return list(effects.values())

    threads_by_app: dict[int, list[EmailThread]] = defaultdict(list)
    for thread in db.scalars(
        select(EmailThread)
        .where(EmailThread.application_id.in_([a.id for a in applications]))
        .options(selectinload(EmailThread.emails))
    ):
        threads_by_app[thread.application_id].append(thread)

    latencies: dict[int, list[float]] = defaultdict(list)
    for application in applications:
        effect = effects[application.campaign_id]
        effect.applications += 1

        emails = [e for t in threads_by_app.get(application.id, []) for e in t.emails]
        # Only mail that actually left. A draft parked for review is work the
        # user has not done yet, and counting it would let a campaign that has
        # sent nothing report a 0% response rate rather than no rate at all.
        sent = [
            e
            for e in emails
            if e.direction == EmailDirection.SENT and e.status == EmailStatus.SENT
        ]
        effect.emails_sent += len(sent)
        if not sent:
            continue

        effect.contacted += 1
        if application.status in ENGAGED_STATUSES:
            effect.responses += 1
        if application.status in INTERVIEWING_STATUSES:
            effect.interviews += 1

        latency = reply_metrics.reply_latency_days(emails)
        if latency is not None:
            latencies[application.campaign_id].append(latency)

    for campaign_id, values in latencies.items():
        effects[campaign_id].days_to_first_response = round(median(values), 1)

    return list(effects.values())


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; the API renders these as instants."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = ["MIN_SAMPLE", "CampaignEffect", "effectiveness"]
