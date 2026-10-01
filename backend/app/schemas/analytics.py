"""Analytics schemas — the "is this working?" read model.

The dashboard answers *where every application stands right now*. Analytics
answers a different question: *what is actually producing replies*. Both read
the same rows; only the grouping differs, so nothing here is stored — it is
recomputed per request from applications, emails and campaigns.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class TrendPoint(BaseModel):
    """One bucket of the response-rate-over-time series.

    The cohort is the application's *creation* date, not the reply's: asking
    "of the applications I sent that week, how many came back" is the only
    version of this number that can be compared week to week.
    """

    period: date          # first day of the bucket (week or month)
    label: str            # short human label for the axis
    applications: int = 0
    responses: int = 0
    interviews: int = 0
    response_rate: float = 0.0
    # Mail actually sent for this cohort's applications — the *effort* behind the
    # rate above. A week whose rate fell because less was sent and a week whose
    # rate fell because the same volume stopped working are the same bar without
    # this number, and they call for opposite responses.
    emails_sent: int = 0
    # True while this cohort is still owed replies — its last application went
    # out less than ``AnalyticsOverview.reply_window_days`` ago, so the rate
    # above is a floor rather than a result. Always true of the bucket in
    # progress, which is why the newest column on any cohort chart droops. A
    # client that draws a maturing bucket like a settled one is showing its most
    # alarming number to the users who are working hardest.
    maturing: bool = False


class StatusSlice(BaseModel):
    """One status in the by-status breakdown."""

    status: str
    label: str
    count: int = 0
    share: float = 0.0


class ResumePerformance(BaseModel):
    """How one resume version is doing, across every campaign that used it."""

    resume_id: int | None = None
    label: str
    applications: int = 0
    responses: int = 0
    interviews: int = 0
    response_rate: float = 0.0
    # True for the row with the best response rate among those with enough
    # volume to mean anything — computed server-side so the UI can't disagree.
    is_best: bool = False


class ProfilePerformance(BaseModel):
    """How one search intent is doing — the profile that won the posting.

    Same shape as :class:`ResumePerformance` and a sharper lever than it: a
    resume is a document to swap, a profile is a search to switch off, and
    ``is_active`` is here so the row can say which ones already are.

    ``profile_id`` is null for two different things that read the same on this
    page — outreach sent before the user made any profile, and outreach whose
    profile has since been deleted (the column is ``SET NULL``, deliberately, so
    deleting a profile does not erase what went out under it). Both are honestly
    described as work not attributable to a current search.
    """

    profile_id: int | None = None
    label: str
    # False for a profile the user has already stopped chasing. Shown rather
    # than filtered: "the one you switched off was your best" is the single most
    # useful thing this table can say.
    is_active: bool = True
    applications: int = 0
    responses: int = 0
    interviews: int = 0
    response_rate: float = 0.0
    is_best: bool = False


class SegmentPerformance(BaseModel):
    """A company or industry, ranked by how often it writes back."""

    name: str
    applications: int = 0
    responses: int = 0
    interviews: int = 0
    response_rate: float = 0.0


class Engagement(BaseModel):
    """Opens and clicks across the user's tracked outreach.

    ``tracked`` counts sent emails that actually carried a tracking token, so
    the denominators are never inflated by mail sent before tracking was on.

    Every number here is *approximate* and the UI says so: mail proxies prefetch
    the pixel and corporate gateways block images, so an open is evidence rather
    than proof (docs/features/email-tracking.md §2). Prefetch-flagged events are
    already excluded.
    """

    tracked: int = 0
    opened: int = 0
    clicked: int = 0
    open_rate: float = 0.0
    click_rate: float = 0.0
    # Of the people who opened, how many clicked. The one rate here that image
    # blocking doesn't bias downward, because both sides share the same bias.
    click_to_open_rate: float = 0.0
    # False when the sample is too thin to read anything into.
    reliable: bool = False


class OutreachVolume(BaseModel):
    """How much mail went out, against the applications it went out for.

    Every other rate in this payload is *per application* — the unit the user
    thinks in. That unit hides the work: an application is one row whether it
    took one email or four, so a funnel can look flat while the sending doubled.

    ``emails_per_response`` is the number this block exists for. "How many
    emails does a reply cost me" is the one figure that says whether more
    volume is worth it, and it cannot be derived from a response rate alone.

    ``awaiting_send`` is the honesty term. Applications the pipeline has created
    but not yet mailed sit in the response-rate denominator and can never reply,
    so a queue that has run ahead of the sender reads as outreach that failed.
    Reported separately rather than quietly excluded: the denominator stays the
    one the rest of the page uses, and the user can see what is in it.
    """

    emails_sent: int = 0
    # Applications that received at least one send — the first email each.
    first_touches: int = 0
    # Everything after the first email on an application.
    follow_ups: int = 0
    replies_received: int = 0
    # Sends per response. None when nothing has replied yet: a division by zero
    # dressed up as "∞ emails per reply" is not a number the user can act on.
    emails_per_response: float | None = None
    # Applications with nothing sent yet — queued, or waiting on the throttle.
    awaiting_send: int = 0


class PersistenceStep(BaseModel):
    """One rung of the follow-up ladder."""

    step: int          # 0 is the first email; 1.. are follow-ups
    label: str
    sent: int = 0
    replies: int = 0
    # A *conditional* rate: of the threads still silent when this touch went
    # out, how many answered it. Not a share of all applications — see
    # :mod:`app.services.follow_up_metrics` for why that version misleads.
    reply_rate: float = 0.0
    reliable: bool = False


class Persistence(BaseModel):
    """Whether chasing works, and where it stops working.

    ``replies_after_follow_up`` is the figure this block exists for: it is the
    only number in the payload that says what the reply rate would look like
    with follow-ups switched off. ``suggested_last_step`` is the only advice
    here, and it stays null unless a dead tail has enough sends behind it to be
    evidence rather than a run of bad luck.
    """

    steps: list[PersistenceStep] = Field(default_factory=list)
    contacted: int = 0
    replied: int = 0
    replies_after_follow_up: int = 0
    share_from_follow_ups: float = 0.0
    last_productive_step: int | None = None
    wasted_sends: int = 0
    suggested_last_step: int | None = None
    summary: str = ""
    min_step_sends: int = 5
    min_contacted: int = 10


class SubjectVariantStats(BaseModel):
    """One arm of a campaign's subject-line experiment."""

    id: int
    label: str
    text: str
    sends: int = 0
    opens: int = 0
    replies: int = 0
    open_rate: float = 0.0
    reply_rate: float = 0.0
    is_winner: bool = False
    is_active: bool = True
    generated_with: str = "template"


