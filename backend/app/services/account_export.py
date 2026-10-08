"""Handing an account back its own data.

Erasure shipped first (:mod:`app.services.account_deletion`) and left the other
half of the same obligation open: a user could close their account and take
nothing with them. Everything they had told this product — the resumes, the
parsed career history, the intents, every message they had exchanged with a
recruiter and every verdict we had formed about a posting — was readable only a
screen at a time through the UI, and only for as long as the account existed.
That is a portability gap with a name, and it is also just an unfinished
product: data you can put in and cannot get out.

The two modules are deliberately built on the same inventory.
``account_deletion.USER_SCOPED_TABLES`` is the list that makes "the cascade
covers everything" checkable rather than asserted, and this module reuses it so
"the export covers everything" is checkable in exactly the same way: every table
on that list is either in :data:`EXPORTED_TABLES` or in :data:`EXCLUDED_TABLES`
with a reason, and ``tests/test_account_export.py`` fails if a new one is in
neither. An export that quietly omits a table is worse than no export, because
the user has no way to notice.

Four decisions worth stating.

**Columns are taken generically, minus a redaction list.** The export selects
whatever columns a table has, so a column added next month is exported without
anyone remembering to add it. What must *not* go out is named instead, in
:data:`REDACTED_COLUMNS`, which is the smaller and far more reviewable list: the
password hash, both encrypted Google tokens, the encrypted LinkedIn password and
session, and the digest unsubscribe token. An export is a file the user
downloads and may well mail to themselves; a credential inside it is a
credential in their inbox.

``email_events.ip_hash`` is redacted for a different reason. It is not the
user's data — it is a pseudonymised identifier for the *recruiter* who opened
their email — and handing one person a pseudonym for another is not portability.

**The correspondence is included, and it is not user-scoped.** ``email_threads``,
``emails``, ``email_attachments`` and ``follow_ups`` carry no ``user_id`` at all;
they hang off ``applications``, which is why ``USER_SCOPED_TABLES`` does not list
them and why the cascade still reaches them. They are most of what a person means
when they ask for their data, so they are scoped here through an explicit chain
rather than left out for want of a column.

**Binaries are described, not embedded.** ``resumes.file_bytes`` and
``email_attachments.content`` are the original files. Base64 in JSON is a third
larger than the bytes, and a few resumes plus a recruiter's attachments would
turn a readable document into something a browser struggles to hold in memory.
Each is exported as its filename, type, size and the API path that returns it,
and the document says so in as many words — a reader who finds `"content"`
missing must not have to guess whether the file was empty or withheld.

**It streams.** The document is generated table by table, a page of rows at a
time, so a large account is bounded in memory on the server rather than
assembled whole and then sent.
"""
from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import Select, Table, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.user import User
from app.services.account_deletion import ANONYMISED_TABLES, USER_SCOPED_TABLES

logger = logging.getLogger(__name__)

#: Rows read per round trip. Small enough that one page is a modest amount of
#: memory even for ``emails``, whose bodies are the largest text here.
PAGE = 200

#: Format version. Bumped when the *shape* changes, so a tool reading an old
#: file can tell. Written into the document rather than only documented here.
EXPORT_VERSION = 1

#: Columns that must never leave the server, and why. The values are written
#: into the export itself under ``notes.redacted`` — a user reading their own
#: data is entitled to know what was held back and on what grounds.
REDACTED_COLUMNS: dict[str, str] = {
    "users.hashed_password": (
        "Your password, hashed. Exporting it would put a crackable credential "
        "in a file you might email to yourself."
    ),
    "users.token_version": (
        "An internal counter used to invalidate sessions. It says nothing about "
        "you."
    ),
    "gmail_accounts.refresh_token_encrypted": (
        "Your Google grant. Anyone holding it could read the mailbox."
    ),
    "gmail_accounts.access_token_encrypted": (
        "The short-lived half of the same grant."
    ),
    "linkedin_accounts.password_encrypted": "Your LinkedIn password.",
    "linkedin_accounts.session_state_encrypted": (
        "A stored LinkedIn browser session, which is a credential in all but "
        "name."
    ),
    "digest_preferences.unsubscribe_token": (
        "The secret in your digest's one-click unsubscribe link."
    ),
    "email_events.ip_hash": (
        "A pseudonymised identifier for the person who opened your email, not "
        "for you. Handing one person a pseudonym for another is not "
        "portability."
    ),
}

#: Binary columns, and the API path that returns the real file. The export
#: carries the description; this is where the bytes stay.
BINARY_COLUMNS: dict[str, str] = {
    "resumes.file_bytes": "/api/v1/resumes/{id}/file",
    "email_attachments.content": (
        "/api/v1/inbox/emails/{email_id}/attachments/{index}"
    ),
}

