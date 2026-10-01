"""A page of rows, and the headers that say what was left off it.

Most list endpoints here returned ``select(...).where(user_id == me)`` with no
ceiling. That is fine right up until it isn't: recruiter rows are written by a
discovery run rather than by a person, so the number of them is a function of
how long the account has been running, and the first user to cross a few
thousand turns one GET into a few thousand serialized objects — held in the
API process's memory, in the JSON encoder, and in the client's.

So every list is bounded now. The interesting part is what happens at the
boundary, because a silent truncation is worse than no limit at all: the client
sees a short list, has no way to tell it is short, and quietly acts on a subset.
Every paged response therefore carries

``X-Total-Count``
    how many rows match, ignoring the page
``X-Has-More``
    ``true`` when there are rows past this page

which is enough for a client to page, and enough for one that does not page to
at least *notice*. The body stays a bare JSON array, so nothing that already
consumes these endpoints has to change.

The count is only queried when it might differ from what was returned. A first
page that came back short is the whole result set — that is not an inference,
it is what "short" means — so the common case of a list that fits costs exactly
one query, the same one it cost before this module existed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Query, Response
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

# High enough that no human-scale list (resumes, campaigns, saved searches) is
# ever truncated by it, low enough to bound the machine-scale ones. A caller who
# wants a real page says so.
DEFAULT_LIMIT = 200
MAX_LIMIT = 500

#: The largest integer any column or clause here can hold: a signed 64-bit int.
#:
#: This is a *database* limit rather than a product one, which is why it is not
#: a taste-driven number. Postgres `bigint` and SQLite `INTEGER` both stop here,
#: and both stop loudly: `OFFSET 99999999999999999999` is `bigint out of range`
#: on one and `OverflowError: Python int too large to convert to SQLite INTEGER`
#: on the other. Python's `int` has no such ceiling, so the value sailed through
#: FastAPI, through SQLAlchemy, and died in the driver — a 500 with nothing in
#: the body, on nine endpoints, from a query string anyone can type.
#:
#: Every `int` query parameter that reaches SQL wants this as its `le=`: a row
#: id to filter on, an `offset` to skip by. It restricts nothing real — no row
#: has an id past it and no list has that many rows — it only moves the refusal
#: from the driver to the schema, where it is a 422 that says which parameter
#: was wrong.
MAX_DB_INT = 2**63 - 1


@dataclass(frozen=True)
class Page:
    """The pagination window a request asked for."""

    limit: int
    offset: int


def page_params(
    limit: int = Query(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Rows per page. `X-Total-Count` reports how many exist.",
    ),
    offset: int = Query(
        default=0, ge=0, le=MAX_DB_INT, description="Rows to skip"
    ),
) -> Page:
    """FastAPI dependency: the standard ``limit``/``offset`` pair."""
    return Page(limit=limit, offset=offset)


def set_page_headers(
    response: Response, *, total: int, returned: int, offset: int
) -> None:
    """Write ``X-Total-Count`` and ``X-Has-More`` for a page this module did not run.

    :func:`paginate` covers the endpoints whose page is one ``SELECT ... LIMIT``.
    Three are not: ``/dashboard`` pages a list it assembled in Python,
    ``/recruiter-inbox`` counts through a subquery of its own, and
    ``/notifications`` returns its totals in the body. All three took ``limit``
    and ``offset``, and all three answered without a single header — so the one
    thing this module exists to prevent was true of them exactly as if they had
    no pagination at all: the client sees a short list and has no way to tell it
    is short.

    ``total`` is the count *after* filters, which is the only number that
    answers "am I looking at all of what I asked for". An endpoint that also
    reports an unfiltered total — the inbox's chips, the notification bell —
    reports it in its body under its own name, where it cannot be mistaken for
    this one.
    """
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Has-More"] = "true" if offset + returned < total else "false"


def paginate(
    db: Session,
    stmt: Select,
    page: Page,
    response: Response,
) -> list[Any]:
    """Run *stmt* for one page, and set the count headers on *response*.

    *stmt* must already carry its ``order_by``. An unordered paged query is a
    bug that only shows up under load — two pages of an unordered result can
    repeat a row and drop another, and which rows depends on the plan the
    database happened to pick.
    """
    rows = list(db.scalars(stmt.limit(page.limit).offset(page.offset)))

    if page.offset == 0 and len(rows) < page.limit:
        # A short first page *is* the result set. No second query.
        total = len(rows)
    else:
        total = (
            db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
            or 0
        )

    set_page_headers(response, total=total, returned=len(rows), offset=page.offset)
    return rows


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_DB_INT",
    "MAX_LIMIT",
    "Page",
    "page_params",
    "paginate",
    "set_page_headers",
]
