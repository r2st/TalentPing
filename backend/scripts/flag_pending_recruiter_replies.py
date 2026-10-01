#!/usr/bin/env python3
"""Give the recruiter inbox's already-held replies the reason they were held.

The companion to ``route_pending_replies.py``, and between them they cover the
backlog. That one walks the *thread poller's* drafts and re-runs
``thread_reply_policy`` over them. Run against production it reported::

    considered 161 draft(s)
      would send: 0
      held for review: 0
      left alone (another pipeline's, or nothing to reply to): 161

Every one of them belongs to the recruiter-inbox scanner, which routed them
under its own bands and switches, and which that task deliberately will not
overrule. This is the other half: the drafts are left exactly where that
pipeline put them, and the decision it already recorded — the route, the band,
the reason — is copied onto the draft so the "Needs review" filter and the badge
can finally see it.

**Nothing is sent, and there is no flag that would send anything.** Every row in
this set routed ``DRAFT`` or ``FLAG``; a row that routed ``AUTO`` had its reply
queued when the mail arrived and is not here. See the task docstring.

    # what would happen, changing nothing (the default)
    python scripts/flag_pending_recruiter_replies.py

    # do it
    python scripts/flag_pending_recruiter_replies.py --apply

Run it on the box, as the service user, with the venv that runs the app. The
settings load ``../.env`` themselves, so no env wrapper is needed::

    ssh root@$HOST 'cd /opt/TalentPing/backend && sudo -u talentping \
        .venv/bin/python scripts/flag_pending_recruiter_replies.py --apply'

Idempotent: a flagged draft no longer matches, so a partial run repeats safely.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tasks.recruiter_reply_tasks import (  # noqa: E402
    flag_pending_recruiter_replies,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write the flags. Without it, nothing is written.",
    )
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    result = flag_pending_recruiter_replies.run(
        user_id=args.user_id,
        limit=args.limit,
        dry_run=not args.apply,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    plan = result.pop("plan", [])
    verb = "would flag" if result["dry_run"] else "flagged"
    print(f"{verb} {result['flagged']} held draft(s) for review")
    print(f"  sent: {result['sent']}  (this task has no send path — see --help)")
    if result["by_route"]:
        print("  by the route the recruiter pipeline recorded:")
        for route, count in sorted(result["by_route"].items()):
            print(f"    {route}: {count}")
    print()
    for row in plan[:40]:
        confidence = "   ?" if row["confidence"] is None else f"{row['confidence']:>5.1f}"
        print(
            f"  #{row['email_id']:>6}  {str(row['route'] or '-'):<6} {confidence}  "
            f"{row['reason'][:88]}"
        )
    if len(plan) > 40:
        print(f"  ... and {len(plan) - 40} more")
    if result["dry_run"]:
        print("\nNothing was written. Re-run with --apply to act on this.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