#: User-scoped tables deliberately left out, and why. Checked by the drift test
#: against ``USER_SCOPED_TABLES``, so "we decided not to" and "we forgot" cannot
#: look the same.
EXCLUDED_TABLES: dict[str, str] = {
    "dead_letter_jobs": (
        "Operational records of background work that failed. They hold no "
        "content you wrote or gave us — only which job broke and the exception "
        "it raised — and erasure deletes them with the account."
    ),
    "feature_events": (
        "Anonymous usage counters. They are detached from the account rather "
        "than deleted on erasure, which is the same reason they are not yours "
        "to take: they no longer identify anybody."
    ),
}


def _direct(name: str):
    """Scope a table by its own ``user_id``."""

    def scope(table: Table, user_id: int, chain: dict[str, Any]) -> Select:
        return select(table).where(table.c.user_id == user_id).order_by(table.c.id)

    return scope


def _through_applications(column: str):
    """Scope a table that has no ``user_id``, through the applications it hangs off.

    The chain is built once per export and handed in, rather than rebuilt as a
    correlated subquery per table: three of these tables reference the same set
    of application ids, and a subquery scoped only through a sibling is the shape
    that gets sequentially scanned.
    """

    def scope(table: Table, user_id: int, chain: dict[str, Any]) -> Select:
        return (
            select(table)
            .where(table.c[column].in_(select(chain["applications"].c.id)))
            .order_by(table.c.id)
        )

    return scope


def _emails_scope(table: Table, user_id: int, chain: dict[str, Any]) -> Select:
    return (
        select(table)
        .where(table.c.thread_id.in_(select(chain["threads"].c.id)))
        .order_by(table.c.id)
    )


def _attachments_scope(table: Table, user_id: int, chain: dict[str, Any]) -> Select:
    return (
        select(table)
        .where(table.c.email_id.in_(select(chain["emails"].c.id)))
        .order_by(table.c.id)
    )


#: What the export contains, in the order it is written: who you are, what you
#: told us, what we found, and then the correspondence. Ordered for a person
#: reading the file top to bottom, not for the query planner.
EXPORTED_TABLES: dict[str, Any] = {
    "resumes": _direct("resumes"),
    "profiles": _direct("profiles"),
    "job_searches": _direct("job_searches"),
    "job_postings": _direct("job_postings"),
    "fit_scores": _direct("fit_scores"),
    "tailored_resumes": _direct("tailored_resumes"),
    "cover_letters": _direct("cover_letters"),
    "campaigns": _direct("campaigns"),
    "subject_variants": _direct("subject_variants"),
    "recruiters": _direct("recruiters"),
    "applications": _direct("applications"),
    "application_status_events": _direct("application_status_events"),
    "email_threads": _through_applications("application_id"),
    "emails": _emails_scope,
    "email_attachments": _attachments_scope,
    "follow_ups": _through_applications("application_id"),
    "email_events": _direct("email_events"),
    "email_bounces": _direct("email_bounces"),
    "recruiter_emails": _direct("recruiter_emails"),
    "recruiter_scan_runs": _direct("recruiter_scan_runs"),
    "recruiter_scan_skips": _direct("recruiter_scan_skips"),
    "reply_feedback": _direct("reply_feedback"),
    "classifier_priors": _direct("classifier_priors"),
    "form_applications": _direct("form_applications"),
    "form_apply_profiles": _direct("form_apply_profiles"),
    "gmail_accounts": _direct("gmail_accounts"),
    "linkedin_accounts": _direct("linkedin_accounts"),
    "notifications": _direct("notifications"),
    "notification_preferences": _direct("notification_preferences"),
    "digest_preferences": _direct("digest_preferences"),
    "recruiter_reply_preferences": _direct("recruiter_reply_preferences"),
    "autopilot_preferences": _direct("autopilot_preferences"),
}

#: Tables in the export that carry no ``user_id`` of their own — the
#: correspondence. Named so the drift test can tell "reached through a chain"
#: apart from "not user data".
CHAINED_TABLES: frozenset[str] = frozenset(
    {"email_threads", "emails", "email_attachments", "follow_ups"}
)


def _json_default(value: Any) -> Any:
    """Everything the database returns that ``json`` will not take on its own."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes | bytearray | memoryview):
        # Never reached for the columns in BINARY_COLUMNS, which are replaced
        # before serialization. Here for any binary column added later: base64
        # is wrong-but-lossless, which beats an exception mid-stream.
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, set | frozenset):
        return sorted(value)
    return str(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False)


def _row_dict(table_name: str, table: Table, row) -> dict[str, Any]:
    """One row as a mapping, with the redactions and binary swaps applied."""
    out: dict[str, Any] = {}
    for column in table.c:
        key = f"{table_name}.{column.name}"
        if key in REDACTED_COLUMNS:
            continue
        if key in BINARY_COLUMNS:
            continue
        out[column.name] = row._mapping[column.name]
    return out


def _binary_note(table_name: str, table: Table, row) -> dict[str, Any] | None:
    """What replaces a file's bytes: enough to identify it, and where to get it."""
    described: dict[str, Any] = {}
    for column in table.c:
        key = f"{table_name}.{column.name}"
        if key not in BINARY_COLUMNS:
            continue
        raw = row._mapping[column.name]
        described[column.name] = {
            "bytes": len(raw) if raw is not None else 0,
            "present": raw is not None,
            "download": BINARY_COLUMNS[key],
        }
    return described or None


