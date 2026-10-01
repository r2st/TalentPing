#!/usr/bin/env python3
"""Read the recruiter mail that arrived while nothing was reading it.

Every ordinary scan is bounded by ``RECRUITER_SCAN_WINDOW_DAYS`` — a week. That
makes any detection outage longer than a week permanent rather than slow: the
next healthy scan looks back seven days, the missing mail is older than that, and
nothing ever looks further. In August 2026 a dead Gmail OAuth grant and a retired
model id together cost this deployment ten days of recruiter mail that way.

``app.tasks.backlog_tasks`` is the standing fix — it runs six-hourly and needs no
operator. This script is the same thing on demand, for the two cases beat is bad
at: right after mailboxes are reconnected, when nobody wants to wait for the next
tick, and with an explicit window when the outage was longer than the default
thirty days.

    # what it would read, and how far back — no Gmail call, no writes
    python scripts/catch_up_backlog.py

    # detect the backlog and start working through it
    python scripts/catch_up_backlog.py --apply

    # a longer outage, one user, a smaller first batch
    python scripts/catch_up_backlog.py --apply --user-id 1 --days 60 --limit 5

Run it on the box, as the service user, with the venv that runs the app::

    ssh root@$HOST 'cd /opt/TalentPing/backend && sudo -u talentping \
        env $(grep -v "^#" ../.env | xargs) .venv/bin/python \
        scripts/catch_up_backlog.py --apply'

**What ``--apply`` actually does.** It scans each connected mailbox over the wide
window and stores a row per message never seen before, then hands the oldest
``RECRUITER_BACKLOG_BATCH_SIZE`` of them to the ordinary classifier, spaced out.
The rest stay ``DETECTED`` and are picked up by the next run, so a large backlog
drains over days instead of arriving as one burst of model calls.

**It cannot answer a recruiter twice.** The scanner skips a message it already
has a row for, the unique constraint refuses a second row for one message, and
``recruiter_reply_service.process`` claims its row in the database before doing
any work, so a message that is already classified comes back ``skipped``. Running
this twice, or alongside beat, is safe.

**And it will not blast out replies.** Anything older than
``recruiter_reply_service.RETRY_AUTO_MAX_AGE_DAYS`` is drafted for review rather
than sent — and a backlog is old by definition, so nearly all of it drafts. What
does qualify still goes through the reputation gate, the warm-up ramp and the
daily auto-reply cap, exactly as a live reply does.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models.recruiter_email import RecruiterReplyPreference  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import gmail_accounts  # noqa: E402
from app.tasks import backlog_tasks  # noqa: E402


def _watched(user_id: int | None) -> list[tuple[int, list[int]]]:
    """``(user_id, [account_id, ...])`` for every user who asked to be watched."""
    from sqlalchemy import select

    db = SessionLocal()
    try:
        stmt = select(RecruiterReplyPreference).where(
            RecruiterReplyPreference.enabled.is_(True)
        )
        if user_id is not None:
            stmt = stmt.where(RecruiterReplyPreference.user_id == user_id)
        out = []
        for pref in db.scalars(stmt).all():
            user = db.get(User, pref.user_id)
            out.append(
                (pref.user_id, [a.id for a in gmail_accounts.live_accounts(user)])
            )
        return out
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually scan and process. Without it, nothing is read or written.",
    )
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help=(
            "how far back to read. Defaults to RECRUITER_BACKLOG_WINDOW_DAYS "
            f"({settings.recruiter_backlog_window_days}). Raise it when the "
            "outage was longer than that."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "messages handed to the classifier this run. Defaults to "
            f"RECRUITER_BACKLOG_BATCH_SIZE ({settings.recruiter_backlog_batch_size}). "
            "The remainder stays DETECTED for the next run."
        ),
    )
    args = parser.parse_args()

    if not settings.recruiter_reply_enabled:
        print("RECRUITER_REPLY_ENABLED is off; nothing to do.", file=sys.stderr)
        return 2

    watched = _watched(args.user_id)
    if not watched:
        print("no watched users match; nothing to do.", file=sys.stderr)
        return 1

    report: dict = {"apply": args.apply, "scans": [], "drain": None}
    unconnected = [uid for uid, accounts in watched if not accounts]
    if unconnected:
        # The expected state until the candidate reconnects Gmail. Said plainly
        # rather than reported as an empty run: "it found nothing" and "it had
        # nothing to look in" are different answers and only one needs action.
        report["users_with_no_connected_mailbox"] = unconnected

    for user_id, account_ids in watched:
        for account_id in account_ids:
            report["scans"].append(
                backlog_tasks.catch_up_mailbox.run(
                    user_id,
                    account_id,
                    window_days=args.days,
                    process_limit=args.limit,
                    # The button was pressed; the six-hourly debounce that keeps
                    # beat off its own heels does not apply to a person asking.
                    force=True,
                    dry_run=not args.apply,
                )
            )

    report["drain"] = backlog_tasks.drain_detected.run(
        args.user_id, limit=args.limit, dry_run=not args.apply
    )

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
