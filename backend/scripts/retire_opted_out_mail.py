#!/usr/bin/env python3
"""Write off unsent mail addressed to contacts who have already opted out.

From now on, opting out writes off the mail still waiting behind it — see
``outreach_service.cancel_unsent_for_recruiter``, called from both opt-out
routes. This is the one-off for the backlog that accumulated before that, and it
is a script rather than a migration because it is a judgement about *this*
deployment's data, not a schema change every deployment needs.

Nothing here changes what reaches a recipient. The send path refuses an
opted-out contact at due time, so every row this touches was already going to be
written off — one at a time, as each countdown came due, over however many hours
the batch was spread across. What it changes is what the user sees in the
meantime: a queue that reads on the tracker as mail still coming, and drafts
offering a send button for someone who asked us to stop.

    # what would happen, changing nothing (the default)
    python scripts/retire_opted_out_mail.py

    # do it
    python scripts/retire_opted_out_mail.py --apply

Run it on the box, as the service user, with the venv that runs the app::

    ssh root@$HOST 'cd /opt/TalentPing/backend && sudo -u talentping env \
        "$(grep -m1 "^DATABASE_URL=" ../.env)" PYTHONPATH=/opt/TalentPing/backend \
        .venv/bin/python scripts/retire_opted_out_mail.py'

Idempotent: a row it has already retired is no longer QUEUED or DRAFT, so a
second run finds nothing and reports nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models.application import Application  # noqa: E402
from app.models.email import Email, EmailDirection, EmailStatus  # noqa: E402
from app.models.email_thread import EmailThread  # noqa: E402
from app.models.recruiter import Recruiter  # noqa: E402


def plan(db, user_id: int | None = None) -> list[dict]:
    """Every unsent outbound row addressed to an opted-out contact."""
    stmt = (
        select(Email, Recruiter.email, Recruiter.id)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .join(Recruiter, Application.recruiter_id == Recruiter.id)
        .where(
            Recruiter.opted_out.is_(True),
            Email.direction == EmailDirection.SENT,
            Email.status.in_([EmailStatus.QUEUED, EmailStatus.DRAFT]),
        )
        .order_by(Email.id)
    )
    if user_id is not None:
        stmt = stmt.where(Application.user_id == user_id)
    return [
        {
            "email_id": email.id,
            "status": email.status.value,
            "recruiter_id": recruiter_id,
            "to": address,
            "subject": (email.subject or "")[:60],
        }
        for email, address, recruiter_id in db.execute(stmt).all()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write the rows off. Without it, nothing is written.",
    )
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = plan(db, user_id=args.user_id)
        if args.apply and rows:
            for row in rows:
                email = db.get(Email, row["email_id"])
                if email is not None:
                    email.status = EmailStatus.FAILED
            db.commit()

        result = {
            "dry_run": not args.apply,
            "retired": len(rows),
            "queued": sum(1 for r in rows if r["status"] == "QUEUED"),
            "drafts": sum(1 for r in rows if r["status"] == "DRAFT"),
            "contacts": len({r["recruiter_id"] for r in rows}),
            "plan": rows,
        }
    finally:
        db.close()

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    verb = "would retire" if result["dry_run"] else "retired"
    print(
        f"{verb} {result['retired']} unsent message(s) to "
        f"{result['contacts']} opted-out contact(s)"
    )
    print(f"  queued (would have been sent): {result['queued']}")
    print(f"  drafts (awaiting approval):    {result['drafts']}")
    print()
    for row in result["plan"]:
        print(
            f"  #{row['email_id']:>6}  {row['status']:<6}  "
            f"{row['to']:<34}  {row['subject']}"
        )
    if result["dry_run"]:
        print("\nNothing was written. Re-run with --apply to act on this.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
