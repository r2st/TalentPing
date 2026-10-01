"""Erasing an account, and everything the account is.

Until this existed there was no way for a user to leave. Every individual
record had a delete — a resume, a contact, a mailbox, a campaign — and the row
that ties them together did not, so "delete my account" was a support request
that nobody could actually carry out without a psql prompt. That is a
compliance gap with a name (erasure), but it is also just an unfinished
product: an account you can create and cannot close.

The work divides into three parts, and only the first is a ``DELETE``.

**What cascades.** Every user-scoped table in this schema declares
``ForeignKey("users.id", ondelete="CASCADE")``, and the chains that hang off
them — ``applications`` -> ``email_threads`` -> ``emails`` ->
``email_attachments`` — cascade the same way. So deleting the ``users`` row
does erase the resumes, the parsed career history, the cover letters, the
tailored PDFs, the correspondence and the attachments, at the database, in one
statement. :func:`orphan_counts` is how that claim is *checked* rather than
asserted, and ``tests/test_account_deletion.py`` runs it after a real delete.

**What does not, and must be swept.** Two tables are outside the cascade on
purpose and need naming:

``dead_letter_jobs``
    Holds ``user_id`` as a plain integer with no foreign key — deliberately,
    because the row exists to record a failure and a constraint that could
    reject it defeats the point (see :mod:`app.models.dead_letter`). The cost
    is that nothing removes it here, so this module does, explicitly.

``feature_events``
    ``ondelete="SET NULL"``. Kept, anonymised. A usage count for last quarter
    is not personal data once it is not attached to a person, and erasure does
    not require destroying aggregate history — it requires that the history
    stop identifying anybody. The row survives with ``user_id`` NULL.

**What is on disk, outside the database entirely.** Two features write
personal data to the filesystem and neither has ever removed it:
``form_apply_service`` writes the extracted resume text to
``<artifacts>/resumes/resume-<id>.txt`` so an ATS upload control has a file to
take, and ``browser_runner`` screenshots every step of a form run — pictures of
a part-filled application carrying the candidate's name, address and phone
number. Both are named by a row id, both outlive the row, and neither is
covered by a cascade, because a cascade only knows about rows. So they are
removed here, by name, from the rows that name them, before those rows go.

**What lives at Google.** A refresh token is not in this database in any form
we can un-issue; it is a grant held by Google, and deleting our copy leaves it
outstanding. So each connected mailbox is revoked upstream first, on a
best-effort basis — a revocation that fails must not leave the user unable to
delete their account, which would be the worse of the two failures.

The whole thing runs in one transaction and re-reads the password first. An
account deletion triggered by a stolen token that never re-authenticated is
the one mistake here that cannot be walked back.
"""
from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.dead_letter import DeadLetterJob
from app.models.feature_event import FeatureEvent
from app.models.form_apply import FormApplication
from app.models.gmail_account import GmailAccount
from app.models.resume import Resume
from app.models.user import User
from app.services import crypto, google_oauth
from app.services.browser_runner import artifact_dir

logger = logging.getLogger(__name__)


def revoke_upstream_grants(db: Session, user: User) -> int:
    """Tell Google to drop every grant this account holds. Best effort.

    Returns how many were revoked without error. Failures are logged and
    swallowed: Google being unreachable is not a reason to refuse someone their
    own deletion, and the local token is about to stop existing either way.
    """
    revoked = 0
    accounts = db.scalars(
        select(GmailAccount).where(GmailAccount.user_id == user.id)
    ).all()
    for account in accounts:
        if not account.refresh_token_encrypted:
            continue
        try:
            with suppress(crypto.TokenCryptoError):
                google_oauth.revoke_token(
                    crypto.decrypt(account.refresh_token_encrypted)
                )
                revoked += 1
        except Exception:  # noqa: BLE001 - see the docstring
            logger.warning(
                "grant revocation failed during account deletion",
                exc_info=True,
                extra={"gmail_account_id": account.id},
            )
    return revoked


def sweep_uncascaded(db: Session, user_id: int) -> dict[str, int]:
    """Remove the rows a ``DELETE FROM users`` would leave behind.

    Runs *before* the user row goes, so ``feature_events`` can be anonymised
    by hand rather than by the ``SET NULL`` the constraint would apply — same
    result, but it happens here where it is visible and counted, instead of as
    a side effect nobody reading this function would know about.
    """
    dead_letters = db.execute(
        delete(DeadLetterJob).where(DeadLetterJob.user_id == user_id)
    ).rowcount
    anonymised = db.execute(
        FeatureEvent.__table__.update()
        .where(FeatureEvent.user_id == user_id)
        .values(user_id=None)
    ).rowcount
    return {
        "dead_letter_jobs_deleted": int(dead_letters or 0),
        "feature_events_anonymised": int(anonymised or 0),
    }


