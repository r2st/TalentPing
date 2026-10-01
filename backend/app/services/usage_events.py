"""The vocabulary of feature usage, the one writer, and the one reader.

Three things live here and nowhere else.

**The closed set.** :data:`FEATURES` is every event this product emits, each with
the area it belongs to and a label the report renders. A name that is not in it
is refused at the write site rather than stored, for the reason
:mod:`app.core.events` gives about its own set: a dashboard is built on names
agreeing across the sites that emit them and the query that reads them, and
nothing else enforces that. ``tests/test_usage_events.py`` holds the emitters to
it.

**One writer.** :func:`record`. It clips, it coerces, and it does not raise —
see its docstring for the transaction contract, which is the part worth reading
before adding a call site.

**One reader.** :func:`report` answers the three questions the product team has:
what is used daily and weekly, how many people who *tried* a feature came back
to it, and which features are at the top and the bottom of the list.

What is deliberately not here:

*Delivery events.* Opens, clicks, replies and bounces already have tables of
their own — ``email_events``, ``email_bounces``, and the ``emails`` rows
themselves — with counters derived from them and three reports already reading
them. Copying them in here would create a second set of numbers for the same
facts, and the two would disagree within a release. ``/analytics/overview`` and
``/analytics/bounces`` remain the answer to those.

*Anything about a recruiter.* Same rule as ``app.core.events``: this table has a
different retention window and a different set of readers from the user's own
inbox, and a third party who never signed up for this product does not belong in
either. The search terms a user types into their own job filters are here,
because they are that user's description of the job they want and the product
team cannot build a search feature without them; a recruiter's name or address
is not.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.feature_event import FeatureEvent

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# The closed set.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Feature:
    """One event name, and how the report should talk about it."""

    name: str
    #: Which part of the product it belongs to. The funnel is drawn per area.
    area: str
    #: How the report labels it. Sentence case, no ids.
    label: str
    #: True when the browser is the only place that can observe it — a panel
    #: expanding, a chart being opened. These are the only names
    #: ``POST /analytics/usage`` will accept, so that the client cannot forge a
    #: "campaign launched" it never launched.
    client: bool = False


CAMPAIGNS = "campaigns"
JOBS = "jobs"
SMART_APPLY = "smart_apply"
PROFILE = "profile"
SETTINGS = "settings"
ANALYTICS = "analytics"

#: Every event, in report order.
FEATURES: tuple[Feature, ...] = (
    # ---- Campaigns -------------------------------------------------------
    Feature("campaign.created", CAMPAIGNS, "Campaign created"),
    Feature("campaign.launched", CAMPAIGNS, "Campaign launched"),
    Feature("campaign.paused", CAMPAIGNS, "Campaign paused"),
    Feature("campaign.resumed", CAMPAIGNS, "Campaign resumed"),
    # ---- The job feed ----------------------------------------------------
    Feature("job.searched", JOBS, "Job feed filtered"),
    Feature("job.viewed", JOBS, "Job posting opened"),
    Feature("job.saved", JOBS, "Job saved"),
    Feature("job.applied", JOBS, "Job marked applied"),
    Feature("job.dismissed", JOBS, "Job dismissed"),
    Feature("job.archived", JOBS, "Job archived"),
    Feature("search.created", JOBS, "Saved search created"),
    Feature("search.ran", JOBS, "Saved search run"),
    # ---- Smart Apply -----------------------------------------------------
    Feature("smart_apply.started", SMART_APPLY, "Smart Apply started"),
    Feature("smart_apply.completed", SMART_APPLY, "Smart Apply completed"),
    Feature("smart_apply.failed", SMART_APPLY, "Smart Apply failed"),
    Feature("cover_letter.written", SMART_APPLY, "Cover letter written"),
    # The prune half of the pair above. Tailoring runs and letters were the two
    # artifacts the product only ever accumulated, so "how often does anybody
    # clear one out?" is a question about whether the history list is a record
    # people keep or a pile they wanted rid of.
    Feature("tailored_resume.deleted", SMART_APPLY, "Tailoring run discarded"),
    Feature("cover_letter.deleted", SMART_APPLY, "Cover letter discarded"),
    Feature("fit.scored", SMART_APPLY, "Fit score run"),
    Feature("fit.breakdown_viewed", SMART_APPLY, "Score breakdown opened", client=True),
    # ---- Profile ---------------------------------------------------------
    Feature("profile.created", PROFILE, "Profile created"),
    Feature("profile.updated", PROFILE, "Profile updated"),
    Feature("resume.uploaded", PROFILE, "Resume uploaded"),
    # ---- Settings --------------------------------------------------------
    Feature("settings.changed", SETTINGS, "Settings changed"),
    # Not a settings change, but it belongs beside one: this is the only signal
    # anybody has that the export exists and is being used. Server-side only —
    # the browser cannot forge "I downloaded my data", and the number is the
    # honest answer to "does anyone ever leave with their data, or did we build
    # a compliance button nobody presses?"
    Feature("account.exported", SETTINGS, "Account data downloaded"),
    # ---- Reports ---------------------------------------------------------
    Feature("analytics.viewed", ANALYTICS, "Analytics opened", client=True),
    Feature("campaign_report.viewed", ANALYTICS, "Campaign report opened"),
)

_BY_NAME: dict[str, Feature] = {f.name: f for f in FEATURES}

#: Every name above, for membership tests.
FEATURE_NAMES = frozenset(_BY_NAME)

#: The subset ``POST /analytics/usage`` accepts. Anything else the browser sends
#: is refused: a client that could post ``campaign.launched`` could invent a
#: launch, and the report would be measuring the client rather than the product.
CLIENT_FEATURES = frozenset(f.name for f in FEATURES if f.client)

#: Report order, for the areas.
AREAS: tuple[str, ...] = tuple(dict.fromkeys(f.area for f in FEATURES))


def feature(name: str) -> Feature | None:
    return _BY_NAME.get(name)


# --------------------------------------------------------------------------
# Writing.
# --------------------------------------------------------------------------

#: Enough for a location filter or a job title. Anything longer is a paste, and
#: a paste is not a search term worth a report row.
_MAX_VALUE = 120
#: A prop bag is a handful of scalars. More than this and somebody is trying to
#: store a document; the extras are dropped rather than the event.
_MAX_PROPS = 12
_MAX_KEY = 32
#: `feature_events.feature` is varchar(48). Postgres answers an over-long value
#: with StringDataRightTruncation, which nothing catches — see
#: tests/column_widths.py. Names come from `FEATURES` so this cannot fire, and
#: it is asserted rather than trusted.
_MAX_NAME = 48


def record(
    db: Session,
    name: str,
    *,
    user_id: int | None,
    commit: bool = False,
    when: datetime | None = None,
    **props: Any,
) -> FeatureEvent | None:
    """Note that *name* was used by *user_id*. Returns the row, or ``None``.

    **The transaction contract**, which decides where a call site puts this:

    The row is added to *db* and, by default, is persisted by whatever commit
    the caller was already going to make. So on a write endpoint this belongs
    immediately *before* ``db.commit()`` — it then costs no extra round trip, it
    lands in the same transaction as the thing it describes, and a handler that
    fails after the button was pressed records neither the change nor a use of
    it, which is the honest outcome.

    ``commit=True`` is for the read-only handlers — a posting opened, a feed
    filtered — which have no commit of their own to join. It suppresses
    ``expire_on_commit`` for the duration, because those handlers have already
    loaded the rows they are about to serialise and a commit that expired them
    would turn one insert into a re-``SELECT`` per row on the response path.
    That regression has been shipped here once already; see the ``perf(api)``
    commit about a commit between reading rows and rendering them.

    **It does not raise.** An unknown name, an unserialisable prop, a value
    three kilobytes long: all are logged and dropped or clipped. The alternative
    is a campaign launch that 500s because the line recording the launch could
    not be written, which is the failure mode :mod:`app.core.events` exists to
    avoid. Note the asymmetry that follows from the paragraph above: everything
    this function can control happens here, but the eventual ``INSERT`` is the
    caller's commit, so the values are made safe *now* rather than defended
    against later.
    """
    if not settings.usage_analytics_enabled:
        return None
    if name not in FEATURE_NAMES:
        # Loud, because this is a typo at a call site and the event is gone.
        # The test suite holds every emitter to the set so it cannot ship.
        logger.warning("usage: refusing unknown feature event %r", name)
        return None

    try:
        moment = when or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        row = FeatureEvent(
            user_id=user_id,
            feature=name[:_MAX_NAME],
            occurred_at=moment,
            day=moment.astimezone(UTC).date(),
            props=_clean(props),
        )
        db.add(row)
        if commit:
            _commit_without_expiring(db)
        return row
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("usage: could not record %r", name, exc_info=True)
        if commit:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 - nothing left to try
                logger.warning("usage: rollback failed after %r", name, exc_info=True)
        return None


def _commit_without_expiring(db: Session) -> None:
    """``db.commit()``, leaving the caller's already-loaded objects usable."""
    was = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    finally:
        db.expire_on_commit = was


