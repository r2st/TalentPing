"""Turning what a person typed into what SQL should match.

Two operators here read the same string very differently. To someone typing
into a search box, ``100%``, ``ml_infra`` and ``C:\\temp`` are just text. To
``LIKE`` they are a wildcard, a single-character wildcard, and an escape
sequence — so a search for ``ml_infra`` quietly matches ``mlXinfra``, and a
search for ``%`` matches every row in the table.

That is a correctness bug everywhere it appears, and it was a security bug in
one place: the public opt-out endpoint matched the address it was handed with
``ilike``, so ``/unsubscribe?email=%`` opted out every contact in the database,
for every user, and cancelled every scheduled follow-up behind them. No login
and no signature required — the endpoint honours unsigned links by design.

So there are two functions, and the choice between them is not stylistic:

:func:`escape_like`
    for a genuine substring search, where the caller wraps the result in ``%``
    and wants everything *inside* treated as text.
:func:`ci_equals`
    for matching one known value — an email address, a token — where ``LIKE``
    was only ever standing in for "ignore case" and a pattern match is the
    wrong tool entirely.

Callers of :func:`escape_like` must pass ``escape="\\\\"`` to ``ilike``/``like``.
SQLAlchemy does not infer it, and without it the backslashes this function adds
are matched literally on some backends — which fails closed (too few rows)
rather than open, but is still wrong.

:func:`search_clause` is the third one, and it is the one the search endpoints
should call: it builds the entire ``OR`` over the columns a box searches, which
is also the only way to stop ``escape="\\\\"`` from being twenty separate
chances to forget it. Escaping is only half of what a typed string needs before
it is a pattern; the other half is the handful of inputs that are not searches
at all:

* **whitespace** — a user clearing the box sends ``?q=`` or ``?q=%20``. Every
  endpoint here meant that to mean "no filter", and three of the four wrote it
  differently; the fourth declared ``min_length=1`` and answered 422, so the
  same empty box was an error on one screen and a full list on the others.
* **a NUL byte** — ``?q=a%00b`` reaches the handler as a real ``"a\x00b"``.
  SQLite shrugs and matches nothing. psycopg refuses the *parameter* outright
  (``PostgreSQL text fields cannot contain NUL (0x00) bytes``), so on production
  the query never runs and the request is a 500 with no useful body. Postgres
  cannot store the byte either, so no row could ever have contained it: the
  honest answer is to say the needle is unsearchable, not to strip the byte and
  quietly run a *different*, broader search than the one that was asked for.
* **whitespace in the middle** — ``LIKE`` compares a run of whitespace against a
  run of whitespace, so a company name pasted out of a rendered page (carrying
  ``\u00a0``), a subject pasted out of a mail client (carrying the newline it
  wrapped at), or a name typed with two spaces all failed to find a row that is
  sitting in the table. See :func:`search_patterns`.

Length is deliberately **not** handled here. Truncating a needle broadens it —
200 characters of a 1000-character paste match strictly more rows than the
whole — and a search returning extra rows it was never asked for is worse than
one that refuses. The cap belongs on the ``Query`` declaration, where it is a
422 before any of this runs and is visible in the schema; :data:`SEARCH_MAX_LENGTH`
is the number to use.
"""
from __future__ import annotations

import re

from fastapi import HTTPException, status
from sqlalchemy import ColumnElement, func, or_

# Backslash first: escaping it after the others would double the escapes this
# function had just added.
_LIKE_META = ("\\", "%", "_")


def escape_like(needle: str) -> str:
    """*needle* with ``LIKE`` metacharacters neutralised.

    The result is meant to be interpolated into a pattern by the caller, which
    is the only party that knows whether it wants a prefix, a suffix or a
    substring match::

        stmt.where(Job.company.ilike(f"%{escape_like(q)}%", escape="\\\\"))
    """
    for char in _LIKE_META:
        needle = needle.replace(char, f"\\{char}")
    return needle


def ci_equals(column: ColumnElement, value: str) -> ColumnElement:
    """A case-insensitive equality test — the thing ``ilike`` is often misused for.

    ``column.ilike(value)`` looks like "equals, ignoring case" and behaves like
    it right up until *value* contains a ``%`` or a ``_``. Email addresses
    legitimately contain underscores, so this is not a hypothetical: matching
    ``a_b@x.com`` with ``ilike`` also matches ``axb@x.com`` — a different human.
    """
    return func.lower(column) == (value or "").strip().lower()