class SubjectExperiment(BaseModel):
    """A campaign's experiment, with the honesty flag attached."""

    campaign_id: int
    campaign_name: str
    variants: list[SubjectVariantStats] = Field(default_factory=list)
    # True only once every arm has enough impressions for the comparison to
    # mean anything. The UI captions the panel "early" until then.
    confident: bool = False


class DomainBounceStats(BaseModel):
    """How mail to one recipient domain is faring."""

    domain: str
    sent: int = 0
    bounced: int = 0
    hard: int = 0
    soft: int = 0
    rate: float = 0.0
    # Worth telling the user about — e.g. a domain that rejects all outside mail.
    # Nothing is auto-blocked on it; the report informs, the human decides.
    alerting: bool = False


class BounceOverview(BaseModel):
    hard_total: int = 0
    soft_total: int = 0
    suppressed_contacts: int = 0
    domains: list[DomainBounceStats] = Field(default_factory=list)
    min_sample: int = 3


class FocusNote(BaseModel):
    """One plain sentence about where the user's effort is paying off.

    Drawn entirely from the ranked lists already in this payload — no new data,
    no model. The sample size is part of ``text`` rather than something the UI
    has to remember to append, because a claim like "3× better" read without its
    denominator is how a user talks themselves into a decision off four sends.

    The list is empty when nothing clears the evidence bar. That is deliberate:
    silence is a better answer than a hedged sentence about noise.
    """

    kind: str      # industry | company | resume | engagement | volume
    text: str
    # What the sentence is about, for the UI to link or highlight.
    subject: str
    # Applications (or tracked emails) the claim rests on, both sides included.
    sample: int = 0


