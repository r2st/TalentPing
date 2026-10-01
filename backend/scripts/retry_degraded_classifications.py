#!/usr/bin/env python3
"""Re-read the recruiter mail whose verdict was a no-model guess.

The pipeline classifies each inbound message once, on arrival. When every
provider is rate limited — which on the free tiers is most of the time — that
one reading is the keyword fallback, whose confidence tops out at 0.8 and is
usually 0.4. Routing multiplies confidence by the match score, so a real
recruiter and a good fit still land below the auto bar, and nothing ever asked
again. A rate limit lasting seconds left a draft lasting forever.

``recruiter_reply_tasks.retry_degraded_classifications`` now runs on beat and
keeps up with new mail. This is the same task, on demand, for working through a
backlog that accumulated before it shipped or faster than it drains.

    # what it would touch, without a single model call (the default)
    python scripts/retry_degraded_classifications.py

    # do it, one small batch
    python scripts/retry_degraded_classifications.py --apply --limit 10

    # keep going until the backlog is clear or the chain goes down
    python scripts/retry_degraded_classifications.py --apply --limit 25 --rounds 20

Run it on the box, as the service user, with the venv that runs the app::

    ssh root@$HOST 'cd /opt/TalentPing/backend && sudo -u talentping \
        .venv/bin/python scripts/retry_degraded_classifications.py'

**What --apply can do.** A message that re-reads as a good match from a real
recruiter, and whose reply a model was able to write, and which arrived inside
``RETRY_AUTO_MAX_AGE_DAYS``, is queued and handed to the throttled sender — the
same path a live auto-reply takes, through the same reputation gate and warm-up
ramp. Everything else is re-drafted and left in the inbox with the reason it is
waiting. Nothing bypasses a paused or warming mailbox, and nothing is deleted.

**Expect the draft count to move in both directions.** Messages the fallback
never answered at all become drafts, and messages it wrongly called recruiter
outreach turn out to be newsletters and stand down. The number to watch is
whether recruiters are getting answered, not whether the badge went down.

Idempotent: a row read successfully is no longer degraded and no later run
selects it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tasks.recruiter_reply_tasks import (  # noqa: E402
    retry_degraded_classifications,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually re-read and re-route. Without it, no model is called.",
    )
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="rows per round (default: RECRUITER_RETRY_BATCH_SIZE)",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help=(
            "run this many batches back to back, stopping early when a round "
            "finds nothing or the model chain is down. Each round is a fresh "
            "selection, so this drains a backlog without one long transaction."
        ),
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="ignore messages older than this (default: RECRUITER_RETRY_MAX_AGE_DAYS)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    rounds: list[dict] = []
    for index in range(max(1, args.rounds)):
        result = retry_degraded_classifications.run(
            user_id=args.user_id,
            limit=args.limit,
            dry_run=not args.apply,
            max_age_days=args.max_age_days,
        )
        rounds.append(result)
        if not args.json:
            _print_round(index, result, dry_run=not args.apply)
        if not _should_continue(result, args.apply):
            break

    if args.json:
        print(json.dumps(rounds, indent=2, default=str))
    elif not args.apply:
        print("\nNo model was called and nothing was written. "
              "Re-run with --apply to act on this.")
    return 0


def _should_continue(result: dict, applying: bool) -> bool:
    """Whether another round is worth running."""
    if not applying:
        return False
    if result.get("status") != "ok":
        return False
    if not result.get("considered"):
        return False
    outcomes = result.get("outcomes") or {}
    # The sweep stops itself once the chain looks down; don't ask it again.
    return not outcomes.get("still_degraded")


def _print_round(index: int, result: dict, *, dry_run: bool) -> None:
    status = result.get("status")
    if status == "disabled":
        print("RECRUITER_RETRY_DEGRADED_ENABLED is off — nothing to do.")
        return

    print(f"\n=== round {index + 1} ===")
    print(f"degraded rows in scope: {result.get('eligible', 0)}")
    print(f"considered this round:  {result.get('considered', 0)}")

    if dry_run:
        header = (
            f"  {'id':>6}  {'kind':<19} {'conf':>5} {'match':>6} "
            f"{'route':<6} {'row status':<13} {'draft':<6} age"
        )
        print(header)
        for row in result.get("plan", []):
            confidence = (
                "    ?" if row["confidence"] is None else f"{row['confidence']:>5.2f}"
            )
            match = "     -" if row["match_score"] is None else f"{row['match_score']:>6.0f}"
            age = "   -" if row["age_days"] is None else f"{row['age_days']:>3}d"
            print(
                f"  {row['recruiter_email_id']:>6}  {row['kind']:<19} {confidence} "
                f"{match} {str(row['route'] or '-'):<6} {row['row_status']:<13} "
                f"{'yes' if row['has_draft'] else 'no':<6} {age}"
            )
        return

    for outcome, count in sorted(
        (result.get("outcomes") or {}).items(), key=lambda kv: -kv[1]
    ):
        print(f"  {outcome}: {count}")
    print(f"  handed to the sender: {result.get('dispatched', 0)}")


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
