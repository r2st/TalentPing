#!/usr/bin/env python3
"""Put the reply drafts that already exist through the new routing policy.

Inbox replies were drafted and never sent until ``thread_reply_policy`` shipped,
so every user's inbox holds a backlog of them. New mail routes itself from now
on; this is the one-off that catches up the backlog, and it is a script rather
than a migration because it *sends email*, which is not a thing to do inside
``alembic upgrade``.

**Most of the backlog is not this pipeline's to route, and the dry run says so.**
Production held 162 pending reply drafts, and 144 of them came from the
recruiter-inbox scanner, which had already routed them under its own confidence
bands, switches and daily cap. Those are reported as ``other_pipeline`` and left
untouched — deliberately not flagged either, because they are not waiting on the
user. Only drafts written by the thread poller (the ones whose inbound message
carries a classified ``intent``) are re-evaluated here.

    # what would happen, changing nothing (the default)
    python scripts/route_pending_replies.py

    # do it
    python scripts/route_pending_replies.py --apply

    # one user, and only a few, for the first run
    python scripts/route_pending_replies.py --user-id 1 --limit 5 --apply

Run it on the box, as the service user, with the venv that runs the app::

    ssh root@$HOST 'cd /opt/TalentPing/backend && sudo -u talentping \
        env $(grep -v "^#" ../.env | xargs) .venv/bin/python \
        scripts/route_pending_replies.py'

``--apply`` queues the qualifying drafts and hands them to the throttled sender,
which spreads them out and puts every one through the reputation gate — the same
path a reply that routed itself takes. Nothing goes out in a burst, and nothing
bypasses a paused or warming mailbox.

Idempotent: a draft this script has already flagged is skipped by the next run,
so it is safe to run again after a partial one.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tasks.inbox_tasks import reevaluate_pending_replies  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually queue and send. Without it, nothing is written.",
    )
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=14,
        help=(
            "hold rather than send a reply to a message older than this. The "
            "backlog is old by definition and a prompt-looking answer to a "
            "three-week-old question is not prompt."
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    result = reevaluate_pending_replies.run(
        user_id=args.user_id,
        limit=args.limit,
        dry_run=not args.apply,
        max_age_days=args.max_age_days,
    )

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    plan = result.pop("plan", [])
    verb = "would send" if result["dry_run"] else "queued"
    print(f"considered {result['considered']} draft(s)")
    print(f"  {verb}: {result['queued']}")
    if not result["dry_run"]:
        print(f"  handed to the sender: {result['dispatched']}")
    print(f"  held for review: {result['held']}")
    if result["skipped"]:
        print(f"  left alone (another pipeline's, or nothing to reply to): "
              f"{result['skipped']}")
    print()
    for row in plan:
        confidence = "  ? " if row["confidence"] is None else f"{row['confidence']:.0%}"
        age = "   -" if row.get("age_days") is None else f"{row['age_days']:>3}d"
        print(
            f"  #{row['email_id']:>6}  thread {row['thread_id']:>5}  "
            f"{str(row['intent'] or '-'):<15} {confidence:>5} {age}  {row['outcome']}"
        )
    if result["dry_run"]:
        print("\nNothing was written. Re-run with --apply to act on this.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