def _stream_table(
    db: Session, table_name: str, stmt: Select, table: Table
) -> Iterator[str]:
    """One table as a JSON array, a page of rows at a time."""
    first = True
    yield "["
    result = db.execute(stmt).yield_per(PAGE)
    for row in result:
        record = _row_dict(table_name, table, row)
        files = _binary_note(table_name, table, row)
        if files is not None:
            record["files"] = files
        yield ("" if first else ",") + _dumps(record)
        first = False
    yield "]"


def _chain(db: Session, user_id: int) -> dict[str, Any]:
    """The id sets the correspondence tables are scoped through.

    CTEs rather than inline subqueries because two of them are referenced twice
    over the course of one export, and a repeated subquery is a repeated scan.
    """
    applications = Base.metadata.tables["applications"]
    threads = Base.metadata.tables["email_threads"]
    emails = Base.metadata.tables["emails"]

    app_cte = (
        select(applications.c.id)
        .where(applications.c.user_id == user_id)
        .cte("export_applications")
    )
    thread_cte = (
        select(threads.c.id)
        .where(threads.c.application_id.in_(select(app_cte.c.id)))
        .cte("export_threads")
    )
    email_cte = (
        select(emails.c.id)
        .where(emails.c.thread_id.in_(select(thread_cte.c.id)))
        .cte("export_emails")
    )
    return {"applications": app_cte, "threads": thread_cte, "emails": email_cte}


def account_summary(user: User) -> dict[str, Any]:
    """The account itself, minus what :data:`REDACTED_COLUMNS` holds back."""
    users = Base.metadata.tables["users"]
    return {
        column.name: getattr(user, column.name)
        for column in users.c
        if f"users.{column.name}" not in REDACTED_COLUMNS
    }


def export_filename(user: User, now: datetime | None = None) -> str:
    """``doaide-autoapply-export-2026-08-27.json`` — dated, so two are distinguishable.

    Deliberately carries no address or name. The file lands in a downloads
    folder that other people may see over a shoulder or in a shared screen, and
    the account it belongs to is inside it either way.
    """
    stamp = (now or datetime.now(UTC)).date().isoformat()
    return f"doaide-autoapply-export-{stamp}.json"


def stream_export(db: Session, user: User) -> Iterator[str]:
    """The whole document, as JSON text, in pieces.

    A generator rather than a string: an account with a few thousand emails is
    tens of megabytes of body text, and building that whole in memory to hand it
    to a response is the one shape that turns a rarely-used feature into an
    outage.

    ``counts`` is written at the end because it is not known at the start —
    reporting it honestly is worth more than putting it where a reader looks
    first, and a machine reading this parses the whole file anyway.
    """
    chain = _chain(db, user.id)
    counts: dict[str, int] = {}

    yield "{"
    yield f'"export_version":{EXPORT_VERSION},'
    yield f'"generated_at":{_dumps(datetime.now(UTC))},'
    yield f'"account":{_dumps(account_summary(user))},'
    yield '"notes":' + _dumps(
        {
            "binaries": (
                "Resume files and email attachments are described here, not "
                "embedded — each entry under a row's \"files\" key gives the "
                "size and the API path that returns the original bytes. "
                "Base64 in JSON is a third larger than the file it carries, "
                "and a document holding every attachment would be one no "
                "browser could open."
            ),
            "redacted": REDACTED_COLUMNS,
            "excluded": EXCLUDED_TABLES,
        }
    ) + ","

    yield '"data":{'
    first_table = True
    for name, scope in EXPORTED_TABLES.items():
        table = Base.metadata.tables[name]
        stmt = scope(table, user.id, chain)
        yield ("" if first_table else ",") + _dumps(name) + ":"
        first_table = False
        rows = 0
        for chunk in _stream_table(db, name, stmt, table):
            # Counting here rather than with a second COUNT query: the rows are
            # already going past, and a count from a separate statement can
            # disagree with the array beside it.
            if chunk not in ("[", "]"):
                rows += 1
            yield chunk
        counts[name] = rows
    yield "},"

    yield '"counts":' + _dumps(counts)
    yield "}"


def unclassified_tables() -> set[str]:
    """User-scoped tables that are neither exported nor excluded — the drift set.

    Empty is the only correct answer. Returned rather than asserted so the test
    that reads it names the table instead of only reporting that something is
    wrong.
    """
    known = set(EXPORTED_TABLES) | set(EXCLUDED_TABLES)
    return (set(USER_SCOPED_TABLES) | set(ANONYMISED_TABLES)) - known


__all__ = [
    "BINARY_COLUMNS",
    "CHAINED_TABLES",
    "EXCLUDED_TABLES",
    "EXPORTED_TABLES",
    "EXPORT_VERSION",
    "REDACTED_COLUMNS",
    "account_summary",
    "export_filename",
    "stream_export",
    "unclassified_tables",
]
