"""One spelling of an email address, so two spellings can't be two people.

An address is case-insensitive in every way that matters to a user: nobody
believes ``Jane.Doe@acme.com`` and ``jane.doe@acme.com`` reach different
mailboxes, and no mail provider in practice treats them as different. Databases
disagree. ``=`` on a Postgres ``varchar`` is case-sensitive, and so is the
unique index behind ``users.email``.

Left alone, that gap does two things to an account, and the quieter one is
worse:

* **Sign-in fails for the account you have.** Registration stored whatever the
  signup form was typed with — Pydantic's ``EmailStr`` lower-cases the domain
  and leaves the local part exactly as entered — and the login lookup is an
  exact match. A candidate who registered from their phone's autocapitalised
  keyboard as ``Jane@acme.com`` and later typed ``jane@acme.com`` got
  "Incorrect email or password" against a password that was correct, with no
  way to tell that from a wrong password and nothing in the logs to say
  otherwise.
* **Two accounts for one person.** ``uq_users_email`` compares bytes, so the
  second registration is not a duplicate as far as the database is concerned.
  Both are real accounts with their own resumes, mailboxes and outreach, and a
  connected Gmail address can only be attached to one of them.

So addresses are normalised at the edge — the schema lower-cases before
anything is stored — and every lookup that could meet a row written before this
existed folds case as well. :func:`normalize_address` is that single spelling.
"""
from __future__ import annotations

__all__ = ["normalize_address"]


def normalize_address(value: str | None) -> str:
    """The canonical form of an email address: trimmed and lower-cased.

    ``lower()`` rather than ``casefold()`` deliberately. Casefolding is for
    comparing human text and maps characters aggressively — Turkish dotless
    ``ı``, the German ``ß`` to ``ss`` — which would map two genuinely distinct
    addresses onto one. ASCII case is the only folding an SMTP address is
    understood to be insensitive to.
    """
    return (value or "").strip().lower()
