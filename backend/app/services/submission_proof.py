"""Proof of work — the receipt for an application the browser agent submitted.

An automated application is a claim the candidate cannot check. The run says
"submitted", the employer says nothing for three weeks, and the only honest
question — *did it actually go through?* — has, until this module, been
answerable only by an operator reading a JSON column. That is the gap this
closes: every submitted run turns into a **receipt** a person can read, keep,
and show.

A receipt is deliberately more than a screenshot:

* **What was claimed** — company, title, the URL that was driven, which ATS,
  which resume file was attached, when, and how long it took.
* **What the employer said back** — ``FormApplication.confirmation``, verbatim.
  A page that acknowledged the application in its own words is the strongest
  evidence there is, and it is the employer's sentence, not ours.
* **What was seen** — every screenshot in order, each with the page it was
  taken of and the SHA-256 :mod:`app.services.browser_runner` stamped on the
  bytes at capture time.

Three rules keep it honest, and they are the whole design:

1. **It never asserts more than the run recorded.** A submitted run whose page
   said nothing back is ``confirmed: false`` with its screenshots — not a
   receipt with a comforting sentence we wrote.
2. **A missing file is reported, never hidden.** Screenshots are on disk and
   disks get pruned; each piece of evidence carries ``available``, so a receipt
   with three of its five pictures gone says so.
3. **Verification is real, and re-reads the bytes.** :func:`verify` re-hashes
   what is on disk against what was recorded at capture. ``verified: false`` on
   an available file means the picture changed after the run — which is a
   different and much more interesting statement than "missing".

Cheap enough for a request: metadata comes off the row, and only
:func:`receipt` (one run, on demand) touches the filesystem.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.form_apply import FormApplication, FormApplyStatus
from app.models.user import User
from app.services import ats_platform, browser_runner

logger = logging.getLogger(__name__)

# Only a submitted run has anything to prove. A filled one is a draft the
# candidate still has to send, and calling that "proof of application" would be
# the most damaging thing this feature could get wrong.
PROVABLE_STATUSES = (FormApplyStatus.SUBMITTED,)


def _evidence(
    application: FormApplication, step: dict[str, Any], *, check_files: bool
) -> dict[str, Any]:
    """One step, as a piece of evidence."""
    name = step.get("screenshot")
    item: dict[str, Any] = {
        "step": step.get("step"),
        "label": step.get("label") or "step",
        "at": step.get("at"),
        "note": step.get("note"),
        "page_url": step.get("url"),
        "screenshot": name,
        "sha256": step.get("sha256"),
        "bytes": step.get("bytes"),
        # Where the browser will fetch the image from. Relative to the API root
        # so it works under whatever prefix the deployment is served at.
        "href": (
            f"/form-apply/{application.id}/screenshots/{name}" if name else None
        ),
        "available": None,
        "verified": None,
    }
    if name is None or not check_files:
        return item

    path = browser_runner.artifact_dir() / name
    stamped = browser_runner.digest(path) if path.exists() else None
    item["available"] = stamped is not None
    if stamped is not None and step.get("sha256"):
        # Only a *recorded* hash can be verified. A capture from before hashing
        # existed is available-but-unverifiable, which is not the same as
        # failing verification — so it stays None rather than False.
        item["verified"] = stamped[0] == step["sha256"]
    return item


def receipt(
    application: FormApplication, *, check_files: bool = True
) -> dict[str, Any]:
    """The full receipt for one run.

    Built for any run, not just a submitted one — the evidence of a run that
    stopped at a sign-in wall is exactly what the candidate needs to see — but
    ``proves_submission`` is true only when the run actually submitted.
    """
    steps = [s for s in (application.steps or []) if isinstance(s, dict)]
    evidence = [_evidence(application, step, check_files=check_files) for step in steps]
    captured = [item for item in evidence if item["screenshot"]]

    submitted = application.status in PROVABLE_STATUSES
    return {
        "application_id": application.id,
        "job_posting_id": application.job_posting_id,
        "company": application.company,
        "job_title": application.job_title,
        "url": application.url,
        "platform": application.platform.value,
        "platform_label": ats_platform.label(application.platform),
        "status": application.status.value,
        "proves_submission": submitted,
        # The employer's own words, and whether there were any.
        "confirmation": application.confirmation,
        "confirmed": bool(application.confirmation),
        "submitted_at": application.finished_at if submitted else None,
        "started_at": application.started_at,
        "duration_ms": application.duration_ms,
        "resume_uploaded": application.resume_uploaded,
        "answers": list(application.answers or []),
        "filled_fields": list(application.filled_fields or []),
        "note": application.note,
        "evidence": evidence,
        "screenshot_count": len(captured),
        # A receipt is *complete* when every picture it claims is still there.
        # Unknown (files unchecked) is not complete: this is a claim about
        # disk, and it is only worth making when disk was actually read.
        "complete": bool(captured) and all(i["available"] for i in captured),
    }


def summary(application: FormApplication) -> dict[str, Any]:
    """The list view of a receipt: no filesystem, no evidence bodies."""
    full = receipt(application, check_files=False)
    for key in ("evidence", "answers", "filled_fields", "complete"):
        full.pop(key, None)
    return full


def verify(application: FormApplication) -> dict[str, Any]:
    """Re-hash every screenshot on disk against what the run recorded.

    Returns the counts a UI can state plainly — "5 of 5 intact" — plus the
    names of anything missing or altered, which is what an operator chasing it
    actually needs.
    """
    result = receipt(application, check_files=True)
    checked = [i for i in result["evidence"] if i["screenshot"]]
    missing = [i["screenshot"] for i in checked if i["available"] is False]
    altered = [i["screenshot"] for i in checked if i["verified"] is False]
    return {
        "application_id": application.id,
        "screenshots": len(checked),
        "available": sum(1 for i in checked if i["available"]),
        "verified": sum(1 for i in checked if i["verified"]),
        "missing": missing,
        "altered": altered,
        "intact": not missing and not altered,
    }


def receipts(
    db: Session,
    user: User,
    *,
    limit: int = 50,
    job_posting_id: int | None = None,
    submitted_only: bool = True,
) -> list[dict[str, Any]]:
    """Receipt summaries for this user's runs, newest first.

    Defaults to submitted runs — the tracker asks "what have I actually sent",
    and a fill that was never submitted is not an answer to that.
    """
    stmt = select(FormApplication).where(FormApplication.user_id == user.id)
    if submitted_only:
        stmt = stmt.where(FormApplication.status.in_(PROVABLE_STATUSES))
    if job_posting_id is not None:
        stmt = stmt.where(FormApplication.job_posting_id == job_posting_id)
    stmt = stmt.order_by(FormApplication.id.desc()).limit(limit)
    return [summary(row) for row in db.scalars(stmt)]


def as_text(data: dict[str, Any]) -> str:
    """A receipt as plain text, for pasting into an email or a spreadsheet.

    Deliberately unadorned: a candidate forwarding this to a recruiter is
    making a factual claim, and every line here is something the run recorded.
    """
    when = data.get("submitted_at")
    if isinstance(when, datetime):
        when = when.isoformat(timespec="seconds")
    lines = [
        f"Application to {data.get('company') or 'employer'}"
        + (f" — {data['job_title']}" if data.get("job_title") else ""),
        f"Submitted via {data.get('platform_label')}"
        + (f" at {when}" if when else ""),
    ]
    if data.get("url"):
        lines.append(f"Form: {data['url']}")
    if data.get("confirmation"):
        lines.append(f'The employer\'s page said: "{data["confirmation"]}"')
    else:
        lines.append(
            "The employer's page showed no confirmation text — see the screenshots."
        )
    lines.append(f"Screenshots on file: {data.get('screenshot_count', 0)}")
    return "\n".join(lines)


def prune(  # pragma: no cover - operator tool
    older_than: datetime, *, base_dir: Path | None = None
) -> int:
    """Delete screenshots last modified before *older_than*. Returns the count.

    Housekeeping for a disk that fills up. Receipts survive it — they keep the
    hash, the page URL and the employer's words — and the pieces of evidence
    whose files went report ``available: false`` rather than quietly reading as
    if they had never been taken.
    """
    directory = base_dir or browser_runner.artifact_dir()
    cutoff = older_than.timestamp()
    removed = 0
    for path in directory.glob("*.png"):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError as exc:  # noqa: BLE001 - one stuck file is not a failure
            logger.warning("could not prune %s: %s", path, exc)
            continue
        removed += 1
    return removed


__all__ = [
    "PROVABLE_STATUSES",
    "as_text",
    "prune",
    "receipt",
    "receipts",
    "summary",
    "verify",
]
