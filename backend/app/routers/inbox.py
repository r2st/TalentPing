"""Inbox — every email on every conversation, sent and received.

The review queue is draft-shaped: it lists messages awaiting approval. The inbox
is thread-shaped and complete: every conversation the user has, the outreach and
follow-ups sent on their behalf, whatever came back, and the full history of
both. It used to list only threads a recruiter had replied to, which meant a
mailbox full of sent applications read as an empty inbox.

    GET  /inbox?direction=all|sent|received  -> conversations + headline counts
    GET  /inbox/threads/{id}                 -> one conversation, full history
    GET  /inbox/emails/{id}/attachments/{n}  -> the nth attachment, as bytes
    GET  /inbox/emails/{id}/attachments/{n}/preview
                                             -> the same file, as something a
                                                browser can draw
    POST /inbox/threads/{id}/read            -> mark its inbound mail as read
    POST /inbox/sync                         -> poll Gmail for new mail now

The list is paged; a thread's detail still returns every message on it, oldest
first. The counts, and the sort the page is cut from, are computed over the
user's whole history regardless of the window — see :func:`_thread_facts`, which
is what makes reading all of it cheap enough to keep doing.

Acting on a draft reply stays with the review endpoints (approve/dismiss) and
``PATCH /tracker/emails/{id}`` (edit), so a draft has exactly one code path
whichever page the user is looking at.
"""
from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import and_, case, func, not_, select
from sqlalchemy.orm import Session, aliased, selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import Page, page_params
from app.core.rate_limit import rate_limit
from app.core.sql_text import SEARCH_MAX_LENGTH, search_clause
from app.core.uploads import read_capped, safe_content_type, safe_upload_filename
from app.models.application import CLOSED_STATUSES, Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus, ReplyIntent
from app.models.email_attachment import EmailAttachment
from app.models.email_thread import EmailThread
from app.models.job import JobPosting
from app.models.recruiter import Recruiter
from app.models.resume import Resume
from app.models.user import User
from app.schemas.inbox import (
    AttachmentItem,
    AttachmentSettings,
    DirectionFilter,
    EmailAttachmentsOut,
    InboxCounts,
    InboxMessage,
    InboxOut,
    InboxSyncOut,
    InboxThread,
    InboxThreadDetail,
    ResumeOption,
)
from app.services import document_preview, email_attachments

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/inbox", tags=["inbox"])

# Polling every thread inline would make the request as slow as the mailbox is
# large, so a manual sync without a worker refreshes the most recent ones.
_INLINE_SYNC_LIMIT = 20

SNIPPET_CHARS = 240


def _owned_threads(
    db: Session, user: User, ids: Collection[int] | None = None
) -> list[EmailThread]:
    """Threads belonging to *user*, with everything a list row reads.

    The eager loads are the endpoint's performance contract, not a micro
    optimisation. A summary names the company, the recruiter and the role, which
    live on three tables away from the thread; fetching them per row cost four
    queries per conversation, so the inbox got linearly slower for exactly the
    users who used the product most. ``tests/test_query_budget.py`` measures the
    slope and fails if it returns.

    They are also the reason *ids* exists. ``selectinload(EmailThread.emails)``
    pulls every message of every thread named here, and a message carries
    ``body_text``, which is unbounded ``Text``. Loading the whole mailbox to
    render one screen is a per-request memory cost that grows with the account
    and is paid inside the API process. The list endpoint therefore decides
    *which* conversations it wants from :func:`_thread_facts` — which reads no
    bodies at all — and calls this only for that page.
    """
    stmt = (
        select(EmailThread)
        .join(Application, EmailThread.application_id == Application.id)
        .where(Application.user_id == user.id)
        .options(
            selectinload(EmailThread.emails),
            selectinload(EmailThread.application).selectinload(Application.recruiter),
            selectinload(EmailThread.application).selectinload(Application.campaign),
        )
        .order_by(EmailThread.id)
    )
    if ids is not None:
        if not ids:
            return []
        stmt = stmt.where(EmailThread.id.in_(ids))
    return list(db.scalars(stmt).all())


def _postings_by_id(
    db: Session, applications: list[Application]
) -> dict[int, JobPosting]:
    """The postings those applications targeted, in one query.

    Separate from the eager loads above because ``Application`` deliberately has
    no ``job_posting`` relationship — the FK is ``SET NULL`` so that pruning the
    job feed cannot delete application history, and a posting a row points at may
    simply be gone. A dict lookup that misses is the same answer as a null FK.
    """
    ids = {
        application.job_posting_id
        for application in applications
        if application.job_posting_id is not None
    }
    if not ids:
        return {}
    rows = db.scalars(select(JobPosting).where(JobPosting.id.in_(ids))).all()
    return {posting.id: posting for posting in rows}


def _role_for(
    application: Application,
    campaign: Campaign | None,
    postings: dict[int, JobPosting],
) -> str | None:
    """The role this conversation is about: the exact posting when the outreach
    targeted one, otherwise the campaign's first target role."""
    if application.job_posting_id is not None:
        posting = postings.get(application.job_posting_id)
        if posting is not None and posting.title:
            return posting.title
    if campaign is not None and campaign.target_roles:
        return campaign.target_roles[0]
    return None


def _is_draft(email: Email) -> bool:
    """An outbound message the agent wrote that nobody has approved yet."""
    return email.direction == EmailDirection.SENT and email.status == EmailStatus.DRAFT