def _clean(props: dict[str, Any]) -> dict[str, Any]:
    """The prop bag, reduced to small JSON scalars.

    Anything that is not a string, number or bool becomes its ``str`` — a list
    of filter names arrives here often enough that silently dropping it would be
    the wrong default, and a comma-joined string is both queryable and bounded.
    ``None`` is dropped rather than stored, for the reason
    ``app.core.events.emit`` gives: a key that is present-but-null on a third of
    rows makes grouping by it quietly wrong in a way an absent key does not.
    """
    out: dict[str, Any] = {}
    for key, value in props.items():
        if value is None or len(out) >= _MAX_PROPS:
            continue
        out[str(key)[:_MAX_KEY]] = _scalar(value)
    return out


def _scalar(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, list | tuple | set):
        value = ",".join(sorted(str(v) for v in value))
    return str(value)[:_MAX_VALUE]


def filters_used(candidates: dict[str, Any]) -> list[str]:
    """The names of the filters a request actually engaged.

    Which knobs get turned is the question the feed's filter row exists to
    answer, and it is answerable without keeping the values. ``False`` counts as
    engaged — "remote only: no" is a choice someone made — but ``None`` does
    not, because that is the parameter having been left alone.
    """
    return sorted(key for key, value in candidates.items() if value is not None)