class CalibrationBand(BaseModel):
    """One fit-score range and how the outreach sent from it fared."""

    label: str
    lower: int
    upper: int  # inclusive
    sends: int = 0
    responses: int = 0
    interviews: int = 0
    response_rate: float = 0.0
    interview_rate: float = 0.0
    # False when the band holds too few sends for its percentage to mean
    # anything. The row is still returned — captioned by the UI, not hidden.
    reliable: bool = False


class FitCalibration(BaseModel):
    """Whether the fit score predicts a reply, graded against outcomes.

    ``verdict`` is the point of the endpoint. A ``flat`` result — high-scoring
    outreach replying no better than low-scoring outreach — is a real finding
    about the scorer, not an empty response, and the UI says so plainly rather
    than showing six near-identical percentages and leaving the reader to
    squint at them.
    """

    bands: list[CalibrationBand] = Field(default_factory=list)
    # Sends that carried a fit score, and sends that could not be attributed to
    # one (company-targeted outreach has no posting behind it). The second is
    # reported so the coverage of the first is legible.
    scored_sends: int = 0
    unscored_sends: int = 0
    responses: int = 0
    interviews: int = 0
    response_rate: float = 0.0

    # Point-biserial correlation between the score and "did they write back",
    # over individual sends. Null when there is no spread to measure.
    correlation: float | None = None
    # separating | flat | inverted | insufficient_data
    verdict: str = "insufficient_data"
    summary: str = ""

    # Offered only when the bands actually separate, and always the *lowest*
    # threshold that beats the account's own baseline — every point above that
    # is outreach the user would stop sending for no measured gain.
    suggested_min_fit_score: int | None = None
    current_min_fit_score: int | None = None

    min_band_sends: int = 3
    min_total_sends: int = 20


class AnalyticsOverview(BaseModel):
    total_applications: int = 0
    responses: int = 0
    interviews: int = 0
    response_rate: float = 0.0
    interview_rate: float = 0.0
    median_days_to_reply: float | None = None
    # How long a cohort needs before its response rate is final — the p90 of
    # this user's own reply latencies, or a default while there are too few
    # replies to have a distribution. Sent so the page can say *why* the newest
    # columns are drawn provisionally rather than just that they are.
    reply_window_days: int = 7

    bucket: str = "week"  # week | month
    trend: list[TrendPoint] = Field(default_factory=list)
    by_status: list[StatusSlice] = Field(default_factory=list)
    resumes: list[ResumePerformance] = Field(default_factory=list)
    # Which search intent is earning the replies. Empty until there is an actual
    # comparison to make — a user with one profile has every application under
    # it, and a table restating the headline response rate is not a finding.
    profiles: list[ProfilePerformance] = Field(default_factory=list)
    companies: list[SegmentPerformance] = Field(default_factory=list)
    industries: list[SegmentPerformance] = Field(default_factory=list)
    # The role applied for, taken from the posting when the outreach targeted
    # one and from the campaign's target roles otherwise. "Senior backend gets
    # answered, staff does not" is a different lever from which company to
    # chase, and it is the one the user can act on this week.
    roles: list[SegmentPerformance] = Field(default_factory=list)
    # Which board the posting came from — remoteok, arbeitnow, a careers page —
    # with everything not tied to a discovered posting grouped as direct
    # outreach. Answers whether the crawlers or the recruiter search is
    # earning the replies, which decides where the next hour goes.
    sources: list[SegmentPerformance] = Field(default_factory=list)

    # Opens and clicks. A 0% reply rate with a 60% open rate and one with a 0%
    # open rate are completely different problems, and until this block existed
    # the product could not tell the user which one they had.
    engagement: Engagement = Field(default_factory=Engagement)

    # Mail volume behind the rates above. Sits next to `engagement` because the
    # two answer adjacent questions — how much was sent, and what happened to it.
    outreach: OutreachVolume = Field(default_factory=OutreachVolume)

    # Whether the follow-ups behind `outreach.follow_ups` earned anything. The
    # volume block says how much chasing happened; this says what it bought,
    # which is the difference between a sequence worth keeping and one that is
    # only costing the sending mailbox its reputation.
    persistence: Persistence = Field(default_factory=Persistence)

    # The ranked lists above, said out loud. Up to three sentences; empty when
    # nothing in the data can carry a claim.
    focus: list[FocusNote] = Field(default_factory=list)

    # Below this many applications a rate is noise, not a signal. The UI uses it
    # to caption the ranked lists rather than inventing its own threshold.
    min_sample: int = 3