def _attachments_for(
    db: Session,
    email: Email,
    *,
    prefetch: email_attachments.PlanPrefetch | None = None,
) -> dict:
    """What this message carries, as ``InboxMessage`` wants it.

    A draft is *resolved* — attachments are decided at send time, so the honest
    answer for something not yet sent is "what would travel if you approved it
    now". A sent message reports what is on the row: the filename that actually
    went, since re-resolving it would answer with today's resume instead. Both
    come off :func:`email_attachments.files_of_record`, which is also what the
    endpoint serving one of them resolves against.

    Received mail is neither. What it carries is what the sender attached, and
    that is recorded at ingest — the inbox used to read ``attachment_filename``
    for every message, a column only the *sender* ever writes, so a recruiter's
    job spec or signed offer was invisible on the one screen built to read it.
    """
    if email.direction == EmailDirection.RECEIVED:
        return {"attachments": email_attachments.inbound_filenames_for(email)}

    # Outbound, either way, off one resolver. A sent message names the recorded
    # resume, the letter that travelled and then the files the user attached by
    # hand; anything not yet sent names what would travel if it went now. The
    # order matters as much as the contents: opening an attachment resolves it
    # by *position* against this list, so building it a second way here — which
    # is how it used to be done — makes every position after a difference open
    # the wrong document.
    files = email_attachments.files_of_record(db, email, prefetch=prefetch)
    return {
        "attachments": [item.filename for item in files],
        # Only a draft has a "why not". A sent message with no resume named is
        # the record of a send that carried none, not a problem to report now.
        "attachment_note": (
            email_attachments.plan_for_email(db, email, prefetch=prefetch).reason
            if _is_draft(email)
            else None
        ),
        "attachment_items": [
            AttachmentItem(
                index=index,
                filename=item.filename,
                kind=item.kind,
                attachment_id=item.attachment_id,
            )
            for index, item in enumerate(files)
        ],
    }


# --------------------------------------------------------------------------- #
# Whose turn is it                                                             #
# --------------------------------------------------------------------------- #

# Replies that end a conversation. A recruiter who passed, or a contact who
# asked to be left alone, is not "quiet" — they answered, and the answer was no.
# Chasing either is the single worst thing this product could suggest.
_TERMINAL_INTENTS = frozenset({ReplyIntent.NOT_INTERESTED, ReplyIntent.UNSUBSCRIBE})


def _quiet_days(
    *,
    last_direction: EmailDirection | None,
    last_spoken_at: datetime | None,
    has_inbound: bool,
    has_draft: bool,
    last_intent: ReplyIntent | None,
    application_status: ApplicationStatus,
    now: datetime,
) -> int | None:
    """Days a live conversation has sat on the candidate's last word, or None.

    "Live" is the load-bearing word, and it is why this is not simply "nothing
    happened for a week":

    * **The recruiter must have written at least once.** Cold outreach nobody
      answered is already the follow-up sequence's job — it schedules the
      chases and cancels them when a reply lands (docs/features/
      follow-up-sequences.md). Flagging it here would put a second, contradictory
      nudge on every unanswered application in the account.
    * **We must have spoken last.** If they spoke last the ball is on this desk,
      which the draft badges and the unread dot already say.
    * **No draft may be waiting.** There is a reply sitting right there; the
      thread is not stalled, the user just has not sent it yet.
    * **Nothing terminal.** A rejection, an opt-out, or a closed application is
      finished, whichever side said so last.

    Returns whole days rather than a boolean so the caller can phrase it, and
    ``None`` — not ``0`` — when the thread is not waiting on anyone. Zero is a
    real answer here: a thread that went quiet today under a 0-day threshold.
    """
    if not has_inbound or has_draft:
        return None
    if last_direction is not EmailDirection.SENT or last_spoken_at is None:
        return None
    if last_intent in _TERMINAL_INTENTS:
        return None
    if application_status in CLOSED_STATUSES:
        return None
    elapsed = (now - last_spoken_at).days
    return elapsed if elapsed >= 0 else None


def _when(email: Email) -> datetime:
    """When a message happened: its send time, else when the row was written.

    Backfilled mail carries the real Gmail timestamp, so this is what history
    has to be ordered by rather than the row id. Naive values (SQLite hands
    timestamps back without a zone) are read as UTC so the two never get
    compared against each other.
    """
    when = email.sent_at or email.created_at
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _summarise(
    thread: EmailThread, postings: dict[int, JobPosting], *, now: datetime | None = None
) -> InboxThread | None:
    """Build the list row for a thread, or None if it has no application.

    Every thread is a row, whether or not a recruiter has written back — an
    outreach nobody answered is still mail the user sent and wants to see.

    Takes no session: everything it reads is either on the thread already or in
    *postings*. That is what keeps the list O(1) in queries — a helper holding a
    ``Session`` is a helper that will eventually be given a reason to use it.
    """
    application = thread.application
    if application is None:
        return None
    recruiter = application.recruiter
    campaign = application.campaign

    inbound = [e for e in thread.emails if e.direction == EmailDirection.RECEIVED]
    outbound = [
        e
        for e in thread.emails
        if e.direction == EmailDirection.SENT and not _is_draft(e)
    ]
    # The row headline comes from the last thing actually said, either way.
    spoken = sorted(inbound + outbound, key=_when)
    latest = spoken[-1] if spoken else None

    draft = next((e for e in reversed(thread.emails) if _is_draft(e)), None)
    # A thread whose only message is an unapproved draft has had nothing said on
    # it yet. It is still the user's mail, so it lists — previewed by the draft,
    # and with no direction, because nothing has gone anywhere.
    preview = latest or draft
    last_inbound_at = _when(inbound[-1]) if inbound else None
    last_outbound_at = max((_when(e) for e in outbound), default=None)

    quiet_days = _quiet_days(
        last_direction=latest.direction if latest else None,
        last_spoken_at=_when(latest) if latest else None,
        has_inbound=bool(inbound),
        has_draft=draft is not None,
        last_intent=inbound[-1].intent if inbound else None,
        application_status=application.status,
        now=now or datetime.now(UTC),
    )

    return InboxThread(
        thread_id=thread.id,
        application_id=application.id,
        subject=thread.subject or (preview.subject if preview else None),
        company=recruiter.company if recruiter else None,
        recruiter_name=(recruiter.name or recruiter.email) if recruiter else None,
        recruiter_email=recruiter.email if recruiter else None,
        role=_role_for(application, campaign, postings),
        application_status=application.status,
        last_intent=inbound[-1].intent if inbound else None,
        snippet=(preview.body_text or "")[:SNIPPET_CHARS] if preview else None,
        last_direction=latest.direction if latest else None,
        message_count=len(thread.emails),
        inbound_count=len(inbound),
        outbound_count=len(outbound),
        unread_count=sum(1 for e in inbound if e.read_at is None),
        last_inbound_at=last_inbound_at,
        last_outbound_at=last_outbound_at,
        last_activity_at=_when(preview) if preview else thread.last_message_at,
        last_message_at=thread.last_message_at,
        draft_email_id=draft.id if draft else None,
        needs_attention=bool(draft is not None and draft.needs_attention),
        attention_reason=draft.attention_reason if draft is not None else None,
        quiet_days=quiet_days,
        gone_quiet=(
            quiet_days is not None and quiet_days >= settings.inbox_quiet_after_days
        ),
    )


