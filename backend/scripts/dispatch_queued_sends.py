#!/usr/bin/env python3
"""Send the QUEUED outreach backlog now, deliberately ignoring the send-time slot.

This is an operator override, not part of the pipeline, and it exists because
the normal recovery path cannot express "today".

``sweep_stranded_sends`` and ``enqueue_campaign_sends`` both route their
countdown through ``email_tasks.send_countdown``, which lands every message in
the *recipient's* business hours. That is the right default and nothing here
changes it. But it means a backlog discovered on a Sunday is scheduled for
Monday morning, and there is no argument to either of them that says otherwise:
the slot search is unconditional. On 2026-08-09 the backlog was 54 messages
composed between 07-28 and 08-04, all of them pinned to Mon 09:00-15:57 UTC,
and the operator's instruction was to send them that day.

So the countdown here is raw. Two consequences worth stating plainly, because
the whole point of this file is that it is the one place they are true:

* **Recipients get mail outside their business hours.** That is the override.
  Run this only when someone has decided that is acceptable.
* **Nothing else is bypassed.** ``send_outreach_email`` still re-checks the
  campaign pause, the opt-out, the hard-bounce suppression list and the full
  reputation gate at send time, and still parks anything the gate will not
  release. This script chooses *when the task runs*, not *whether it sends* —
  which is why it is safe to point at a backlog without auditing it first.

The spacing is not decoration either. Fifty-four messages published with
``countdown=0`` arrive at Gmail as one burst from a personal mailbox, which is
the single most reliable way to get that mailbox throttled or flagged — and a
throttled mailbox sends *none* of the backlog, so the burst defeats the errand
it was in a hurry for. The default trickles them out over a quarter of an hour,
which is still unambiguously "now" and looks like a person clearing a queue.

Idempotent by construction: it only ever selects rows that are still QUEUED, and
``_claim_for_send`` takes each row under ``FOR UPDATE SKIP LOCKED`` and re-checks
the status, so running it twice does not send anything twice.

Usage::

    python backend/scripts/dispatch_queued_sends.py --dry-run
    python backend/scripts/dispatch_queued_sends.py --spacing 18
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_ENV_FILE = "/opt/TalentPing/.env"


def load_env_file(path: str) -> int:
    """Populate ``os.environ`` from a systemd-style env file.

    Not ``. .env``. systemd takes ``KEY=value`` to the end of the line; bash
    word-splits, so an unquoted value containing a space silently becomes its
    first word — the exact trap ``verify_deploy.envfile`` checks for, and this
    file is run by the same hands in the same shell. Parsing it the way systemd
    does is the only way the process sees what the services see.

    Existing environment wins, so an operator can override one value inline
    without editing the file.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return 0
    loaded = 0
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value
        loaded += 1
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help=f"systemd env file to load before importing the app (default {DEFAULT_ENV_FILE})",
    )
    parser.add_argument(
        "--spacing",
        type=int,
        default=18,
        help="seconds between consecutive sends (default 18; 0 for one burst)",
    )
    parser.add_argument(
        "--start-after",
        type=int,
        default=5,
        help="seconds before the first send (default 5)",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="dispatch at most N rows (0 = all)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be dispatched and exit without publishing",
    )
    args = parser.parse_args()

    load_env_file(args.env_file)

    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models.application import Application
    from app.models.email import Email, EmailDirection, EmailStatus
    from app.models.email_thread import EmailThread
    from app.tasks.email_tasks import send_outreach_email

    db = SessionLocal()
    try:
        rows = list(
            db.scalars(
                select(Email)
                .join(EmailThread, Email.thread_id == EmailThread.id)
                .join(Application, EmailThread.application_id == Application.id)
                .where(
                    Email.direction == EmailDirection.SENT,
                    Email.status == EmailStatus.QUEUED,
                )
                .order_by(Email.created_at, Email.id)
            )
        )
        if args.limit:
            rows = rows[: args.limit]

        if not rows:
            print("nothing QUEUED; nothing to do")
            return 0

        print(f"{len(rows)} QUEUED message(s) to dispatch")
        countdown = args.start_after
        dispatched = 0
        for email in rows:
            if args.dry_run:
                print(
                    f"  [dry-run] email {email.id} -> {email.to_address} "
                    f"in {countdown}s (created {email.created_at})"
                )
            else:
                send_outreach_email.apply_async(args=[email.id], countdown=countdown)
                # Same stamp every other dispatcher keeps, so the hourly
                # `sweep_stranded_sends` can see that these rows already have a
                # task aiming at them. Without it the sweep would find 54
                # week-old QUEUED rows an hour from now and publish the whole
                # backlog a second time.
                email.send_dispatched_at = datetime.now(UTC)
                dispatched += 1
                print(f"  email {email.id} -> {email.to_address} in {countdown}s")
            countdown += args.spacing

        if args.dry_run:
            print("dry run: nothing published")
        else:
            db.commit()
            last = countdown - args.spacing
            print(f"dispatched {dispatched}; last one fires in ~{last}s")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
