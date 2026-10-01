"""Retention for the personal data this product writes to the filesystem.

Two features put a candidate's own details on disk, outside the database and
outside every retention window that existed:

``form_apply_service.resume_file``
    Writes the extracted resume text to ``<artifacts>/resumes/resume-<id>.txt``
    so an ATS upload control has a file to take. That file is a plaintext
    career history — name, address, employers, dates.

``browser_runner.StepLog``
    Screenshots every step of a form run. A screenshot of a part-filled
    application shows the same details as the form does, with the phone number
    and postal address already typed in.

Neither had anything that removed it. Not the row being deleted (a cascade
knows about rows, not files), not a sweep, not a clock — so the directory grew
one resume and a dozen pictures per application, indefinitely, on a box whose
backups nobody is thinking about as a store of personal data.

Account deletion removes a departing user's files by name
(:mod:`app.services.account_deletion`). This is the other half: the files of
users who are still here, which have no reason to outlive the run that
produced them by more than the window in which somebody might want to look at
the evidence.

Age, not ownership, is what this deletes on. That is deliberate — the mapping
from a file back to a user costs a query per file and would make the sweep
proportional to the wrong thing, and every file here is equally personal, so
there is no class of them worth keeping longer.

The endpoint that serves a screenshot already answers **410 Gone** for a file
that is no longer there, which is what makes this safe to run: a pruned
screenshot degrades to an honest "that has been cleaned up" rather than to a
broken image.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from app.core.config import settings
from app.services.browser_runner import artifact_dir
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

#: Only these are pruned. An allowlist rather than "everything under the
#: directory", because the artifact directory is configurable and a
#: misconfiguration that pointed it at something else must not turn this into a
#: recursive delete of that something else.
PRUNABLE_SUFFIXES = (".png", ".txt")


def _older_than(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except OSError:
        return False


def prune_artifacts(*, now: float | None = None) -> dict:
    """Delete artifact files past the retention window. Returns what it did.

    Written as a plain function with the task wrapping it so a test — and an
    operator at a shell — can run it without a broker.
    """
    days = max(1, int(settings.form_apply_artifact_retention_days))
    cutoff = (now if now is not None else time.time()) - days * 86400
    root = artifact_dir()

    deleted = 0
    failed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in PRUNABLE_SUFFIXES:
            continue
        if not _older_than(path, cutoff):
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError:
            # A file we cannot remove is worth counting and not worth raising
            # over: the next run will try again, and a retention sweep that
            # dies on one bad permission stops protecting everything else.
            failed += 1

    if deleted or failed:
        logger.info(
            "pruned %s form-apply artifact(s), %s could not be removed",
            deleted,
            failed,
        )
    return {"deleted": deleted, "failed": failed, "retention_days": days}


@celery_app.task(name="app.tasks.artifact_tasks.prune_form_apply_artifacts")
def prune_form_apply_artifacts() -> dict:
    return prune_artifacts()


__all__ = ["PRUNABLE_SUFFIXES", "prune_artifacts", "prune_form_apply_artifacts"]