def _summarise_one(db: Session, thread: EmailThread) -> InboxThread | None:
    """``_summarise`` for the endpoints that hold a single thread.

    One thread needs no batching, but it still needs the posting, so the lookup
    goes through the same map rather than a second code path that could drift
    from the list's answer.
    """
    application = thread.application
    return _summarise(thread, _postings_by_id(db, [application] if application else []))


# --------------------------------------------------------------------------- #
# What the list knows before it loads anything                                 #
# --------------------------------------------------------------------------- #


@dataclass
class _ThreadFacts:
    """Everything the inbox list needs about a thread except its text.

    The headline counts are computed over the user's whole history and the sort
    is over all of it too, so *something* has to touch every thread on every
    request. The question is what it costs to touch one. Summarising a thread
    costs its messages, and a message carries an unbounded body — so the old
    path held every email the account had ever seen in memory to produce a
    handful of integers and a 240-character snippet.

    These facts are the integers without the text: a dozen small scalars per
    *conversation*, aggregated by the database. They answer every count, every
    structural filter and the sort, which is enough to decide which page of
    conversations to load in full. :func:`_thread_facts` builds them and is
    where the cost of "touch every thread" is actually paid.

    The aggregation deliberately mirrors :func:`_summarise` rather than
    improving on it — ``last_intent`` is the highest-*id* inbound message's
    intent, matching ``inbound[-1]`` over a relationship ordered by id, and
    ``activity_at`` is the newest *spoken* message's timestamp even when an
    unapproved draft is newer. Two answers to "the latest message" would be a
    bug wherever they disagreed.
    """

    thread_id: int
    last_message_at: datetime | None = None
    # The application's own state, carried because "is this conversation still
    # live" cannot be answered from the mail alone — a recruiter who passed
    # leaves a thread whose last word is ours and which must never be chased.
    application_status: ApplicationStatus = ApplicationStatus.QUEUED
    has_inbound: bool = False
    has_outbound: bool = False
    has_unread: bool = False
    has_draft: bool = False
    # A draft the agent held back rather than sent. Strictly narrower than
    # ``has_draft`` — see ``InboxCounts.needs_attention``.
    has_attention: bool = False
    last_intent: ReplyIntent | None = None
    # Newest spoken message, and newest draft, kept apart: a draft only dates
    # the thread when nothing has actually been said on it.
    _spoken_at: datetime | None = None
    _spoken_direction: EmailDirection | None = None
    _draft_at: datetime | None = None

    @property
    def activity_at(self) -> datetime | None:
        return self._spoken_at or self._draft_at or self.last_message_at

    @property
    def sort_key(self) -> tuple[bool, datetime, int]:
        """Newest conversation first, with the tie-break a paged window needs.

        Threads with no timestamp at all sink to the bottom rather than sorting
        against ``datetime.min``, which is why the first element is a flag.

        Timestamps collide constantly here — a campaign sends its outreach in
        one pass, so a hundred threads can share a send time to the second. On
        an unpaged list the order within a tie did not matter; on a paged one a
        tie that resolves differently between two requests puts a conversation
        on both pages and its neighbour on neither.
        """
        when = self.activity_at
        return (
            when is not None,
            when or datetime.min.replace(tzinfo=UTC),
            self.thread_id,
        )

    def quiet_days(self, now: datetime) -> int | None:
        """Days this conversation has waited on the recruiter — see `_quiet_days`.

        Routed through the same function `_summarise` uses so the count in the
        chip and the badge on the row can never disagree about one thread.
        """
        return _quiet_days(
            last_direction=self._spoken_direction,
            last_spoken_at=self._spoken_at,
            has_inbound=self.has_inbound,
            has_draft=self.has_draft,
            last_intent=self.last_intent,
            application_status=self.application_status,
            now=now,
        )


