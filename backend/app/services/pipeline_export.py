"""A one-page pipeline report, as a PDF.

The product already exports CSV (``GET /dashboard/export.csv``). This is not a
second one — a PDF of four hundred rows is a worse CSV, and two files that
answer the same question differently is the failure the CSV route's own
docstring warns about. What a PDF is good for is the thing a spreadsheet is bad
at: one page a person can read, or send to somebody else, with the headline
numbers and the conversations that are actually live.

So it is built from **the same read the CSV is built from** — ``dashboard()``,
with the same filters — rather than from its own queries. That is deliberate and
it is the whole architectural point of this module: a report that disagreed with
the table it was downloaded from, or with the CSV downloaded beside it, would be
worse than no report. Nothing here queries the database.

It also truncates on purpose, and says so on the page. The totals always
describe the whole filtered set; only the table is cut. A report that silently
listed the first forty of four hundred would be a lie of the most useful-looking
kind.

reportlab is an optional dependency and :mod:`app.services.resume_pdf` owns the
import guard, so this asks that module rather than importing reportlab a second
time — one place decides whether there is a PDF today.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.application import ApplicationStatus
from app.models.user import User

logger = logging.getLogger(__name__)

#: How many rows the report names before it stops listing and starts counting.
#: Roughly a page and a half; past that the reader is scrolling a table they
#: should have exported as CSV, which is the button next to this one.
PDF_ROW_LIMIT = 40

#: Statuses that mean the recruiter engaged, wherever it ended up. Mirrors
#: ``ENGAGED_STATUSES`` on the model — imported rather than restated, because
#: two readers of "did this get a reply" that disagree is a bug nobody notices
#: for months.
def _engaged() -> frozenset[str]:
    from app.models.application import ENGAGED_STATUSES

    return frozenset(s.value for s in ENGAGED_STATUSES)


def _interviewing() -> frozenset[str]:
    from app.models.application import INTERVIEWING_STATUSES

    return frozenset(s.value for s in INTERVIEWING_STATUSES)


#: The order rows are listed in: live conversations first. A person reading a
#: one-page report wants the offer on the first line, not the oldest cold email.
#: This is *not* the CSV's order, and that is not an inconsistency — the CSV is
#: a record and is chronological, this is a status report.
_REPORT_ORDER = (
    ApplicationStatus.OFFER,
    ApplicationStatus.INTERVIEW_SCHEDULED,
    ApplicationStatus.SCHEDULING,
    ApplicationStatus.INTERESTED,
    ApplicationStatus.REPLIED,
    ApplicationStatus.FOLLOW_UP,
    ApplicationStatus.OUTREACH_SENT,
    ApplicationStatus.QUEUED,
)


def pdf_available() -> bool:
    """Whether a PDF can be rendered in this deployment."""
    from app.services import resume_pdf

    return resume_pdf.is_available()


@dataclass(frozen=True)
class ReportSummary:
    """The headline numbers the report leads with."""

    total: int
    sent: int
    replied: int
    interviewing: int
    reply_rate: float
    #: Replies among the outreaches this user actually sent — the numerator the
    #: rate above is over. Separate from ``replied`` because they are different
    #: populations: ``replied`` counts every live conversation, including the
    #: ones a recruiter opened, and those had no outreach to reply to.
    replied_to_outreach: int = 0


def _status_of(row) -> str:
    status = row.status
    return status.value if hasattr(status, "value") else str(status)


def summarise(rows) -> ReportSummary:
    """Fold the dashboard's application rows into the report's headline.

    Computed from the rows the report was handed rather than by re-querying, so
    the header and the table below it can never describe different sets — a
    report saying "12 replied" above a table showing nine is worse than one
    saying nothing at all.
    """
    rows = list(rows)
    engaged, interviewing = _engaged(), _interviewing()

    total = len(rows)
    # "Sent" is rows with a delivery, not rows that exist. A queued outreach has
    # not reached anybody, and counting it here would put it in the denominator
    # of the reply rate below.
    delivered = [r for r in rows if r.first_sent_at is not None]
    sent = len(delivered)
    replied = sum(1 for r in rows if _status_of(r) in engaged)
    # The rate's numerator is *not* `replied`. A conversation a recruiter opened
    # is created already at REPLIED and has no outreach behind it, so it counts
    # as a reply and contributes nothing to `sent` — and the inbound path is the
    # bulk of a real pipeline, not a rarity. Dividing the whole engaged count by
    # `sent` printed rates like "3 replied (300% of sent)" on the report.
    #
    # Numerator and denominator are now the same population: outreach that went
    # out, and the part of it that got an answer.
    replied_to_outreach = sum(1 for r in delivered if _status_of(r) in engaged)
    return ReportSummary(
        total=total,
        sent=sent,
        replied=replied,
        interviewing=sum(1 for r in rows if _status_of(r) in interviewing),
        reply_rate=round(replied_to_outreach / sent, 3) if sent else 0.0,
        replied_to_outreach=replied_to_outreach,
    )


def render_pdf(user: User, rows, *, now: datetime | None = None) -> bytes:
    """Render the report. Raises ``PDFNotAvailable`` when reportlab is absent.

    *rows* are ``app.schemas.dashboard.ApplicationRow`` objects — whatever
    ``dashboard()`` returned for the caller's filters.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    from app.services.resume_pdf import PDFNotAvailable, _escape, is_available

    if not is_available():  # pragma: no cover - exercised only in slim envs
        raise PDFNotAvailable("reportlab is not installed")

    rows = list(rows)
    now = now or datetime.now(UTC)
    summary = summarise(rows)

    base = getSampleStyleSheet()
    title = ParagraphStyle("pipeline_title", parent=base["Title"], fontSize=16, spaceAfter=2)
    meta = ParagraphStyle("pipeline_meta", parent=base["Normal"], fontSize=8, textColor=colors.grey)
    body = ParagraphStyle("pipeline_body", parent=base["Normal"], fontSize=9.5, leading=13)
    cell = ParagraphStyle("pipeline_cell", parent=base["Normal"], fontSize=7.5, leading=9.5)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title="DoAide AutoApply pipeline report",
        author="DoAide AutoApply",
    )

    story = [
        Paragraph("Pipeline report", title),
        Paragraph(
            f"{_escape(user.full_name or user.email)} &middot; {now.date().isoformat()}",
            meta,
        ),
        Spacer(1, 8),
        HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey),
        Spacer(1, 10),
        # "Conversations", not "outreaches": a thread a recruiter opened is on
        # this report and was never outreach. The reply rate gets its own line
        # below rather than a parenthetical here, because its numerator is a
        # different number from the `replied` beside it and two figures sharing
        # a bracket read as one over the other.
        Paragraph(
            f"<b>{summary.total}</b> conversations &middot; "
            f"<b>{summary.sent}</b> outreach sent &middot; "
            f"<b>{summary.replied}</b> replied &middot; "
            f"<b>{summary.interviewing}</b> in interviews",
            body,
        ),
        Spacer(1, 4),
        Paragraph(_reply_rate_line(summary), meta),
        Spacer(1, 12),
    ]

    if rows:
        rank = {status.value: i for i, status in enumerate(_REPORT_ORDER)}
        listed = sorted(
            rows,
            key=lambda r: (
                rank.get(_status_of(r), len(_REPORT_ORDER)),
                -r.application_id,
            ),
        )[:PDF_ROW_LIMIT]

        table_data = [
            [
                Paragraph("<b>Company</b>", cell),
                Paragraph("<b>Role</b>", cell),
                Paragraph("<b>Contact</b>", cell),
                Paragraph("<b>Status</b>", cell),
                Paragraph("<b>Sent</b>", cell),
                Paragraph("<b>Replied</b>", cell),
            ]
        ]
        for row in listed:
            table_data.append(
                [
                    # Everything a stranger wrote goes through `_escape`:
                    # reportlab parses a Paragraph as mini-markup, so an
                    # unescaped `<` in a company name raises mid-render and the
                    # download 500s.
                    Paragraph(_escape(row.company or "—"), cell),
                    Paragraph(_escape(row.role or "—"), cell),
                    Paragraph(_escape(row.contact or "—"), cell),
                    Paragraph(_escape(_status_of(row).replace("_", " ").lower()), cell),
                    Paragraph(_date(row.first_sent_at), cell),
                    Paragraph(_date(row.replied_at), cell),
                ]
            )

        table = Table(
            table_data,
            colWidths=[1.3 * inch, 1.7 * inch, 1.6 * inch, 1.1 * inch, 0.7 * inch, 0.7 * inch],
            repeatRows=1,
        )
        table.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.Color(0.97, 0.97, 0.97)],
                    ),
                ]
            )
        )
        story.append(table)

        if len(rows) > PDF_ROW_LIMIT:
            # Said on the page, not just implied by the row count. A report that
            # silently listed the first forty of four hundred is a lie of the
            # most useful-looking kind.
            story.append(Spacer(1, 8))
            story.append(
                Paragraph(
                    f"{len(rows) - PDF_ROW_LIMIT} more not listed — the totals "
                    "above cover all of them. Export the CSV for the full table.",
                    meta,
                )
            )
    else:
        story.append(Paragraph("No outreach yet.", body))

    doc.build(story)
    return buffer.getvalue()