class SkillGapRow(BaseModel):
    """One skill the market keeps asking for and a resume does not answer."""

    skill: str
    # Distinct postings that asked for it and did not find it.
    postings: int = 0
    # That count as a share of every posting the user has scored, 0..1. Against
    # everything scored rather than against the postings that ask for this
    # skill — the second number is ~1.0 for every row here and says nothing.
    share: float = 0.0
    # Set only on `already_have`: the resume of theirs that does carry it.
    on_resume_id: int | None = None
    on_resume_label: str | None = None


class SkillsGapOut(BaseModel):
    """`missing_skills`, summed over every posting instead of read one at a time.

    The two lists are different kinds of advice and the UI must not merge them.
    `blocking` is homework — asked for repeatedly, on none of the candidate's
    resumes. `already_have` is one edit — asked for repeatedly, absent from the
    document that got scored, present on another one they own.

    `note` carries the reason the lists are empty when they are, which is a real
    answer rather than a hedge: under `min_scored_postings` there is not enough
    scored to say anything, and saying so beats six invented recommendations.
    """

    scored_postings: int = 0
    blocking: list[SkillGapRow] = Field(default_factory=list)
    already_have: list[SkillGapRow] = Field(default_factory=list)
    note: str = ""

    # The thresholds, so the UI captions the lists with the numbers actually
    # used rather than inventing its own.
    min_scored_postings: int = 0
    min_postings_per_skill: int = 0


class StageVelocityRow(BaseModel):
    """One stage: how long applications sit in it, and how many never leave."""

    status: str
    # Distinct visits — a card dragged back to a stage counts twice.
    entered: int = 0
    moved_on: int = 0
    # Over the visits that *ended*. Null when none have.
    median_days: float | None = None
    slowest_days: float | None = None
    # Live applications sitting here now. Terminal statuses are excluded: a
    # rejection is finished, not stalled.
    stuck: int = 0
    longest_stuck_days: float | None = None
    exit_rate: float = 0.0
    # False when too few visits for the numbers to read as a finding. The row is
    # still returned and captioned, the same way calibration bands are.
    reliable: bool = False


class StageVelocityOut(BaseModel):
    """Where applications stop moving, from the transitions already stored.

    `median_days` and `stuck` describe two different populations and the UI must
    not merge them. The first is over dwells that *ended* — a completed
    observation. The second counts the ones that have not, which are exactly the
    applications the question is about. A stage with a two-day median and forty
    stuck cards is not a fast stage, and a single pooled number would say it was.
    """

    stages: list[StageVelocityRow] = Field(default_factory=list)
    applications: int = 0
    events: int = 0
    # The slowest-exiting reliable stage that still has applications waiting in
    # it. Named once here rather than left for the reader to infer from a list
    # deliberately kept in funnel order.
    worst_stage: str | None = None
    note: str = ""
    min_stage_sample: int = 0