def _thread_facts(db: Session, user: User) -> dict[int, _ThreadFacts]:
    """Fold the user's whole mailbox into one small record per conversation.

    One query, one row per conversation. This fold used to run in Python: the
    endpoint selected eight scalars for *every message the account had ever
    seen* and folded them one at a time. That is the right answer computed on
    the wrong side of the wire. A mailbox does not mainly grow by gaining
    conversations, it grows by gaining replies on the ones it has — so the rows
    that path dragged back rose faster than the rows it returned, and the badge
    below polls it every thirty seconds on every screen in the product.
    Measured on Postgres against 10,000 threads, the Python fold cost 247ms at
    four messages each and 716ms at twelve; on the twelve-message mailbox this
    costs 290ms, and unlike the old one it is flat in messages-per-thread,
    because what crosses the wire is one row per conversation either way.

    The aggregate mirrors the fold it replaced clause for clause rather than
    improving on it — same draft test, same "spoken" exclusion, same
    "highest-id inbound message names the intent" — and was checked against it
    field by field over 10,000 seeded conversations. The two subqueries exist
    because two of the facts are *arg-max* rather than aggregates: the direction
    belonging to the newest spoken message, and the intent belonging to the
    newest inbound one. Each is resolved by aggregating the id and joining the
    row back.

    One deliberate difference. Where several spoken messages share the newest
    timestamp to the microsecond, the Python fold kept whichever the cursor
    happened to hand it first; an unordered query makes that the planner's
    choice, not a contract, and a parallel scan could have answered differently
    on the same data. This takes the highest id — the last row written among
    them — which is at least the same answer twice.
    """
    when = func.coalesce(Email.sent_at, Email.created_at)
    is_draft = and_(
        Email.direction == EmailDirection.SENT, Email.status == EmailStatus.DRAFT
    )
    is_inbound = Email.direction == EmailDirection.RECEIVED

    # ``max(case(...))`` rather than ``bool_or``/``filter``: SQLite runs this
    # suite, and only one of the three spellings exists in both dialects.
    folded = (
        select(
            Email.thread_id.label("thread_id"),
            func.max(case((is_draft, when))).label("draft_at"),
            func.max(case((not_(is_draft), when))).label("spoken_at"),
            func.max(case((is_draft, 1), else_=0)).label("has_draft"),
            func.max(
                case((and_(is_draft, Email.needs_attention.is_(True)), 1), else_=0)
            ).label("has_attention"),
            func.max(case((is_inbound, 1), else_=0)).label("has_inbound"),
            func.max(
                case((and_(is_inbound, Email.read_at.is_(None)), 1), else_=0)
            ).label("has_unread"),
            func.max(
                case((and_(not_(is_inbound), not_(is_draft)), 1), else_=0)
            ).label("has_outbound"),
            func.max(case((is_inbound, Email.id))).label("last_inbound_id"),
        )
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(Application.user_id == user.id)
        .group_by(Email.thread_id)
        # A CTE, not an inline subquery: this is referenced twice — once by
        # `newest_spoken` and once by the statement below — and a subquery
        # would be evaluated once per reference, folding the mailbox twice.
        .cte("folded")
    )
    # The newest spoken message, by the timestamp the fold just found.
    #
    # The ownership join is repeated rather than inherited from `folded`. Every
    # row this can return is already one of the user's — `folded` only names
    # their threads — so as a filter it is redundant, and it is not there as
    # one. It is there because a join whose only restriction arrives through a
    # subquery gives the planner nothing to start from: measured against ten
    # tenants holding 1.2M messages between them, Postgres hash-joined the
    # whole ``emails`` table against the fold and the statement took 618ms.
    # Spelled out, the same statement drives from ``ix_applications_user_id_id``
    # into ``ix_emails_thread_id`` and reads only this mailbox.
    newest_spoken = (
        select(
            Email.thread_id.label("thread_id"),
            func.max(Email.id).label("spoken_id"),
        )
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .join(folded, folded.c.thread_id == Email.thread_id)
        .where(
            Application.user_id == user.id,
            not_(is_draft),
            when == folded.c.spoken_at,
        )
        .group_by(Email.thread_id)
        .subquery()
    )
    spoken = aliased(Email)
    last_inbound = aliased(Email)

    # Outer joins throughout: a thread with no messages at all is still a
    # conversation the user owns and still lists, dated by `last_message_at`
    # alone. It arrives here with every aggregate null, which is what the
    # dataclass defaults already say.
    stmt = (
        select(
            EmailThread.id,
            EmailThread.last_message_at,
            Application.status,
            folded.c.draft_at,
            folded.c.spoken_at,
            folded.c.has_draft,
            folded.c.has_attention,
            folded.c.has_inbound,
            folded.c.has_unread,
            folded.c.has_outbound,
            spoken.direction.label("spoken_direction"),
            last_inbound.intent.label("last_intent"),
        )
        .select_from(EmailThread)
        .join(Application, EmailThread.application_id == Application.id)
        .outerjoin(folded, folded.c.thread_id == EmailThread.id)
        .outerjoin(newest_spoken, newest_spoken.c.thread_id == EmailThread.id)
        .outerjoin(spoken, spoken.id == newest_spoken.c.spoken_id)
        .outerjoin(last_inbound, last_inbound.id == folded.c.last_inbound_id)
        .where(Application.user_id == user.id)
    )

    return {
        row.id: _ThreadFacts(
            thread_id=row.id,
            last_message_at=_utc(row.last_message_at),
            application_status=row.status,
            has_inbound=bool(row.has_inbound),
            has_outbound=bool(row.has_outbound),
            has_unread=bool(row.has_unread),
            has_draft=bool(row.has_draft),
            has_attention=bool(row.has_attention),
            last_intent=row.last_intent,
            _spoken_at=_utc(row.spoken_at),
            _spoken_direction=row.spoken_direction,
            _draft_at=_utc(row.draft_at),
        )
        for row in db.execute(stmt)
    }


def _utc(when: datetime | None) -> datetime | None:
    """``_when``'s zone handling, for a value read off a row rather than an ORM
    object. SQLite hands timestamps back naive; comparing those against aware
    ones raises rather than sorting wrong, so it would not go unnoticed — but it
    would go unnoticed until a mailbox happened to hold both."""
    if when is None:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _searched_ids(db: Session, user: User, q: str | None) -> set[int] | None:
    """Thread ids matching *q*, decided by the database — or ``None`` for no search.

    ``None`` and ``set()`` are the two results a caller must not confuse: the
    first is "the box was empty, show everything", the second is "the box had a
    word in it and nothing matched". `search_clause` owns that distinction for
    every search in the API, including which columns carry the escape argument,
    so this holds none of it — it only names the columns an inbox search reads.

    ``q`` used to be a substring test over the summaries the endpoint had
    already built, which is only possible when every summary exists — the whole
    point of the facts above is that they do not. So it moves into SQL, where it
    is also a better search than it was: the old one looked at the snippet, so a
    word 241 characters into a message was unfindable, and the message it was in
    was invisible unless it happened to be the newest on its thread.

    This is the one read here whose cost is still the size of the mailbox rather
    than the size of the answer, and it is not an indexing oversight. A
    substring match open at the front cannot use a b-tree, so every body has to
    be looked at: measured on Postgres against a mailbox of 10,000 threads and
    120,000 messages, 456ms, whether the needle hit one conversation or all of
    them. The *scoping* is right — the plan drives from
    ``ix_applications_user_id_id`` into ``ix_emails_thread_id`` and reads only
    this user's mail, which is the part that would otherwise get worse as the
    deployment gained tenants. Making the match itself cheap needs a trigram
    index and the extension behind it, which is a schema decision rather than a
    query one.
    """
    clause = search_clause(
        q,
        Recruiter.company,
        Recruiter.name,
        Recruiter.email,
        EmailThread.subject,
        Email.subject,
        Email.body_text,
    )
    if clause is None:
        return None
    return set(
        db.scalars(
            select(EmailThread.id)
            # One row per *thread*, not one per matching message. Without it a
            # needle common enough to hit every body — which is what a
            # two-letter query is — returns the whole mailbox's message count
            # for the database to send and Python to collapse into the same set
            # the database could have collapsed it into.
            .distinct()
            .join(Application, EmailThread.application_id == Application.id)
            .outerjoin(Recruiter, Application.recruiter_id == Recruiter.id)
            .outerjoin(Email, Email.thread_id == EmailThread.id)
            .where(Application.user_id == user.id, clause)
        ).all()
    )


