"""The base class for an enum a *client* is allowed to name.

Most enums here are internal: a column's storage, a state machine's alphabet.
A handful are something else as well — they are the vocabulary of a query
string, typed into a URL bar, written into a client by hand, or round-tripped
through a bookmark. ``?status=``, ``?intent=``, ``?view=``, ``?direction=``,
``?kind=``, ``?platform=``.

Those had two conventions and no rule. The model enums spell their values in
upper case (``JobStatus.NEW == "NEW"``) because that is how they were written
when they were only ever a column; the view enums added later for the filter
tabs spell theirs in lower case (``DirectionFilter.ALL == "all"``) because that
is how they read in a URL. Both were matched exactly, so the same instinct was
right on one screen and a 422 on the next:

    GET /api/v1/jobs?status=new          -> 422  ("Input should be 'NEW' ...")
    GET /api/v1/inbox?direction=all      -> 200
    GET /api/v1/jobs?status=NEW          -> 200
    GET /api/v1/inbox?direction=ALL      -> 422

Nothing about the product distinguishes those four. The case a filter value is
written in is a spelling decision made years apart by different files, and
asking a person to remember which one applies to which screen is asking them to
remember an implementation detail. It is also the one place the API contradicts
its own search boxes, which have matched case-insensitively from the start.

So: **the canonical spelling is still the only one in the schema**, and it is
what every response and every ``openapi.json`` says. This only widens what is
*accepted*, and only for a value that names a real member — ``?status=bogus``
is the same 422 it always was, listing the same canonical values.

Deliberately not applied to every enum in the codebase. ``_missing_`` runs on
``Cls(value)``, which is also the call a *parser* makes when it is deciding
whether a string it read is a member at all — an LLM's answer, a webhook's
payload, a column read back from a database that has drifted. Loosening that
comparison turns a caught drift into a silent coercion. The enums below are the
ones a URL names, and the loosening is what a URL is owed.
"""
from __future__ import annotations

import enum


class FilterEnum(str, enum.Enum):
    """A ``str`` enum whose members can be named in any case from a query string."""

    @classmethod
    def _missing_(cls, value: object) -> FilterEnum | None:
        """The member *value* names ignoring case, or ``None`` to raise as usual.

        Matched against ``member.value`` rather than ``member.name`` because the
        value is what the schema publishes and what a response body carries.
        For every enum here the two are the same string in a different case, so
        this reaches both spellings either way — but a future member whose name
        and value diverge should follow the one the client was shown.
        """
        if not isinstance(value, str):
            return None
        folded = value.casefold()
        for member in cls:
            if member.value.casefold() == folded:
                return member
        return None


__all__ = ["FilterEnum"]
