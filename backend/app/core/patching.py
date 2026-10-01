"""Refusing a partial update that would write a null where none is allowed.

Every PATCH/PUT in this app has the same shape: an all-optional schema, a
``model_dump(exclude_unset=True)``, and a loop that ``setattr``s whatever
survived onto the ORM row. ``exclude_unset`` is what makes "leave this field
alone" expressible — a key the client omitted never reaches the loop.

An *explicit* ``null`` is a different thing, and it reached the loop. For a
column that accepts nulls that is correct and useful: ``{"salary_min": null}``
is how the UI clears a floor. For a column that does not, it was a 500, because
nothing between the request and the database asked the question:

* ``{"remote_only": null}`` on a ``NOT NULL`` boolean raised ``IntegrityError``
  out of ``db.commit()``, which the error middleware turns into a bare 500 —
  telling the client "the server broke" for what is a malformed request.
* JSON columns are worse, and worse in a way that outlives the request.
  SQLAlchemy's ``JSON`` type stores Python ``None`` as the JSON value ``null``
  rather than as SQL ``NULL``, so ``{"target_roles": null}`` **satisfies** the
  ``NOT NULL`` constraint, commits, and leaves a row whose ``target_roles``
  reads back as ``None``. The response model declares ``list[str]``, so the
  write 500s on the way out — and so does every subsequent read. One request
  permanently bricked the user's profile list, with no way to repair it through
  the API, because the PATCH that could fix the row 500s rendering its own
  response.

So the check belongs in front of the write, and it belongs somewhere shared:
the rule is the same for every one of these endpoints, and the field lists are
long enough that restating them per router is how one gets forgotten.

Nullability is read off the mapper rather than declared here, which means a new
``NOT NULL`` column is covered the moment it is added and a column that becomes
nullable stops being guarded without anyone remembering to come back.
"""
from __future__ import annotations

from functools import cache
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase


@cache
def required_columns(model: type[DeclarativeBase]) -> frozenset[str]:
    """The attribute names on *model* that may never hold ``None``.

    Keyed on the class and cached: the mapper is fixed at import time, and this
    runs on every write request.

    Composite and relationship attributes are skipped — only plain columns have
    a nullability to read, and only plain columns are what a patch loop assigns.
    """
    return frozenset(
        attr.key
        for attr in sa_inspect(model).mapper.column_attrs
        if not any(column.nullable for column in attr.columns)
    )


def reject_nulls(model: type[DeclarativeBase], fields: dict[str, Any]) -> None:
    """422 rather than 500 when *fields* would null out a required column.

    *fields* is the ``model_dump(exclude_unset=True)`` of a patch payload, so a
    key being present means the client sent it. Keys that are not columns of
    *model* — a schema-only switch like ``is_default`` handled by the router
    itself — are ignored here; the router still owns what they mean.

    Raises rather than filtering the offending keys out. Silently dropping them
    would answer "saved" to a request that changed nothing the caller asked for,
    which is the failure mode that makes a UI look haunted.
    """
    offenders = sorted(
        name
        for name, value in fields.items()
        if value is None and name in required_columns(model)
    )
    if not offenders:
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            f"{', '.join(offenders)} cannot be set to null — omit the field to "
            "leave it unchanged."
        ),
    )


__all__ = ["reject_nulls", "required_columns"]