def _quiet_by_thread(
    facts: dict[int, _ThreadFacts], now: datetime
) -> dict[int, int]:
    """Threads sitting on the candidate's last word past the threshold, and for
    how long. Keyed rather than counted because the list filter needs the ids
    and the counts only need the size."""
    return {
        thread_id: days
        for thread_id, fact in facts.items()
        if (days := fact.quiet_days(now)) is not None
        and days >= settings.inbox_quiet_after_days
    }


def _counts(
    facts: dict[int, _ThreadFacts], quiet_by_thread: dict[int, int]
) -> InboxCounts:
    """The headline numbers, over the whole mailbox and before any filter.

    One definition, used by the list and by the badge endpoint below — the same
    reason `_draft_count` exists in `routers/review`. A badge computed
    separately from the list it labels is a badge that will eventually disagree
    with it, and the user has no way to tell which one is lying.
    """
    # Only replied-to threads carry an intent, so an outreach awaiting an answer
    # must not land in an "other" bucket and inflate the chips.
    by_intent: dict[str, int] = {}
    for fact in facts.values():
        if fact.last_intent is None:
            continue
        by_intent[fact.last_intent.value] = by_intent.get(fact.last_intent.value, 0) + 1

    return InboxCounts(
        threads=len(facts),
        sent=sum(1 for f in facts.values() if f.has_outbound),
        received=sum(1 for f in facts.values() if f.has_inbound),
        unread=sum(1 for f in facts.values() if f.has_unread),
        awaiting_reply=sum(1 for f in facts.values() if f.has_draft),
        needs_attention=sum(1 for f in facts.values() if f.has_attention),
        gone_quiet=len(quiet_by_thread),
        by_intent=by_intent,
    )


@router.get("/counts", response_model=InboxCounts)
def inbox_counts(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InboxCounts:
    """Just the numbers, for the nav badge.

    The badge in the app shell polls every thirty seconds on every route, and it
    reads exactly one integer: `unread`. Against `GET /inbox` that cost a page
    of up to two hundred threads with every message body eagerly loaded, the
    posting lookup behind them, and a summary built for each — four times a
    minute, for one number, on every screen in the product.

    `review/count` exists for the same reason and says the same thing about the
    draft queue. The counts here come from the same `_counts` the list uses, so
    the badge and the page it links to cannot disagree.

    `_thread_facts` is the whole cost of this: two queries, neither selecting a
    body. It reads the account's entire mailbox, which is the point — the counts
    are a claim about all of it, not about a page.
    """
    facts = _thread_facts(db, user)
    return _counts(facts, _quiet_by_thread(facts, datetime.now(UTC)))


@router.get("", response_model=InboxOut)
def inbox(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    direction: DirectionFilter = Query(
        default=DirectionFilter.ALL,
        description="all (default), sent, or received",
    ),
    intent: ReplyIntent | None = Query(default=None),
    unread: bool = Query(default=False),
    needs_reply: bool = Query(default=False, description="Only threads with a draft"),
    needs_attention: bool = Query(
        default=False, description="Only threads with a draft the agent held back"
    ),
    gone_quiet: bool = Query(
        default=False,
        description="Only live conversations waiting on the recruiter past the threshold",
    ),
    q: str | None = Query(
        default=None,
        max_length=SEARCH_MAX_LENGTH,
        description="Match company, name or message",
    ),
    page: Page = Depends(page_params),
) -> InboxOut:
    """The user's conversations, newest activity first, unfiltered by date.

    Counts are computed over everything the user has, *before* filters, so the
    filter chips keep showing what they would reveal rather than what is left.
    That is a claim about the whole mailbox, and it survives paging: the counts
    come from :func:`_thread_facts`, which reads every conversation the account
    has and none of their text.

    The page is the part that is bounded, and it is bounded because the rows are
    expensive rather than because the list is long. Building a summary means
    loading a thread's messages, and a message body has no size limit — so the
    endpoint used to hold an account's entire mail history in the API process to
    render one screen, and got heavier every week the account stayed in use.
    Only the conversations on this page are loaded now.

    ``matched`` and ``has_more`` (and the usual ``X-Total-Count`` /
    ``X-Has-More``) describe the *filtered* list, which is a different number
    from ``counts.threads``: one says how many conversations the current filters
    select, the other how many exist. A client needs both — the first to know
    whether it is showing all of what it asked for, the second to label a chip
    with what it would reveal.
    """
    facts = _thread_facts(db, user)
    now = datetime.now(UTC)
    quiet_by_thread = _quiet_by_thread(facts, now)
    counts = _counts(facts, quiet_by_thread)

    matching = list(facts.values())
    if direction is DirectionFilter.SENT:
        matching = [f for f in matching if f.has_outbound]
    elif direction is DirectionFilter.RECEIVED:
        matching = [f for f in matching if f.has_inbound]
    if intent is not None:
        matching = [f for f in matching if f.last_intent == intent]
    if unread:
        matching = [f for f in matching if f.has_unread]
    if needs_reply:
        matching = [f for f in matching if f.has_draft]
    if gone_quiet:
        matching = [f for f in matching if f.thread_id in quiet_by_thread]
    if needs_attention:
        matching = [f for f in matching if f.has_attention]
    if (found := _searched_ids(db, user, q)) is not None:
        matching = [f for f in matching if f.thread_id in found]

    matching.sort(key=lambda f: f.sort_key, reverse=True)
    window = matching[page.offset : page.offset + page.limit]

    # The page, and only the page, is loaded with its messages attached.
    threads = _owned_threads(db, user, [f.thread_id for f in window])
    postings = _postings_by_id(db, [t.application for t in threads if t.application])
    summaries = {
        thread.id: row
        for thread in threads
        # The same clock the counts were taken on. Two `datetime.now()` calls a
        # few milliseconds apart can straddle midnight, and then one thread is
        # in the chip's count and not badged on its own row.
        if (row := _summarise(thread, postings, now=now))
    }
    # Ordered by the facts, not by what came back: `IN (...)` has no order, and
    # re-sorting the summaries would be a second implementation of the sort.
    rows = [summaries[f.thread_id] for f in window if f.thread_id in summaries]

    has_more = page.offset + len(window) < len(matching)
    response.headers["X-Total-Count"] = str(len(matching))
    response.headers["X-Has-More"] = "true" if has_more else "false"
    return InboxOut(
        counts=counts,
        threads=rows,
        matched=len(matching),
        has_more=has_more,
        quiet_after_days=settings.inbox_quiet_after_days,
    )


def _owned_thread(db: Session, user: User, thread_id: int) -> EmailThread:
    """One thread belonging to *user*, loaded the way its summary reads it.

    The eager loads mirror :func:`_owned_threads` for one row rather than a
    page: `_summarise` names the company, the recruiter and the role, and the
    attachment prefetch below walks `Email.thread.application` to learn which
    posting to ask about. Left lazy, that is the same handful of queries a
    little later — but issued from inside a loop, which is where they stop
    being a handful.
    """
    thread = db.scalar(
        select(EmailThread)
        .join(Application, EmailThread.application_id == Application.id)
        .where(EmailThread.id == thread_id, Application.user_id == user.id)
        .options(
            selectinload(EmailThread.emails),
            selectinload(EmailThread.application).selectinload(Application.recruiter),
            selectinload(EmailThread.application).selectinload(Application.campaign),
        )
    )
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return thread


@router.get("/threads/{thread_id}", response_model=InboxThreadDetail)
def thread_detail(
    thread_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InboxThreadDetail:
    """One conversation with its full history, oldest message first.

    Sent and received messages are interleaved by the time they happened, so a
    thread reads top to bottom the way it actually went.
    """
    thread = _owned_thread(db, user, thread_id)
    summary = _summarise_one(db, thread)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )

    # Unlike the list, this endpoint is unpaged — it returns *every* message on
    # the conversation, so its cost is set by the longest thread the account
    # has rather than by a window. Resolving each outbound message's
    # attachments on its own made that a query per message: a 304-message
    # thread cost 161 queries against a four-message thread's 11. One prefetch
    # for the whole conversation flattens it.
    outbound = [
        e for e in thread.emails if e.direction is not EmailDirection.RECEIVED
    ]
    prefetch = email_attachments.prefetch_plans(db, user, outbound)

    return InboxThreadDetail(
        **summary.model_dump(),
        messages=[
            InboxMessage(
                id=e.id,
                direction=e.direction,
                status=e.status,
                from_address=e.from_address,
                to_address=e.to_address,
                subject=e.subject,
                body_text=e.body_text,
                intent=e.intent,
                intent_confidence=e.intent_confidence,
                is_draft=_is_draft(e),
                needs_attention=bool(e.needs_attention),
                attention_reason=e.attention_reason,
                draft_template=e.draft_template,
                draft_note=e.draft_note,
                **_attachments_for(db, e, prefetch=prefetch),
                read_at=e.read_at,
                sent_at=e.sent_at,
                created_at=e.created_at,
            )
            # Chronological, with the id as the tie-break so messages written in
            # one pass (a reply and the draft answering it) keep their order.
            for e in sorted(thread.emails, key=lambda e: (_when(e), e.id))
        ],
    )


def _owned_email(db: Session, user: User, email_id: int) -> Email:
    """One message belonging to *user*, or 404.

    Another user's message is *not found* rather than *forbidden*: a 403 would
    confirm the id exists, and mail ids are guessable.
    """
    email = db.get(Email, email_id)
    thread = db.get(EmailThread, email.thread_id) if email is not None else None
    application = (
        db.get(Application, thread.application_id) if thread is not None else None
    )
    if application is None or application.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Message not found"
        )
    return email