#: The longest search needle the API accepts, as a ``Query(max_length=...)``.
#:
#: Chosen against the columns rather than against taste: the widest thing any
#: search matches on is ``Recruiter.email`` at ``String(320)``, and names and
#: companies are narrower still. A needle longer than the column it is compared
#: to cannot match, so past this point every extra character only buys a longer
#: scan. The free-text columns (``Email.body_text``) have no width, but nobody
#: finds a message by pasting a paragraph at it.
SEARCH_MAX_LENGTH = 200


#: A run of whitespace, as a person's keyboard and clipboard produce them.
#:
#: ``\s`` is Unicode-aware here on purpose: the runs that reach a search box are
#: rarely plain spaces. A company name copied out of a web page arrives carrying
#: ``\u00a0``, a subject copied out of a mail client arrives with the newline it
#: was wrapped at, and a name typed in a hurry arrives with two spaces.
_WHITESPACE_RUN = re.compile(r"\s+")


def search_patterns(raw: str | None) -> tuple[str, ...]:
    """Every ``LIKE`` pattern *raw* should be matched by — empty when it is not a search.

    Usually one. Two when the typed string's *internal* whitespace is not a
    single space, because then there are two honest readings of it and no way to
    express both in one pattern.

    ``LIKE`` matches a literal run of whitespace against a literal run of
    whitespace, which makes "Acme  Corp" and "Acme Corp" different searches, and
    ``Acme\nCorp`` a third. Nobody typing into a search box means that. The runs
    are not typed at all in the usual case — they are what a clipboard put there:
    a non-breaking space out of a rendered page, the newline a mail client
    wrapped a subject at, a stray second space. Every one of them turned a search
    for a name that is *in* the database into an empty result, which reads as
    "we do not have this contact" rather than as "your paste has a newline in
    it".

    So the collapsed reading comes first, and the string as typed comes second
    when it differs. Both, rather than only the collapsed one, because a stored
    value can carry the odd run too — ``Recruiter.company`` and
    ``JobPosting.location`` are scraped — and collapsing the needle alone would
    have *un*-found the row that a doubled space genuinely matches. Widening
    only: every needle that matched before still matches.

    What this cannot reach is the mirror case — a needle with single spaces
    against a *stored* run, "senior engineer" against a body that wrapped
    between the two words. That one is the haystack's spelling rather than the
    needle's, so it needs the database to normalise the column, the same shape
    of answer that accent-insensitivity needs and for the same reason. See
    ``TestKnownGaps`` in ``tests/test_search_edge_cases.py``.
    """
    if raw is None:
        return ()
    needle = raw.strip()
    if not needle:
        return ()
    if "\x00" in needle:
        # Not merely unmatchable — unsendable. psycopg rejects the parameter, so
        # without this the request is a 500 from inside the driver rather than
        # an answer about rows.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Search text cannot contain a NUL byte",
        )
    collapsed = _WHITESPACE_RUN.sub(" ", needle)
    if collapsed == needle:
        return (f"%{escape_like(needle)}%",)
    return (f"%{escape_like(collapsed)}%", f"%{escape_like(needle)}%")


def search_clause(raw: str | None, *columns: ColumnElement) -> ColumnElement | None:
    """The whole ``WHERE`` fragment for a search box over *columns*, or ``None``.

    The one call the search endpoints should make, so that "the user typed
    nothing" and "the user typed something unsearchable" have one answer each
    instead of one per router::

        if (clause := search_clause(q, Recruiter.name, Recruiter.email)) is not None:
            stmt = stmt.where(clause)

    ``None`` means *apply no filter* — not *match nothing*. The two are opposite
    result sets and the distinction is the whole reason this returns an optional
    rather than a clause that happens to be ``ilike '%%'``: ``%%`` reads as
    "match everything" but is false for a NULL column, so filtering on it
    silently drops every row whose searchable fields are all empty — which for
    ``/recruiters`` is exactly the row discovery writes first.

    It also owns ``escape="\\"``, which is the argument SQLAlchemy will not
    infer and which every call site used to repeat: twenty copies of a keyword
    that fails closed when forgotten, and fails closed *quietly*.
    """
    patterns = search_patterns(raw)
    if not patterns:
        return None
    if not columns:  # pragma: no cover - a caller bug, not an input
        raise ValueError("search_clause needs at least one column")
    tests = [
        column.ilike(pattern, escape="\\")
        for pattern in patterns
        for column in columns
    ]
    return tests[0] if len(tests) == 1 else or_(*tests)


__all__ = [
    "SEARCH_MAX_LENGTH",
    "ci_equals",
    "escape_like",
    "search_clause",
    "search_patterns",
]
