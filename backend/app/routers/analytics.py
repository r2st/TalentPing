"""Application analytics — what is actually producing replies.

    GET /analytics/overview  -> trend, status mix, volume, resume + segment performance
    GET /analytics/campaigns -> per-campaign response rate, latency, conversion
    POST /analytics/usage    -> record one browser-observed feature use

The dashboard reports state; this reports *effect*. Everything is derived from
the same applications the pipeline shows, so the two can never disagree about
how many there are — they only group them differently.

Two rules shape every number here:

* **Cohort by send date, not reply date.** A week's response rate is "of the
  applications created that week, how many came back". Bucketing by reply date
  makes a quiet week look like a collapse in performance when it is really just
  a week where nothing was sent.
* **Rates below ``MIN_SAMPLE`` are reported but never ranked first.** One
  application to one company that happened to reply is a 100% response rate and
  tells the user nothing. The ordering puts volume-backed rows on top and the
  UI captions the rest.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from math import ceil
from statistics import median

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import MAX_DB_INT
from app.core.rate_limit import rate_limit
from app.models.application import (
    ENGAGED_STATUSES,
    INTERVIEWING_STATUSES,
    Application,
    ApplicationStatus,
)
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_bounce import BounceKind, EmailBounce
from app.models.email_event import EmailEvent, EmailEventType
from app.models.email_thread import EmailThread
from app.models.job import JobPosting
from app.models.profile import Profile
from app.models.recruiter import DeliveryState, Recruiter
from app.models.resume import Resume
from app.models.user import User
from app.schemas.analytics import (
    AnalyticsOverview,
    BounceOverview,
    CalibrationBand,
    DomainBounceStats,
    Engagement,
    FitCalibration,
    FocusNote,
    OutreachVolume,
    Persistence,
    PersistenceStep,
    ProfilePerformance,
    ResumePerformance,
    SegmentPerformance,
    SkillGapRow,
    SkillsGapOut,
    StageVelocityOut,
    StageVelocityRow,
    StatusSlice,
    SubjectExperiment,
    SubjectVariantStats,
    TrendPoint,
)
from app.schemas.usage import (
    CampaignEffectivenessOut,
    CampaignEffectOut,
    ClientEventIn,
)
from app.services import (
    bounce_service,
    campaign_metrics,
    fit_calibration,
    follow_up_metrics,
    reply_metrics,
    skills_gap,
    stage_velocity,
    subject_ab_service,
    usage_events,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])

# Shared with the calibration report and the digest — see models/application.py
# for why they live there rather than here.
ENGAGED = ENGAGED_STATUSES
INTERVIEWING = INTERVIEWING_STATUSES

# How each status reads in the by-status breakdown. Kept in the pipeline's
# order so the chart is a funnel top to bottom rather than an alphabet.
STATUS_LABELS: list[tuple[ApplicationStatus, str]] = [
    (ApplicationStatus.QUEUED, "Queued"),
    (ApplicationStatus.OUTREACH_SENT, "Sent"),
    (ApplicationStatus.FOLLOW_UP, "Followed up"),
    (ApplicationStatus.REPLIED, "Replied"),
    (ApplicationStatus.INTERESTED, "Interested"),
    (ApplicationStatus.SCHEDULING, "Scheduling"),
    (ApplicationStatus.INTERVIEW_SCHEDULED, "Interview"),
    (ApplicationStatus.OFFER, "Offer"),
    (ApplicationStatus.NOT_INTERESTED, "Passed"),
    (ApplicationStatus.NO_RESPONSE, "No reply"),
    (ApplicationStatus.UNSUBSCRIBED, "Opted out"),
    (ApplicationStatus.CLOSED, "Closed"),
]

# Fewer applications than this and a percentage is noise.
MIN_SAMPLE = 3

# How outreach that never came off a board is labelled in the source ranking.
DIRECT_SOURCE = "Direct outreach"

# How long a cohort needs before its response rate means anything.
#
# The trend buckets applications by *when they were sent*, which is the only
# honest way to ask "is my outreach still landing?" — but it has one cost, and
# it lands on the right-hand edge of every chart. Mail sent on Friday cannot
# have been replied to by Saturday, so the newest bucket always reports a rate
# somewhere below its eventual one, and a user who applied hard this week reads
# the resulting cliff as "my outreach has stopped working" when what it says is
# "the replies have not arrived yet". It is the most alarming and least true
# thing the page can show, and it shows it to exactly the users who are working
# hardest.
#
# So each bucket carries whether enough time has passed for its number to be
# final, measured against how long *this user's* replies actually take.
DEFAULT_REPLY_WINDOW_DAYS = 7
# Below this many observed replies the user's own latency is a coincidence, not
# a distribution, and the default stands in.
MIN_LATENCY_SAMPLE = 5
# One recruiter who answered after four months must not grey out the whole
# chart, so the window is capped well short of the p90 it is drawn from.
MAX_REPLY_WINDOW_DAYS = 30

# How outreach that belongs to no current profile is labelled. Two different
# histories land here — mail sent before the user made any profile, and mail
# whose profile has since been deleted — and the page cannot tell them apart,
# so it says the one true thing about both. Phrased to match
# ``profile_service.ScoringTarget.label``, which is what the pipelines call the
# no-profile fallback everywhere else in the product.
NO_PROFILE = "Your default search"


def _role_key(title: str) -> str:
    """Collapse the spellings one job title arrives in, into a grouping key.

    Boards write the same role as "Senior Backend Engineer", "senior backend
    engineer" and "SENIOR BACKEND ENGINEER". Left alone that is three rows with
    a third of the sample each — which is precisely how a real signal ends up
    ranked below the noise floor by ``MIN_SAMPLE``.

    Grouping used to happen on ``.title()``-ing the shouted spellings and
    leaving mixed-case ones alone, which merged the three only while the title
    held no acronym and no numeral. It holds one more often than not: "Senior QA
    Engineer" title-cases to "Senior Qa Engineer" and "Software Engineer III" to
    "Software Engineer Iii", neither of which is ever equal to the mixed-case
    spelling sitting beside it. The commonest titles in the feed — QA, ML, AI,
    SRE, UX, iOS, and every levelled ladder — were exactly the ones that stayed
    split, and split is where ``MIN_SAMPLE`` buries them.

    So the key folds case outright and never reaches the display. What the user
    reads is chosen separately, by :func:`_role_label`.
    """
    return " ".join(title.split()).casefold()


def _better_spelling(current: str | None, candidate: str) -> str:
    """The more human of two spellings of one title, for display.

    A mixed-case spelling was typed by a person and carries its acronyms intact;
    an all-caps or all-lowercase one came off a board's template. Kept as the
    *raw* spelling rather than as a rendered label, so a mixed-case sighting
    later in the scan can still displace a shouted one seen first.
    """
    cleaned = " ".join(candidate.split())
    if current is None:
        return cleaned
    if _is_shouted(current) and not _is_shouted(cleaned):
        return cleaned
    return current


def _is_shouted(title: str) -> bool:
    return title.isupper() or title.islower()


def _role_label(spelling: str) -> str:
    """The title as the page shows it.

    ``.title()`` is the last resort rather than the rule — it is the thing that
    turns "QA" into "Qa" — so it only runs when every spelling of this role in
    the whole window was shouted and there is no human one to prefer.
    """
    return spelling.title() if _is_shouted(spelling) else spelling


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; the arithmetic below needs tz-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _bucket_start(when: date, bucket: str) -> date:
    """The first day of the week (Monday) or month *when* falls in."""
    if bucket == "month":
        return when.replace(day=1)
    return when - timedelta(days=when.weekday())


def _next_bucket(period: date, bucket: str) -> date:
    """The bucket after *period*. Month arithmetic without a calendar library."""
    if bucket == "month":
        return (period.replace(day=28) + timedelta(days=7)).replace(day=1)
    return period + timedelta(days=7)


def _bucket_span(first: date, last: date, bucket: str) -> list[date]:
    """Every bucket from *first* to *last* inclusive, with none missing.

    The trend is tallied into a dict keyed by bucket, so a period in which the
    user applied to nothing simply has no key. Rendered straight, that series
    lies: a chart draws its points evenly spaced, so three silent weeks between
    two active ones collapse into one step and the line reads as continuous
    activity. The gap is also the single most actionable thing on the chart —
    "you stopped for three weeks" is advice; a smooth line is not.

    Bounded by construction: the caller passes a span it already limited, and
    ``days`` is capped at 730, so the worst case is ~105 weekly points.
    """
    periods: list[date] = []
    current = first
    while current <= last:
        periods.append(current)
        current = _next_bucket(current, bucket)
    return periods


def _reply_window(latencies: list[float]) -> int:
    """How many days a cohort needs before its response rate is final.

    The p90 of the user's own reply latencies rather than the median: the median
    is the answer to "when does a reply usually arrive", and the question here
    is the different one of "when have they all arrived". Half a cohort's
    replies still outstanding is not a settled number.

    Rounded up to whole days, floored at one — a bucket is never final on the
    day its last application went out — and capped at
    ``MAX_REPLY_WINDOW_DAYS`` so a single four-month reply cannot mark the
    entire chart provisional. Under ``MIN_LATENCY_SAMPLE`` replies there is no
    distribution to take a percentile of and the default stands in.
    """
    if len(latencies) < MIN_LATENCY_SAMPLE:
        return DEFAULT_REPLY_WINDOW_DAYS
    ordered = sorted(latencies)
    # Nearest-rank p90: the smallest value at or above 90% of the sample.
    index = min(ceil(0.9 * len(ordered)) - 1, len(ordered) - 1)
    return max(1, min(ceil(ordered[index]), MAX_REPLY_WINDOW_DAYS))


def _is_maturing(period: date, bucket: str, window: int, today: date) -> bool:
    """Whether *period* is still collecting the replies it is owed.

    Measured from the bucket's *last* day, not its first: a week is only settled
    once its youngest application has had the full window, and using the first
    day would call a Monday-to-Sunday week final while Sunday's outreach was
    barely a day old.
    """
    last_day = _next_bucket(period, bucket) - timedelta(days=1)
    return (today - last_day).days < window


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


class _Tally:
    """Applications / responses / interviews for one grouping key.

    ``emails_sent`` is only read by the trend, where volume and rate have to be
    shown together; the ranked lists take the default and ignore it.
    """

    __slots__ = ("applications", "responses", "interviews", "emails_sent")

    def __init__(self) -> None:
        self.applications = self.responses = self.interviews = self.emails_sent = 0

    def add(self, status: ApplicationStatus, *, emails_sent: int = 0) -> None:
        self.applications += 1
        self.emails_sent += emails_sent
        if status in ENGAGED:
            self.responses += 1
        if status in INTERVIEWING:
            self.interviews += 1

    @property
    def response_rate(self) -> float:
        return _rate(self.responses, self.applications)


def _profile_rows(
    tallies: dict[int | None, _Tally],
    profile_rows: dict[int, Profile],
    limit: int,
) -> list[ProfilePerformance]:
    """The profile ranking, or nothing.

    Returns an empty list unless there are at least two buckets *and* at least
    one of them is a real profile. Both conditions matter and for different
    reasons: with one bucket the table restates the headline response rate under
    a new heading, and with no profile at all every application is in the
    fallback bucket, which is a table with one row called "your default search".
    Neither is a finding, and the panel is hidden rather than captioned — an
    empty comparison is worse than an absent one, because the reader assumes a
    comparison was made.

    An id with no row behind it is a profile the user deleted. It folds into the
    fallback bucket rather than being dropped: the outreach still went out, and
    losing it here would make this table the one place on the page whose total
    disagrees with every other.
    """
    named = {
        profile_id: tally
        for profile_id, tally in tallies.items()
        if profile_id is not None and profile_id in profile_rows
    }
    if not named:
        return []

    orphaned = _Tally()
    for profile_id, tally in tallies.items():
        if profile_id in named:
            continue
        orphaned.applications += tally.applications
        orphaned.responses += tally.responses
        orphaned.interviews += tally.interviews

    rows = [
        ProfilePerformance(
            profile_id=profile_id,
            label=profile_rows[profile_id].name,
            is_active=profile_rows[profile_id].is_active,
            applications=t.applications,
            responses=t.responses,
            interviews=t.interviews,
            response_rate=t.response_rate,
        )
        for profile_id, t in named.items()
    ]
    if orphaned.applications:
        rows.append(
            ProfilePerformance(
                profile_id=None,
                label=NO_PROFILE,
                applications=orphaned.applications,
                responses=orphaned.responses,
                interviews=orphaned.interviews,
                response_rate=orphaned.response_rate,
            )
        )
    if len(rows) < 2:
        return []

    rows.sort(
        key=lambda r: (r.applications >= MIN_SAMPLE, r.response_rate, r.applications),
        reverse=True,
    )
    # Same rule as the resume table: nothing is crowned off a lucky reply, and
    # a switched-off profile can still hold the crown — that is the finding.
    best = next(
        (r for r in rows if r.applications >= MIN_SAMPLE and r.responses), None
    )
    if best is not None:
        best.is_best = True
    return rows[:limit]


def _rank(tallies: dict[str, _Tally], limit: int) -> list[SegmentPerformance]:
    """Best-responding segments first, with thin samples pushed down.

    Sorting on rate alone puts "1 application, 1 reply" above a company with 20
    applications and a 40% rate, which is exactly backwards as advice. Rows that
    clear ``MIN_SAMPLE`` sort ahead of those that don't; within each group it is
    rate, then volume.
    """
    rows = [
        SegmentPerformance(
            name=name,
            applications=t.applications,
            responses=t.responses,
            interviews=t.interviews,
            response_rate=t.response_rate,
        )
        for name, t in tallies.items()
    ]
    rows.sort(
        key=lambda r: (
            r.applications >= MIN_SAMPLE,
            r.response_rate,
            r.applications,
        ),
        reverse=True,
    )
    return rows[:limit]


# ---------------------------------------------------------------------------
# "Where to focus" — the same rankings, said out loud
#
# The ranked tables above already hold the answer to "what is working"; until
# now the user had to derive it themselves by reading two lists and doing the
# division. These helpers turn the top and bottom of a ranking into sentences.
#
# Nothing new is computed and no model is called. The only judgement encoded
# here is *when to stay quiet*, which is the whole difficulty: a recommendation
# drawn from four applications is worse than no recommendation, because the user
# will act on it.
# ---------------------------------------------------------------------------

# Both sides of a comparison need this many applications before it is said
# aloud. Higher than ``MIN_SAMPLE`` on purpose: three applications is enough for
# a table row the user reads with their own eyes and discounts accordingly, and
# not enough for a sentence telling them where to spend next month.
MIN_FOCUS_SAMPLE = 5

# How much better one segment has to do before the gap is worth a sentence.
# Below this the honest summary is "they're about the same", which nobody needs
# three bullet points to be told.
FOCUS_LIFT = 1.5

# Sends below which no comparison is offered at all, whatever the segments say.
MIN_FOCUS_TOTAL = 10

# An open rate this far above the reply rate means the message is being read and
# not answered — a body problem, not a subject-line problem.
BODY_PROBLEM_OPEN_RATE = 0.35
# Below this, almost nobody is opening at all and the subject is where the work
# is, whatever the body says.
SUBJECT_PROBLEM_OPEN_RATE = 0.20


def _reliable_extremes(
    tallies: dict[str, _Tally],
) -> tuple[tuple[str, _Tally], tuple[str, _Tally]] | None:
    """Best- and worst-responding rows that both clear ``MIN_FOCUS_SAMPLE``."""
    rows = [(name, t) for name, t in tallies.items() if t.applications >= MIN_FOCUS_SAMPLE]
    if len(rows) < 2:
        return None
    rows.sort(key=lambda row: row[1].response_rate)
    best, worst = rows[-1], rows[0]
    if best[0] == worst[0]:
        return None
    return best, worst


def _comparison(dimension: str, tallies: dict[str, _Tally]) -> FocusNote | None:
    """"Your reply rate at X is 3× your rate at Y" — or nothing.

    Returns None far more often than it returns a sentence, which is the point.
    """
    extremes = _reliable_extremes(tallies)
    if extremes is None:
        return None
    (best_name, best), (worst_name, worst) = extremes
    if not best.responses:
        return None

    if worst.responses:
        lift = best.response_rate / worst.response_rate
        if lift < FOCUS_LIFT:
            return None
        multiple = f"{lift:.1f}×".replace(".0×", "×")
        text = (
            f"Your reply rate at {best_name} is {multiple} your rate at "
            f"{worst_name} — {best.responses} of {best.applications} came back "
            f"versus {worst.responses} of {worst.applications}."
        )
    else:
        text = (
            f"{best_name} answers and {worst_name} does not — "
            f"{best.responses} of {best.applications} came back versus "
            f"none of {worst.applications}."
        )
    return FocusNote(
        kind=dimension,
        text=text,
        subject=best_name,
        sample=best.applications + worst.applications,
    )


def _profile_note(rows: list[ProfilePerformance]) -> FocusNote | None:
    """Whether one search intent is measurably out-performing another.

    The sharpest lever on the page: a profile is a switch the user already has,
    and unlike a resume comparison this one names something they can turn off
    this afternoon. Which is exactly why it also carries the biggest risk of
    being acted on off nothing, so it takes the same ``MIN_FOCUS_SAMPLE`` on
    both sides as every other comparison here.
    """
    reliable = [r for r in rows if r.applications >= MIN_FOCUS_SAMPLE]
    if len(reliable) < 2:
        return None
    reliable.sort(key=lambda r: r.response_rate)
    best, worst = reliable[-1], reliable[0]
    if not best.responses:
        return None
    if worst.responses and best.response_rate / worst.response_rate < FOCUS_LIFT:
        return None
    # A profile the user already stopped chasing outperforming one they are
    # still running is the finding, not a footnote — it changes the sentence
    # from "send more of this" to "you turned off the wrong one".
    tail = (
        f' — and "{best.label}" is switched off.'
        if not best.is_active
        else f' — put more through "{best.label}".'
    )
    return FocusNote(
        kind="profile",
        text=(
            f'"{best.label}" is pulling {best.responses} replies from '
            f"{best.applications} applications against {worst.responses} from "
            f'{worst.applications} for "{worst.label}"{tail}'
        ),
        subject=best.label,
        sample=best.applications + worst.applications,
    )


def _resume_note(rows: list[ResumePerformance]) -> FocusNote | None:
    """Whether one resume is measurably out-performing another."""
    reliable = [r for r in rows if r.applications >= MIN_FOCUS_SAMPLE and r.label]
    if len(reliable) < 2:
        return None
    reliable.sort(key=lambda r: r.response_rate)
    best, worst = reliable[-1], reliable[0]
    if not best.responses:
        return None
    if worst.responses and best.response_rate / worst.response_rate < FOCUS_LIFT:
        return None
    return FocusNote(
        kind="resume",
        text=(
            f'"{best.label}" is pulling {best.responses} replies from '
            f"{best.applications} sends against {worst.responses} from "
            f'{worst.applications} for "{worst.label}" — send the first one more.'
        ),
        subject=best.label,
        sample=best.applications + worst.applications,
    )


def _persistence_note(effect: follow_up_metrics.FollowUpEffect) -> FocusNote | None:
    """Whether the follow-up sequence is earning its sends, in one sentence.

    Two claims, in this order, and never both:

    * a dead tail — the step to stop at. This one is worth interrupting the user
      for even when the rest of the account is healthy, because every send past
      it costs the sending mailbox reputation for nothing.
    * follow-ups carrying most of the replies — worth saying because the
      obvious reaction to a low first-email reply rate is to send more first
      emails, and this is the evidence that the opposite is true.

    Silent otherwise, including the whole "chasing works about as well as you
    would expect" middle, which needs no sentence.
    """
    if effect.contacted < follow_up_metrics.MIN_CONTACTED:
        return None
    if effect.suggested_last_step is not None:
        return FocusNote(
            kind="persistence",
            text=effect.summary,
            subject=follow_up_metrics.step_label(effect.suggested_last_step),
            sample=effect.contacted,
        )
    # Only claim follow-ups are pulling their weight when they are pulling most
    # of the load *and* there are enough replies for "most" to be a fact.
    if (
        effect.replies_after_follow_up >= follow_up_metrics.MIN_STEP_SENDS
        and effect.share_from_follow_ups >= 0.5
    ):
        return FocusNote(
            kind="persistence",
            text=(
                f"{effect.share_from_follow_ups:.0%} of your replies "
                f"({effect.replies_after_follow_up} of {effect.replied}) only "
                "came after a follow-up — the first email is not where your "
                "answers are."
            ),
            subject="follow-ups",
            sample=effect.replied,
        )
    return None


def _engagement_note(engagement: Engagement, response_rate: float) -> FocusNote | None:
    """Whether the problem is the subject line or the message underneath it.

    A 0% reply rate at a 60% open rate and a 0% reply rate at a 5% open rate are
    completely different problems with opposite fixes, and this is the one place
    the product says which one the user has.
    """
    if not engagement.reliable:
        return None
    if engagement.open_rate >= BODY_PROBLEM_OPEN_RATE and response_rate < 0.10:
        return FocusNote(
            kind="engagement",
            text=(
                f"{engagement.opened} of {engagement.tracked} tracked emails were "
                f"opened but only {response_rate:.0%} got a reply — the subject line "
                "is working and the message under it is not."
            ),
            subject="message body",
            sample=engagement.tracked,
        )
    if engagement.open_rate < SUBJECT_PROBLEM_OPEN_RATE:
        return FocusNote(
            kind="engagement",
            text=(
                f"Only {engagement.opened} of {engagement.tracked} tracked emails "
                "were opened at all — work on subject lines before anything else."
            ),
            subject="subject line",
            sample=engagement.tracked,
        )
    return None


def _focus_notes(
    *,
    total: int,
    companies: dict[str, _Tally],
    industries: dict[str, _Tally],
    roles: dict[str, _Tally],
    sources: dict[str, _Tally],
    resumes: list[ResumePerformance],
    profiles: list[ProfilePerformance],
    engagement: Engagement,
    response_rate: float,
    persistence: follow_up_metrics.FollowUpEffect,
) -> list[FocusNote]:
    """At most three sentences about where the user's next week should go.

    Ordered by how actionable the advice is rather than by how big the number
    is: which market to chase beats which document to attach, and both beat a
    diagnosis of the copy. Returns an empty list — not a hedge — when the data
    cannot carry a claim.
    """
    if total < MIN_FOCUS_TOTAL:
        return [
            FocusNote(
                kind="volume",
                text=(
                    f"{total} applications so far. At {MIN_FOCUS_TOTAL} there is "
                    "enough here to say where your replies are coming from."
                ),
                subject="volume",
                sample=total,
            )
        ]

    # Ordered by how actionable the advice is. Role and source come in above
    # company because both are a decision about what to send next — which title
    # to chase, which board to keep scanning — while a company comparison is
    # mostly a fact about a list the user already built.
    candidates = [
        # Above every market comparison: a profile is a switch the user already
        # has, so this is the only sentence here whose advice is an action
        # rather than a direction.
        _profile_note(profiles),
        _comparison("industry", industries),
        _comparison("role", roles),
        _comparison("source", sources),
        _comparison("company", companies),
        _resume_note(resumes),
        # Below the segment comparisons and above the copy diagnosis: knowing
        # which market to chase is a bigger lever than how many times to chase
        # it, and both are more actionable than "your subject lines are weak".
        _persistence_note(persistence),
        _engagement_note(engagement, response_rate),
    ]
    return [note for note in candidates if note is not None][:3]


def _engagement(db: Session, user_id: int, since: datetime | None) -> Engagement:
    """Opens and clicks over the user's tracked sends.

    The denominator is *tracked* sends, not all sends: mail that went out before
    tracking was switched on carried no pixel and can never register an open, so
    counting it would understate every rate for as long as the account lives.

    Prefetch-flagged events are excluded — those are mail proxies caching the
    image at delivery time, which fires for every delivered message and would
    make the open rate approach 100% and mean nothing.
    """
    tracked_stmt = (
        select(func.count(Email.id))
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.user_id == user_id,
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.SENT,
            Email.tracking_token.is_not(None),
        )
    )
    if since is not None:
        tracked_stmt = tracked_stmt.where(Email.sent_at >= since)
    tracked = db.scalar(tracked_stmt) or 0
    if not tracked:
        return Engagement()

    def _distinct_emails(*event_types: EmailEventType) -> int:
        stmt = (
            select(func.count(func.distinct(EmailEvent.email_id)))
            .join(Email, EmailEvent.email_id == Email.id)
            .where(
                EmailEvent.user_id == user_id,
                EmailEvent.event_type.in_(event_types),
                EmailEvent.is_prefetch.is_(False),
                Email.tracking_token.is_not(None),
            )
        )
        if since is not None:
            stmt = stmt.where(Email.sent_at >= since)
        return db.scalar(stmt) or 0

    # A click is an open. Image blocking is the norm rather than the exception
    # (docs/features/email-tracking.md §2), so a recipient reading in Outlook
    # produces a CLICK and no OPEN at all — and counting opens off OPEN rows
    # alone put that message in the numerator of the click rate and nowhere in
    # the open rate. "Clicked after opening" then divided by a number smaller
    # than itself and reported 200%. ``email_tracking.record_click`` already
    # books the synthetic open on the message row for exactly this reason; this
    # is the same rule applied to the events those counters are derived from.
    opened = _distinct_emails(EmailEventType.OPEN, EmailEventType.CLICK)
    clicked = _distinct_emails(EmailEventType.CLICK)
    return Engagement(
        tracked=tracked,
        opened=opened,
        clicked=clicked,
        open_rate=_rate(opened, tracked),
        click_rate=_rate(clicked, tracked),
        click_to_open_rate=_rate(clicked, opened),
        reliable=tracked >= MIN_SAMPLE,
    )


@router.get("/overview", response_model=AnalyticsOverview)
def overview(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    days: int | None = Query(default=None, ge=1, le=730, description="Look back N days"),
    bucket: str = Query(default="week", pattern="^(week|month)$"),
    limit: int = Query(default=8, ge=1, le=50, description="Rows per ranked list"),
) -> AnalyticsOverview:
    """Everything the analytics panel renders, in one read."""
    since = datetime.now(UTC) - timedelta(days=days) if days else None

    # Outer join on the posting: plain company-targeted outreach has no
    # ``job_posting_id``, and an inner join would silently drop it from every
    # number on the page.
    stmt = (
        select(Application, Recruiter, Campaign, JobPosting)
        .join(Recruiter, Application.recruiter_id == Recruiter.id)
        .join(Campaign, Application.campaign_id == Campaign.id)
        .outerjoin(JobPosting, Application.job_posting_id == JobPosting.id)
        .where(Application.user_id == user.id)
    )
    if since is not None:
        stmt = stmt.where(Application.created_at >= since)
    records = db.execute(stmt).all()

    if not records:
        return AnalyticsOverview(bucket=bucket, min_sample=MIN_SAMPLE)

    engagement = _engagement(db, user.id, since)

    # One pass for every thread, rather than a query per application — this
    # endpoint is polled alongside the pipeline.
    application_ids = [a.id for a, _, _, _ in records]
    threads_by_app: dict[int, list[EmailThread]] = defaultdict(list)
    for thread in db.scalars(
        select(EmailThread)
        .where(EmailThread.application_id.in_(application_ids))
        .options(selectinload(EmailThread.emails))
    ):
        threads_by_app[thread.application_id].append(thread)

    resume_labels = {
        row.id: row.display_label
        for row in db.scalars(select(Resume).where(Resume.user_id == user.id))
    }
    profile_rows = {
        row.id: row
        for row in db.scalars(select(Profile).where(Profile.user_id == user.id))
    }

    trend: dict[date, _Tally] = defaultdict(_Tally)
    by_status: dict[ApplicationStatus, int] = defaultdict(int)
    resumes: dict[int | None, _Tally] = defaultdict(_Tally)
    profiles: dict[int | None, _Tally] = defaultdict(_Tally)
    companies: dict[str, _Tally] = defaultdict(_Tally)
    industries: dict[str, _Tally] = defaultdict(_Tally)
    roles: dict[str, _Tally] = defaultdict(_Tally)
    # Keyed by the same case-folded key as `roles`; holds the best raw spelling
    # seen for it, which becomes the row's name once the pass is done.
    role_spellings: dict[str, str] = {}
    sources: dict[str, _Tally] = defaultdict(_Tally)
    latencies: list[float] = []
    # One list per application, handed to the persistence ladder after the pass.
    # These are the same `Email` objects the loop already walks — collected
    # rather than re-queried, so the ladder cannot disagree with the volume
    # counters computed beside it.
    email_sets: list[list[Email]] = []
    responses = interviews = 0
    emails_sent = replies_received = first_touches = 0

    for application, recruiter, campaign, posting in records:
        status = application.status
        by_status[status] += 1
        if status in ENGAGED:
            responses += 1
        if status in INTERVIEWING:
            interviews += 1

        emails = [e for t in threads_by_app.get(application.id, []) for e in t.emails]
        email_sets.append(emails)
        # Only mail that actually left counts. Drafts and queued messages are
        # work the user has not done yet, and counting them would let the
        # volume figure run ahead of the sender.
        sent = [
            e
            for e in emails
            if e.direction == EmailDirection.SENT and e.status == EmailStatus.SENT
        ]
        received = [e for e in emails if e.direction == EmailDirection.RECEIVED]
        emails_sent += len(sent)
        replies_received += len(received)
        if sent:
            first_touches += 1

        created = _aware(application.created_at) or datetime.now(UTC)
        # Mail is bucketed by the *application's* cohort, not its own send date,
        # so a follow-up chased in March still counts against the January batch
        # that earned it. Same rule as the response rate it sits beside.
        trend[_bucket_start(created.date(), bucket)].add(status, emails_sent=len(sent))
        resumes[campaign.resume_id].add(status)
        # Recorded on the application at send time, so a profile renamed or
        # switched off since keeps its history. See models/application.py.
        profiles[application.profile_id].add(status)

        if recruiter.company:
            companies[recruiter.company].add(status)
        # The recruiter's own industry is the best signal; a campaign that
        # targeted one industry is the fallback so the list isn't empty for
        # users whose contacts were scraped without one.
        industry = recruiter.industry or (campaign.target_industries or [None])[0]
        if industry:
            industries[industry].add(status)

        # The posting's own title is the role that was actually applied for. The
        # campaign's first target role is the fallback for company-targeted
        # outreach, which names no single posting — same shape as the industry
        # fallback directly above.
        role = (posting.title if posting else None) or (
            (campaign.target_roles or [None])[0]
        )
        if role:
            key = _role_key(role)
            roles[key].add(status)
            role_spellings[key] = _better_spelling(role_spellings.get(key), role)

        # Everything that did not come off a board is one bucket rather than
        # none: "did the crawlers or the recruiter search earn this?" is only a
        # question you can answer if both sides are counted.
        sources[(posting.source if posting else None) or DIRECT_SOURCE].add(status)

        latency = reply_metrics.reply_latency_days(emails)
        if latency is not None:
            latencies.append(latency)

    total = len(records)
    effect = follow_up_metrics.persistence(email_sets)
    # Drawn from every reply in the window, not from the trend, so the whole
    # series is judged against one window rather than each bucket against
    # itself.
    reply_window = _reply_window(latencies)
    today = datetime.now(UTC).date()
    profile_performance = _profile_rows(profiles, profile_rows, limit)
    # Case-folded keys were only ever for grouping; the page shows the spelling
    # the candidate's own feed used. One label per key, so no row can collide.
    labelled_roles = {
        _role_label(role_spellings[key]): tally for key, tally in roles.items()
    }

    resume_rows = [
        ResumePerformance(
            resume_id=resume_id,
            label=(
                resume_labels.get(resume_id, f"Resume #{resume_id}")
                if resume_id is not None
                else "No resume attached"
            ),
            applications=t.applications,
            responses=t.responses,
            interviews=t.interviews,
            response_rate=t.response_rate,
        )
        for resume_id, t in resumes.items()
    ]
    resume_rows.sort(
        key=lambda r: (r.applications >= MIN_SAMPLE, r.response_rate, r.applications),
        reverse=True,
    )
    # "Best performing" only means something against a real sample. With no row
    # clearing the bar nothing is crowned, rather than crowning a fluke.
    best = next((r for r in resume_rows if r.applications >= MIN_SAMPLE and r.responses), None)
    if best is not None:
        best.is_best = True

    return AnalyticsOverview(
        total_applications=total,
        responses=responses,
        interviews=interviews,
        response_rate=_rate(responses, total),
        interview_rate=_rate(interviews, total),
        median_days_to_reply=round(median(latencies), 1) if latencies else None,
        reply_window_days=reply_window,
        bucket=bucket,
        trend=[
            TrendPoint(
                period=period,
                label=(
                    period.strftime("%b %Y")
                    if bucket == "month"
                    else period.strftime("%d %b")
                ),
                # ``.get`` rather than ``[]`` — ``trend`` is a defaultdict, and
                # indexing it here would write an empty tally back into the dict
                # being iterated over.
                applications=(t := trend.get(period) or _Tally()).applications,
                responses=t.responses,
                interviews=t.interviews,
                response_rate=t.response_rate,
                emails_sent=t.emails_sent,
                maturing=_is_maturing(period, bucket, reply_window, today),
            )
            for period in _bucket_span(
                # Anchored to the window the caller asked for, not to the first
                # row in it. Someone asking for 90 days and seeing a series that
                # starts at their first application in week six has been shown a
                # different question than the one they asked.
                _bucket_start(since.date(), bucket)
                if since is not None
                else min(trend),
                # Runs to *today*, not to the last application. A user who
                # stopped applying three weeks ago should see three empty
                # buckets, which is the chart earning its place. The max() is
                # for a row dated ahead of the clock — gap-filling must never be
                # the thing that drops a real bucket off the end.
                max(_bucket_start(datetime.now(UTC).date(), bucket), max(trend)),
                bucket,
            )
        ],
        by_status=[
            StatusSlice(
                status=status.value,
                label=label,
                count=by_status[status],
                share=_rate(by_status[status], total),
            )
            for status, label in STATUS_LABELS
            if by_status.get(status)
        ],
        resumes=resume_rows[:limit],
        profiles=profile_performance,
        companies=_rank(companies, limit),
        industries=_rank(industries, limit),
        roles=_rank(labelled_roles, limit),
        sources=_rank(sources, limit),
        engagement=engagement,
        outreach=OutreachVolume(
            emails_sent=emails_sent,
            first_touches=first_touches,
            # Every contacted application accounts for exactly one first email,
            # so the remainder is what was sent chasing them.
            follow_ups=emails_sent - first_touches,
            replies_received=replies_received,
            emails_per_response=(
                round(emails_sent / responses, 1) if responses else None
            ),
            awaiting_send=total - first_touches,
        ),
        persistence=Persistence(
            steps=[
                PersistenceStep(
                    step=step.step,
                    label=step.label,
                    sent=step.sent,
                    replies=step.replies,
                    reply_rate=step.reply_rate,
                    reliable=step.reliable,
                )
                for step in effect.steps
            ],
            contacted=effect.contacted,
            replied=effect.replied,
            replies_after_follow_up=effect.replies_after_follow_up,
            share_from_follow_ups=effect.share_from_follow_ups,
            last_productive_step=effect.last_productive_step,
            wasted_sends=effect.wasted_sends,
            suggested_last_step=effect.suggested_last_step,
            summary=effect.summary,
            min_step_sends=effect.min_step_sends,
            min_contacted=effect.min_contacted,
        ),
        # Deliberately built from the *full* tallies rather than the truncated
        # ranked lists above: the worst-performing segment is exactly the row
        # ``limit`` cuts off, and it is half of every comparison worth making.
        focus=_focus_notes(
            total=total,
            companies=companies,
            industries=industries,
            roles=labelled_roles,
            sources=sources,
            resumes=resume_rows,
            profiles=profile_performance,
            engagement=engagement,
            response_rate=_rate(responses, total),
            persistence=effect,
        ),
        min_sample=MIN_SAMPLE,
    )


@router.get("/subject-variants", response_model=list[SubjectExperiment])
def subject_variants(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    campaign_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
) -> list[SubjectExperiment]:
    """Per-campaign subject-line experiments, newest campaign first.

    Scoped by joining to ``Campaign.user_id`` — another user's campaign id is a
    404, the same shape as ``routers/inbox._owned_thread``.
    """
    stmt = select(Campaign).where(Campaign.user_id == user.id)
    if campaign_id is not None:
        stmt = stmt.where(Campaign.id == campaign_id)
    campaigns = list(db.scalars(stmt.order_by(Campaign.id.desc())))

    if campaign_id is not None and not campaigns:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found"
        )

    # One read for every campaign's variants rather than one per campaign. The
    # loop that was here cost a query per campaign, which is invisible on a
    # developer's two and is the whole cost of the page for a six-month search.
    by_campaign = subject_ab_service.stats_for_user(
        db, user.id, campaign_id=campaign_id
    )

    experiments: list[SubjectExperiment] = []
    for campaign in campaigns:
        rows, confident = by_campaign.get(campaign.id, ([], False))
        if not rows:
            continue
        experiments.append(
            SubjectExperiment(
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                variants=[SubjectVariantStats(**vars(r)) for r in rows],
                confident=confident,
            )
        )
    return experiments


@router.get("/calibration", response_model=FitCalibration)
def calibration(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    days: int | None = Query(default=None, ge=1, le=730, description="Look back N days"),
) -> FitCalibration:
    """Is the fit score predicting replies? Bucketed by the score sent under.

    The one endpoint here that can return bad news about the product itself: if
    the bands do not separate, ``verdict`` says ``flat`` and the summary says so
    in words. See :mod:`app.services.fit_calibration` for why that is a feature.
    """
    result = fit_calibration.report(db, user.id, days=days)
    return FitCalibration(
        bands=[
            CalibrationBand(
                label=b.label,
                lower=b.lower,
                upper=b.upper,
                sends=b.sends,
                responses=b.responses,
                interviews=b.interviews,
                response_rate=b.response_rate,
                interview_rate=b.interview_rate,
                reliable=b.reliable,
            )
            for b in result.bands
        ],
        scored_sends=result.scored_sends,
        unscored_sends=result.unscored_sends,
        responses=result.responses,
        interviews=result.interviews,
        response_rate=result.response_rate,
        correlation=result.correlation,
        verdict=result.verdict,
        summary=result.summary,
        suggested_min_fit_score=result.suggested_min_fit_score,
        current_min_fit_score=result.current_min_fit_score,
        min_band_sends=result.min_band_sends,
        min_total_sends=result.min_total_sends,
    )


def _skill_rows(rows: list[skills_gap.SkillGap]) -> list[SkillGapRow]:
    return [
        SkillGapRow(
            skill=row.skill,
            postings=row.postings,
            share=row.share,
            on_resume_id=row.on_resume_id,
            on_resume_label=row.on_resume_label,
        )
        for row in rows
    ]


@router.get("/skills-gap", response_model=SkillsGapOut)
def skills_gap_report(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SkillsGapOut:
    """Which absences from the resume are actually costing roles.

    `FitScore.missing_skills` was written on every scored posting and read back
    one posting at a time. Summed, it answers a question the product could not
    answer before — and splits into two lists that must not be merged, because
    one is a quarter of study and the other is one edit. See
    :mod:`app.services.skills_gap`.

    Returns `note` and empty lists rather than thin advice when too little has
    been scored to support a claim, the same way `_focus_notes` does above.
    """
    result = skills_gap.report(db, user)
    return SkillsGapOut(
        scored_postings=result.scored_postings,
        blocking=_skill_rows(result.blocking),
        already_have=_skill_rows(result.already_have),
        note=result.note,
        min_scored_postings=skills_gap.MIN_SCORED_POSTINGS,
        min_postings_per_skill=skills_gap.MIN_POSTINGS_PER_SKILL,
    )


@router.get("/stage-velocity", response_model=StageVelocityOut)
def stage_velocity_report(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StageVelocityOut:
    """How long applications sit in each stage, and where they stop moving.

    `ApplicationStatusEvent` has recorded every transition since the board
    learned to let a user drag a card, and until now it was read only one
    application at a time to render a history panel. `median_days_to_reply` on
    the dashboard answers a narrower question — it cannot see a thread that
    reached SCHEDULING and sat there for five weeks. See
    :mod:`app.services.stage_velocity`, in particular why a finished dwell and a
    running one are reported separately and never pooled.
    """
    result = stage_velocity.report(db, user)
    return StageVelocityOut(
        stages=[
            StageVelocityRow(
                status=row.status,
                entered=row.entered,
                moved_on=row.moved_on,
                median_days=row.median_days,
                slowest_days=row.slowest_days,
                stuck=row.stuck,
                longest_stuck_days=row.longest_stuck_days,
                exit_rate=row.exit_rate,
                reliable=row.reliable,
            )
            for row in result.stages
        ],
        applications=result.applications,
        events=result.events,
        worst_stage=result.worst_stage,
        note=result.note,
        min_stage_sample=result.min_stage_sample,
    )


@router.get("/bounces", response_model=BounceOverview)
def bounces(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(default=20, ge=1, le=100),
) -> BounceOverview:
    """Hard/soft bounce totals and the worst recipient domains.

    Domains below ``MIN_DOMAIN_SENDS`` are excluded: one bounce out of one send
    is a 100% rate and tells the user nothing, the same reasoning the ranked
    lists above apply to response rates.
    """
    totals = dict(
        db.execute(
            select(EmailBounce.kind, func.count(EmailBounce.id))
            .where(EmailBounce.user_id == user.id)
            .group_by(EmailBounce.kind)
        ).all()
    )
    suppressed = (
        db.scalar(
            select(func.count(Recruiter.id)).where(
                Recruiter.user_id == user.id,
                Recruiter.delivery_state == DeliveryState.HARD_BOUNCED,
            )
        )
        or 0
    )
    rows = bounce_service.domain_bounce_rates(db, user.id, limit=limit)
    return BounceOverview(
        hard_total=totals.get(BounceKind.HARD, 0),
        soft_total=totals.get(BounceKind.SOFT, 0),
        suppressed_contacts=suppressed,
        domains=[
            DomainBounceStats(
                domain=r.domain,
                sent=r.sent,
                bounced=r.bounced,
                hard=r.hard,
                soft=r.soft,
                rate=r.rate,
                alerting=r.alerting,
            )
            for r in rows
        ],
        min_sample=bounce_service.MIN_DOMAIN_SENDS,
    )


@router.get("/campaigns", response_model=CampaignEffectivenessOut)
def campaign_effectiveness(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CampaignEffectivenessOut:
    """Per-campaign effectiveness: response rate, latency, and conversion.

    The overview above ranks six dimensions of a user's outreach and, until
    now, not the one they actually create and name. "Was the Series-B fintech
    push worth doing?" is a question about a campaign, and it was the only
    question the analytics page could not answer about the object the whole
    product is organised around.

    Three figures rather than one, because they fail in different directions
    and the fix for each is different — see
    :mod:`app.services.campaign_metrics` for what each is over and why. Rows
    below ``MIN_SAMPLE`` contacts come back with ``reliable=false`` rather than
    being dropped: a campaign that has contacted two people is a real campaign
    and its absence from this list would read as a bug.
    """
    rows = campaign_metrics.effectiveness(db, user.id)
    # Opening this screen is itself a feature use, and it is the one report
    # whose adoption nobody could otherwise measure. Recorded before the
    # response is built so that the commit it needs cannot expire the campaigns
    # this handler has already loaded.
    usage_events.record(
        db,
        "campaign_report.viewed",
        user_id=user.id,
        commit=True,
        campaigns=len(rows),
    )
    return CampaignEffectivenessOut(
        campaigns=[
            CampaignEffectOut(
                campaign_id=row.campaign_id,
                name=row.name,
                status=row.status.value,
                created_at=row.created_at.isoformat() if row.created_at else None,
                applications=row.applications,
                contacted=row.contacted,
                responses=row.responses,
                interviews=row.interviews,
                emails_sent=row.emails_sent,
                response_rate=row.response_rate,
                success_rate=row.success_rate,
                days_to_first_response=row.days_to_first_response,
                reliable=row.reliable,
            )
            for row in rows
        ],
        min_sample=campaign_metrics.MIN_SAMPLE,
    )


@router.post(
    "/usage",
    status_code=status.HTTP_204_NO_CONTENT,
    # Cheap per call — one INSERT — and rationed anyway, because it is the only
    # route in this application whose whole purpose is to let a client write a
    # row on demand. Unmetered, a page with a loop in it fills the table faster
    # than the daily prune empties it, and the first symptom is the report this
    # endpoint feeds becoming the slowest screen in the product. Generous
    # enough that ordinary use never sees it: a busy session opens a dozen
    # panels a minute, not sixty.
    dependencies=[Depends(rate_limit(60, 60, scope="usage-beacon"))],
)
def record_client_event(
    payload: ClientEventIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Record one browser-observed feature use.

    A handful of the things worth measuring leave no trace on the server: the
    "Why this score" panel expanding, the analytics page being opened. They are
    a click and a re-render against data the client already holds, so no request
    is made and no row is written, and a feature that is used constantly is
    indistinguishable from one nobody has ever opened.

    **Only the names in ``CLIENT_FEATURES`` are accepted**, and the refusal is a
    422 that names them. Everything else in the vocabulary describes something
    the server observed for itself, and a client that could post
    ``campaign.launched`` could invent a launch — at which point the report is
    measuring what the browser claims rather than what the product did. That is
    the failure mode of every analytics pipeline that trusts its own client, and
    the allowlist is the whole defence against it.

    ``user_id`` comes from the token, never from the body.
    """
    if payload.feature not in usage_events.CLIENT_FEATURES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Unknown client event; accepted: "
                + ", ".join(sorted(usage_events.CLIENT_FEATURES))
            ),
        )
    usage_events.record(
        db, payload.feature, user_id=user.id, commit=True, **payload.props
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
