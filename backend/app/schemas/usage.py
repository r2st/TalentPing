"""Feature-usage schemas — the internal "what do people actually use" read model.

Two audiences and two shapes, kept apart on purpose.

:class:`UsageReportOut` is the administrator's, and is the only place the usage
table is ever rendered. It carries counts and rates, never a user id: the
question it exists for is about the product, and a per-account breakdown would
turn a feature report into a surveillance screen over people who signed up to
find a job.

:class:`ClientEventIn` is the browser's, and is deliberately the smallest thing
that works — a name from a fixed list and a few scalar props. The name is
validated against ``usage_events.CLIENT_FEATURES`` at the route, not here, so
that the refusal message can name the accepted set.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class FeatureUsageOut(BaseModel):
    """One feature's line in the report."""

    name: str
    area: str
    label: str
    events: int = 0
    #: Distinct accounts that used it at all in the window.
    users: int = 0
    #: Of those, the ones who used it on two or more distinct days — the
    #: difference between trying a feature and adopting it.
    repeat_users: int = 0
    #: ``repeat_users`` over ``users``, as a percentage. The adoption funnel in
    #: one number: 40 people opened it, 4 came back.
    retention: float = 0.0
    last_used: date | None = None


class PeriodUsageOut(BaseModel):
    """Whole-product activity for one day or week."""

    period: date
    users: int = 0
    events: int = 0


class UsageReportOut(BaseModel):
    """Everything ``GET /admin/usage`` renders, in one read."""

    since: date
    until: date
    bucket: str
    #: Ranked by event volume, most-used first.
    features: list[FeatureUsageOut] = Field(default_factory=list)
    active: list[PeriodUsageOut] = Field(default_factory=list)
    active_users: int = 0
    #: Names in the vocabulary with no rows at all in the window. The most
    #: useful list on the page, and the one a "top N" ranking can never show:
    #: a feature that is never used produces no row to rank.
    unused: list[str] = Field(default_factory=list)
    #: How many distinct days inside the window count as adoption. Published so
    #: the client can caption `repeat_users` without hard-coding the rule.
    adoption_days: int = 2
    #: False when `USAGE_ANALYTICS_ENABLED` is off on this deployment. Without
    #: it, a switched-off collector and a product nobody uses render identically
    #: — an empty table — which is the failure this whole feature exists to stop
    #: happening to other numbers.
    collecting: bool = True


class ClientEventIn(BaseModel):
    """One browser-observed use of a feature."""

    feature: str = Field(max_length=48)
    #: Small scalars only; the service clips and drops anything larger. Bounded
    #: here as well so an oversized body is refused before it is parsed into
    #: something that has to be walked.
    props: dict[str, str | int | float | bool] = Field(default_factory=dict)


class CampaignEffectOut(BaseModel):
    """One campaign's effectiveness line."""

    campaign_id: int
    name: str
    status: str
    created_at: str | None = None
    applications: int = 0
    #: Applications with at least one message actually sent. Every rate below is
    #: over this rather than over `applications` — outreach that never left has
    #: not failed to get a reply, it has not been tried.
    contacted: int = 0
    responses: int = 0
    interviews: int = 0
    emails_sent: int = 0
    response_rate: float = 0.0
    #: Reached an interview stage, of everyone contacted. Kept apart from the
    #: response rate because a campaign can be answered by everybody and convert
    #: nobody, and that calls for a different fix.
    success_rate: float = 0.0
    #: Median days from first send to first reply. Null when nothing came back.
    days_to_first_response: float | None = None
    #: False when the campaign has contacted too few people for a percentage to
    #: mean anything. The row is still returned — captioning it says more than
    #: hiding it — but a client must not rank on the rates of an unreliable row.
    reliable: bool = False


class CampaignEffectivenessOut(BaseModel):
    campaigns: list[CampaignEffectOut] = Field(default_factory=list)
    #: The sample size below which `reliable` is false.
    min_sample: int = 3


__all__ = [
    "CampaignEffectOut",
    "CampaignEffectivenessOut",
    "ClientEventIn",
    "FeatureUsageOut",
    "PeriodUsageOut",
    "UsageReportOut",
]