def _resolved_attachment(db: Session, email: Email, index: int):
    """The file at *index*, or the right HTTP error about why there isn't one.

    Shared by the two endpoints that serve one attachment — the file itself and
    the browser-readable rendering of it — so a position that 404s on one cannot
    200 on the other.

    404 for a position that carries nothing. 409 when a document *is* listed
    there and could not be produced — the resume that could not be resolved at
    all, carrying the same sentence the list endpoints show, and the one that
    was named and then failed to render. "No resume on file" is an answer, not
    a server error, and so is "that file would not build"; a 404 on a filename
    the user can see on screen is neither. 502 when Gmail holds a listed file
    and won't hand it over, which is a failure to report rather than a file to
    invent.
    """
    try:
        file = email_attachments.attachment_at(db, email, index)
    except email_attachments.AttachmentUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    if file is not None:
        return file

    # Only a message still to be sent has a *plan*, and only a plan has a
    # reason. Asking one for a received message would answer "no resume on
    # file", which is true and completely irrelevant to a recruiter's email
    # that simply had no attachment; asking one for a sent message would
    # describe a send that already happened without it.
    if email.direction != EmailDirection.RECEIVED:
        if index == 0 and _is_draft(email):
            reason = email_attachments.plan_for_email(db, email).reason
            if reason:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=reason
                )
        # This list is what the screen listed, so a position inside it is a
        # file the user is looking at. It resolved to nothing, which means it
        # could not be built — say that, rather than denying a name they can
        # read. Outside it, there is genuinely nothing there.
        files = email_attachments.files_of_record(db, email)
        if 0 <= index < len(files):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{files[index].filename} couldn't be prepared just now. "
                    "Try again in a moment."
                ),
            )
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="This message has no attachment there.",
    )