# --------------------------------------------------------------------------
# Reading.
# --------------------------------------------------------------------------

#: Distinct days inside the window on which one user touched a feature before
#: they count as having *adopted* it rather than merely tried it. Two is the
#: smallest number that means anything at all: it is the difference between
#: opening a screen once and coming back to it.
ADOPTION_DAYS = 2

#: The report reads a window, and the window is bounded so that a screen nobody
#: has opened in months cannot become an unbounded scan when somebody does.
MAX_WINDOW_DAYS = 365
DEFAULT_WINDOW_DAYS = 30


@dataclass(slots=True)
class FeatureUsage:
    """One feature's line in the report."""

    name: str
    area: str
    label: str
    events: int = 0
    #: Distinct accounts that used it at all in the window.
    users: int = 0
    #: Of those, the ones who used it on `ADOPTION_DAYS` or more distinct days.
    repeat_users: int = 0
    #: The most recent day it was used. ``None`` when it never was.
    last_used: date | None = None

    @property
    def retention(self) -> float:
        """Share of the people who tried it who came back. 0.0 when nobody did."""
        return round(100.0 * self.repeat_users / self.users, 1) if self.users else 0.0


@dataclass(slots=True)
class PeriodUsage:
    """Active accounts and event volume for one day or week."""

    period: date
    users: int
    events: int