def _unlink_within(root: Path, candidate: Path) -> bool:
    """Delete *candidate*, but only if it really is inside *root*.

    The filenames come from our own ``steps`` JSON rather than from a request,
    so this is belt-and-braces — but it is a deletion driven by stored strings,
    and the cost of being sure is one ``resolve``.
    """
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root.resolve()):
            return False
        resolved.unlink()
        return True
    except (OSError, ValueError):
        return False


def purge_disk_artifacts(db: Session, user_id: int) -> dict[str, int]:
    """Remove the personal data this user has sitting on the filesystem.

    Runs before the rows go, because the rows are what name the files.
    Failures are counted, not raised: a screenshot that cannot be unlinked
    must not leave an account half-deleted, and the database is the copy that
    matters. What is left behind is reported by the return value so the caller
    can log it.
    """
    root = artifact_dir()
    removed_resumes = 0
    removed_screenshots = 0

    resume_ids = db.scalars(
        select(Resume.id).where(Resume.user_id == user_id)
    ).all()
    for resume_id in resume_ids:
        if _unlink_within(root, root / "resumes" / f"resume-{resume_id}.txt"):
            removed_resumes += 1

    applications = db.scalars(
        select(FormApplication).where(FormApplication.user_id == user_id)
    ).all()
    for application in applications:
        for step in application.steps or []:
            name = step.get("screenshot") if isinstance(step, dict) else None
            if name and _unlink_within(root, root / name):
                removed_screenshots += 1

    return {
        "resume_files_deleted": removed_resumes,
        "screenshots_deleted": removed_screenshots,
    }


def delete_account(db: Session, user: User) -> dict[str, int]:
    """Erase *user* and everything belonging to them. Commits.

    The order is load-bearing. Revocation first, because it needs the tokens
    that are about to be deleted. The sweep second, because it needs the
    ``user_id`` and one of its two halves must not be left to the constraint.
    The row itself last, which is what fires every cascade.
    """
    user_id = user.id
    revoked = revoke_upstream_grants(db, user)
    files = purge_disk_artifacts(db, user_id)
    swept = sweep_uncascaded(db, user_id)
    db.delete(user)
    db.commit()

    # No address, no name: this line outlives the account it records the end
    # of, which is the whole reason the account was deleted.
    logger.warning(
        "account deleted",
        extra={
            "deleted_user_id": user_id,
            "grants_revoked": revoked,
            **files,
            **swept,
        },
    )
    return {"grants_revoked": revoked, **files, **swept}


#: Every table holding rows keyed to a user, and the column that keys them.
#: Used by :func:`orphan_counts` — and by the test that runs it — so "the
#: cascade covers everything" is a claim with a list behind it rather than a
#: belief. A table added with a ``user_id`` and not added here is caught by
#: ``tests/test_account_deletion.py::test_every_user_scoped_table_is_listed``.
USER_SCOPED_TABLES: tuple[str, ...] = (
    "gmail_accounts",
    "linkedin_accounts",
    "resumes",
    "profiles",
    "campaigns",
    "applications",
    "application_status_events",
    "recruiters",
    "recruiter_emails",
    "recruiter_reply_preferences",
    "recruiter_scan_runs",
    "recruiter_scan_skips",
    "reply_feedback",
    "classifier_priors",
    "cover_letters",
    "tailored_resumes",
    "fit_scores",
    "job_postings",
    "job_searches",
    "form_applications",
    "form_apply_profiles",
    "subject_variants",
    "email_bounces",
    "email_events",
    "notifications",
    "notification_preferences",
    "digest_preferences",
    "autopilot_preferences",
    "dead_letter_jobs",
)


#: User-keyed tables that erasure *detaches* rather than deletes. Listed
#: separately from :data:`USER_SCOPED_TABLES` because they are the exception to
#: the rule that test enforces, and an exception with no name is
#: indistinguishable from an oversight.
ANONYMISED_TABLES: tuple[str, ...] = ("feature_events",)


def orphan_counts(db: Session, user_id: int) -> dict[str, int]:
    """Rows still keyed to *user_id* in each table, after a deletion.

    Every value should be zero. Returned as a mapping rather than a bool so a
    failing test names the table that kept the data instead of only reporting
    that something did.
    """
    from app.core.database import Base

    counts: dict[str, int] = {}
    for name in USER_SCOPED_TABLES:
        table = Base.metadata.tables.get(name)
        if table is None or "user_id" not in table.c:
            continue
        counts[name] = int(
            db.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c.user_id == user_id)
            )
            or 0
        )
    return counts


__all__ = [
    "ANONYMISED_TABLES",
    "USER_SCOPED_TABLES",
    "delete_account",
    "purge_disk_artifacts",
    "orphan_counts",
    "revoke_upstream_grants",
    "sweep_uncascaded",
]