@router.get("/emails/{email_id}/attachments/{index}/preview")
def attachment_preview(
    email_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """The same attachment, as something a browser will actually draw.

    A recruiter's Word document was as unreadable in the page as the
    candidate's own: the frame drew nothing, and nothing on screen is
    indistinguishable from a broken preview. This converts it —
    :mod:`app.services.document_preview` renders a .docx to HTML in-process, and
    a legacy .doc to a PDF where the box has LibreOffice.

    Reading only, and that matters as much here as it does on a resume: the
    rendering is never attached to anything and never replaces the file. A
    PDF comes back unchanged so the page has one endpoint it can always read
    from. 409 when no rendering can be produced, carrying the sentence the UI
    shows before falling back to a download.
    """
    email = _owned_email(db, user, email_id)
    file = _resolved_attachment(db, email, index)
    preview = document_preview.preview_for(
        file.filename,
        file.content,
        file.mime_type or email_attachments.media_type_for(file.filename),
    )
    if preview is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=document_preview.UNAVAILABLE_DETAIL,
        )
    return Response(
        content=preview.content,
        media_type=preview.media_type,
        headers={
            "Content-Disposition": email_attachments.inline_content_disposition(
                preview.filename
            ),
            "X-Preview-Of": email_attachments.header_safe(
                preview.source_filename or preview.filename
            ),
            "Content-Security-Policy": document_preview.CONTENT_SECURITY_POLICY,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/emails/{email_id}/attachments/{index}")
def attachment(
    email_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """The bytes behind one attachment, so the user can actually read it.

    Every screen that mentions attachments named them and stopped there, which
    left the filename as the only evidence of what was about to be sent — and a
    reviewer approving a reply cannot check a resume they can't open. This
    serves the file itself, resolved by the same code the sender runs, at the
    same position the name was listed at.

    Received mail resolves differently: the bytes are the sender's, they live
    in Gmail, and they are fetched on demand against the mailbox the message
    was delivered to. Nothing inbound is ever stored here — the row records
    only what the file is called and the id that redeems it.

    These are the bytes as they are — the file, not a rendering of it, which is
    what a download has to be and what a browser needs for a PDF. A format no
    browser draws is read through ``/preview`` instead.

    **The type it is served as is ours to decide, not the sender's.** On inbound
    mail every field behind this response came off a MIME part a stranger wrote,
    including the part's declared ``mimeType`` — and this response used to hand
    that value straight to the browser under ``Content-Disposition: inline``. A
    recruiter attaching ``brief.html`` could therefore choose that the candidate
    was about to be shown an HTML document, which the web client frames from a
    ``blob:`` URL on the app's own origin, next to the session token. See
    :func:`app.services.email_attachments.served_media_type` for the allowlist
    that closes it and the two headers that keep a browser from sniffing its way
    around it.

    404 for a message that isn't the caller's, and for a position that carries
    nothing. 409 and 502 as :func:`_resolved_attachment` describes.
    """
    email = _owned_email(db, user, email_id)
    file = _resolved_attachment(db, email, index)

    media_type, headers = email_attachments.serving_headers(
        file.mime_type, file.filename
    )
    return Response(content=file.content, media_type=media_type, headers=headers)


MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_ATTACHMENTS_PER_EMAIL = 10


def _editable(email: Email) -> bool:
    """Whether the user can still change what this message carries.

    A draft, and only a draft. Once approved the message is queued behind a
    throttled sender and may go out at any moment — changing its attachments
    then is a race whose loser is the recruiter, who receives a document the
    user thought they had removed.
    """
    return email.direction == EmailDirection.SENT and email.status == EmailStatus.DRAFT


def _require_editable(email: Email) -> None:
    if not _editable(email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This message has already been approved — its attachments can't "
                "be changed."
            ),
        )


def _attachments_out(db: Session, user: User, email: Email) -> EmailAttachmentsOut:
    """The queued files plus everything needed to change them.

    The resume options travel with the list rather than behind a second request
    because they are the answer to the complaint this endpoint exists for: a
    user looking at the wrong CV wants to see the right one in the same place,
    not to go and find out what their other resumes are called.
    """
    # The same list the thread view shows and the same list positions are
    # resolved against — a second opinion here would put the controls on the
    # wrong file.
    files = email_attachments.files_of_record(db, email)
    resumes = list(
        db.scalars(
            select(Resume)
            .where(Resume.user_id == user.id)
            .order_by(Resume.is_default.desc(), Resume.id.desc())
        )
    )
    return EmailAttachmentsOut(
        email_id=email.id,
        editable=_editable(email),
        files=[
            AttachmentItem(
                index=index,
                filename=item.filename,
                kind=item.kind,
                attachment_id=item.attachment_id,
            )
            for index, item in enumerate(files)
        ],
        note=(
            email_attachments.plan_for_email(db, email).reason
            if _is_draft(email)
            else None
        ),
        resume_id=email.attachment_resume_id,
        resume_removed=email_attachments.RESUME
        in email_attachments.suppressed_kinds(email),
        resume_options=[
            ResumeOption(
                id=resume.id,
                label=resume.display_label,
                filename=resume.filename,
                is_default=resume.is_default,
                has_original_file=resume.has_original_file,
            )
            for resume in resumes
        ],
    )


@router.get("/emails/{email_id}/attachments", response_model=EmailAttachmentsOut)
def list_attachments(
    email_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmailAttachmentsOut:
    """What this message will carry, and what else it could."""
    email = _owned_email(db, user, email_id)
    return _attachments_out(db, user, email)


@router.patch("/emails/{email_id}/attachments", response_model=EmailAttachmentsOut)
def set_attachment_settings(
    email_id: int,
    payload: AttachmentSettings,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmailAttachmentsOut:
    """Pin the resume this draft should carry, or hand the choice back.

    Pinning also un-removes the resume. Choosing a document is an unambiguous
    statement that one should travel, and making the user press two controls to
    undo one removal would be the kind of state puzzle this screen exists to
    avoid.
    """
    email = _owned_email(db, user, email_id)
    _require_editable(email)

    if payload.resume_id is not None:
        resume = db.get(Resume, payload.resume_id)
        if resume is None or resume.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found"
            )

    email.attachment_resume_id = payload.resume_id
    if payload.resume_id is not None:
        email.suppressed_attachments = [
            kind
            for kind in (email.suppressed_attachments or [])
            if kind != email_attachments.RESUME
        ]
    db.commit()
    db.refresh(email)
    return _attachments_out(db, user, email)


@router.post(
    "/emails/{email_id}/attachments",
    response_model=EmailAttachmentsOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_attachment(
    email_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmailAttachmentsOut:
    """Attach a file of the user's own to a draft.

    Deliberately unfiltered by type. The resolved attachments are resumes and
    letters because that is all the pipeline knows how to produce, but a
    recruiter asking for a portfolio, a transcript or a signed offer is asking
    for whatever the candidate has — and refusing a .zip on the grounds that
    this product is about PDFs would just send the user back to Gmail.
    """
    email = _owned_email(db, user, email_id)
    _require_editable(email)

    existing = len(email_attachments.user_attachments_for_email(db, email))
    if existing >= MAX_ATTACHMENTS_PER_EMAIL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"At most {MAX_ATTACHMENTS_PER_EMAIL} files per message",
        )

    # `strip()` was the whole of this, and it is the wrong half of the job: the
    # value is a client-written multipart header, unbounded in length and free
    # to hold a NUL or a path. Both of those are a 500 on the way to a
    # `String(255)` in Postgres. See `app.core.uploads.safe_upload_filename`.
    filename = safe_upload_filename(file.filename, fallback="attachment")
    # Capped during the read, not after it: see `app.core.uploads`.
    data = await read_capped(file, MAX_ATTACHMENT_BYTES, filename=filename)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{filename}: the file is empty",
        )

    db.add(
        EmailAttachment(
            email_id=email.id,
            filename=filename,
            # The browser's guess, then ours from the extension. Neither is
            # authoritative, and the fallback is a type no client will try to
            # interpret on the recipient's behalf.
            #
            # The browser's guess is also unbounded — this column is
            # `String(120)` and a `Content-Type` on the multipart part is
            # whatever the client wrote — so it is only believed while it is
            # shaped like a MIME type and short enough to store.
            content_type=safe_content_type(
                file.content_type,
                fallback=email_attachments.media_type_for(filename),
            ),
            size=len(data),
            content=data,
        )
    )
    db.commit()
    db.refresh(email)
    return _attachments_out(db, user, email)


@router.delete(
    "/emails/{email_id}/attachments/{index}", response_model=EmailAttachmentsOut
)
def remove_attachment(
    email_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmailAttachmentsOut:
    """Take one file off a draft, by the position it is listed at.

    A resolved file (the resume, the letter) is *suppressed* rather than
    deleted — there is nothing to delete, it is derived — and that suppression
    is what the sender reads. An uploaded file is deleted outright, addressed by
    its row id rather than by the position that found it, so a list rendered
    before another change still removes the file the user pointed at.
    """
    email = _owned_email(db, user, email_id)
    _require_editable(email)

    plan = email_attachments.plan_for_email(db, email)
    if index < 0 or index >= len(plan.files):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This message has no attachment there.",
        )

    target = plan.files[index]
    if target.kind == email_attachments.UPLOAD:
        row = db.get(EmailAttachment, target.attachment_id)
        if row is not None and row.email_id == email.id:
            db.delete(row)
    else:
        email.suppressed_attachments = sorted(
            {*(email.suppressed_attachments or []), target.kind}
        )

    db.commit()
    db.refresh(email)
    return _attachments_out(db, user, email)


@router.post("/threads/{thread_id}/read", response_model=InboxThread)
def mark_read(
    thread_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InboxThread:
    """Mark every inbound message on a thread as read."""
    thread = _owned_thread(db, user, thread_id)
    now = datetime.now(UTC)
    for email in thread.emails:
        if email.direction == EmailDirection.RECEIVED and email.read_at is None:
            email.read_at = now
    db.commit()
    db.refresh(thread)

    summary = _summarise_one(db, thread)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return summary


@router.post(
    "/sync",
    response_model=InboxSyncOut,
    # The one route here whose cost is not its own. It fans out: the queued
    # path — production's — dispatches `poll_thread` once per thread the user
    # has, with no ceiling on the count, and every one of those is a Gmail API
    # call against a per-account quota the beat is already spending. A user with
    # several hundred threads turns one press of a button into several hundred
    # tasks, and a held-down button into as many as they like.
    #
    # The inline path is already bounded by `_INLINE_SYNC_LIMIT`, which is
    # exactly why the fan-out went unnoticed: the branch with a visible ceiling
    # is the one that does not run in production.
    #
    # The beat polls on its own schedule regardless; this button is a nudge, so
    # once every fifty seconds is well past the point of diminishing returns.
    dependencies=[Depends(rate_limit(6, 300, scope="inbox-sync"))],
)
def sync(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> InboxSyncOut:
    """Ask Gmail for new messages on this user's threads right now.

    With a worker running this is a dispatch and returns immediately. Without
    one (local dev, a single-box install with no broker) the most recently
    active threads are polled inline so the button still does something.
    """
    thread_ids = list(
        db.scalars(
            select(EmailThread.id)
            .join(Application, EmailThread.application_id == Application.id)
            .where(
                Application.user_id == user.id,
                EmailThread.gmail_thread_id.is_not(None),
            )
            .order_by(EmailThread.last_message_at.desc().nullslast(), EmailThread.id.desc())
        ).all()
    )
    if not thread_ids:
        return InboxSyncOut()

    from app.tasks.inbox_tasks import poll_thread

    if settings.celery_enabled:
        dispatched = 0
        for tid in thread_ids:
            try:
                poll_thread.delay(tid)
                dispatched += 1
            except Exception as exc:  # noqa: BLE001 - broker down; beat will catch up
                logger.warning("inbox sync dispatch failed for thread %s: %s", tid, exc)
        return InboxSyncOut(
            threads_polled=dispatched,
            dispatched=True,
            errors=len(thread_ids) - dispatched,
        )

    batch = thread_ids[:_INLINE_SYNC_LIMIT]
    polled = errors = 0
    for tid in batch:
        try:
            poll_thread.run(tid)
            polled += 1
        except Exception as exc:  # noqa: BLE001 - one bad thread must not 500 the page
            errors += 1
            logger.warning(
                    "inbox sync failed for thread %s: %s", tid, exc, exc_info=True
                )
    return InboxSyncOut(
        threads_polled=polled,
        dispatched=False,
        errors=errors,
        threads_skipped=len(thread_ids) - len(batch),
    )