@dataclass(slots=True)
class UsageReport:
    since: date
    until: date
    bucket: str
    features: list[FeatureUsage] = field(default_factory=list)
    #: Whole-product activity per bucket — the denominator for everything above.
    active: list[PeriodUsage] = field(default_factory=list)
    #: Accounts that did anything at all in the window.
    active_users: int = 0
    #: Names in `FEATURES` with no rows at all in the window. The most useful
    #: column on the page and the one a "top N" ranking can never show.
    unused: list[str] = field(default_factory=list)


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def report(
    db: Session,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    bucket: str = "day",
    today: date | None = None,
) -> UsageReport:
    """Feature usage over the last *days*, bucketed by day or week.

    One query. It groups to ``(feature, day, user)`` triples in the database and
    rolls those up in Python, which is the only arrangement that can answer
    *distinct users per week* and *distinct users per feature* from the same
    read — a per-day distinct count cannot be summed into a per-week one, and
    running a query per bucket per feature is a screen that gets slower every
    release. The triple count is bounded by ``features × days × accounts``, and
    the window is capped, so the result set is small by construction rather than
    by luck.
    """
    days = max(1, min(days, MAX_WINDOW_DAYS))
    until = today or datetime.now(UTC).date()
    since = until - timedelta(days=days - 1)

    rows = db.execute(
        select(
            FeatureEvent.feature,
            FeatureEvent.day,
            FeatureEvent.user_id,
            func.count(FeatureEvent.id),
        )
        .where(FeatureEvent.day >= since, FeatureEvent.day <= until)
        .group_by(FeatureEvent.feature, FeatureEvent.day, FeatureEvent.user_id)
    ).all()

    per_feature: dict[str, FeatureUsage] = {}
    # feature -> user -> the days they used it. Sizes the adoption split.
    days_by_user: dict[str, dict[int, set[date]]] = defaultdict(
        lambda: defaultdict(set)
    )
    per_period: dict[date, tuple[set[int], int]] = {}
    everyone: set[int] = set()

    for name, day, user_id, count in rows:
        # A day arrives as a `date` from Postgres and, depending on the driver,
        # as a string from SQLite. Normalise once, here, rather than at four
        # comparison sites below.
        day = _as_date(day)
        meta = _BY_NAME.get(name)
        usage = per_feature.get(name)
        if usage is None:
            usage = per_feature[name] = FeatureUsage(
                name=name,
                area=meta.area if meta else "other",
                # A name that has been retired from `FEATURES` still has rows in
                # the table. Showing it raw is better than dropping it: the
                # column is what tells whoever removed it that it is still being
                # emitted somewhere.
                label=meta.label if meta else name,
            )
        usage.events += count
        if usage.last_used is None or day > usage.last_used:
            usage.last_used = day
        if user_id is not None:
            days_by_user[name][user_id].add(day)
            everyone.add(user_id)

        period = _week_start(day) if bucket == "week" else day
        users, events = per_period.get(period, (set(), 0))
        if user_id is not None:
            users.add(user_id)
        per_period[period] = (users, events + count)

    for name, by_user in days_by_user.items():
        usage = per_feature[name]
        usage.users = len(by_user)
        usage.repeat_users = sum(
            1 for used in by_user.values() if len(used) >= ADOPTION_DAYS
        )

    ordered = sorted(
        per_feature.values(),
        key=lambda u: (-u.events, u.name),
    )
    return UsageReport(
        since=since,
        until=until,
        bucket=bucket,
        features=ordered,
        active=[
            PeriodUsage(period=period, users=len(users), events=events)
            for period, (users, events) in sorted(per_period.items())
        ],
        active_users=len(everyone),
        unused=[f.name for f in FEATURES if f.name not in per_feature],
    )


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# --------------------------------------------------------------------------
# Retention.
# --------------------------------------------------------------------------


def prune(db: Session, *, today: date | None = None) -> int:
    """Delete events past the retention window. Returns the row count.

    Nothing else deletes from this table, and it takes a row per click. The
    argument is the one ``prune_notifications`` makes: the failure is not disk,
    it is a report that gets slower every week while answering a question about
    the last month.
    """
    keep = max(1, settings.usage_analytics_retention_days)
    cutoff = (today or datetime.now(UTC).date()) - timedelta(days=keep)
    result = db.execute(delete(FeatureEvent).where(FeatureEvent.day < cutoff))
    db.commit()
    return int(result.rowcount or 0)


__all__ = [
    "ADOPTION_DAYS",
    "AREAS",
    "CLIENT_FEATURES",
    "DEFAULT_WINDOW_DAYS",
    "FEATURES",
    "FEATURE_NAMES",
    "MAX_WINDOW_DAYS",
    "Feature",
    "FeatureUsage",
    "PeriodUsage",
    "UsageReport",
    "feature",
    "filters_used",
    "prune",
    "record",
    "report",
]