def _reply_rate_line(summary: ReportSummary) -> str:
    """The reply rate, spelled out as the fraction it actually is.

    A bare percentage invites the reader to check it against the two numbers
    above, and it will not divide: `replied` counts every live conversation and
    this counts only the ones that answered outreach. So both terms are printed.

    The trailing clause appears only when the two populations differ, which is
    the only time the difference needs explaining — on a pipeline that is all
    outbound there is nothing to caveat and the sentence would just be noise.
    """
    if not summary.sent:
        return "No outreach sent yet, so there is no reply rate to report."
    noun = "outreach" if summary.sent == 1 else "outreaches"
    line = (
        f"Reply rate {summary.reply_rate:.0%} — {summary.replied_to_outreach} "
        f"of the {summary.sent} {noun} sent got a reply."
    )
    inbound = summary.replied - summary.replied_to_outreach
    if inbound > 0:
        line += (
            f" The other {inbound} counted above are conversations a recruiter "
            "started, which had no outreach to answer."
        )
    return line


def _date(value: datetime | None) -> str:
    return value.date().isoformat() if value else "—"


def filename(*, now: datetime | None = None) -> str:
    """``talentping-pipeline-2026-08-23.pdf``.

    Matches the CSV export's naming exactly, minus the extension: the thing
    people do with these is accumulate them in a downloads folder, and two files
    from the same day about the same pipeline should sort together.
    """
    stamp = (now or datetime.now(UTC)).date().isoformat()
    return f"talentping-pipeline-{stamp}.pdf"
