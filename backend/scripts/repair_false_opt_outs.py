#!/usr/bin/env python3
"""Undo the opt-outs a platform footer caused, and only those.

``reply_classifier`` used to score the bare substring "unsubscribe" as decisive.
Every message a platform forwards on someone's behalf carries that word in its
footer — LinkedIn ends an InMail notification with ``Unsubscribe: https://...``
— so a recruiter quoting an hourly rate was read as asking never to be contacted
again. ``_apply_intent`` set ``Recruiter.opted_out``, which is the CAN-SPAM
consent flag and is checked before every send forever, and returned without
drafting anything. The conversation ended there, silently, and nothing in the
product said why.

This reverses that, and it is deliberately timid about it, because the two ways
of being wrong are not symmetric. Leaving a real opportunity dead costs the user
an opportunity. Un-suppressing someone who genuinely asked us to stop costs a
CAN-SPAM violation and a spam complaint against the mailbox everything else
depends on. So a row is only repaired when **all** of the following hold:

* Every inbound message on that contact currently marked ``UNSUBSCRIBE``
  re-classifies as something else under the fixed rules. One message that still
  reads as an opt-out disqualifies the contact entirely.
* At least one such message exists — a contact with no misread message is not
  this bug's doing and is left exactly as it is. That is what keeps the opt-outs
  that came from the web link, which leave no inbound row, out of scope.
* The contact is not hard-bounced. ``opted_out`` on a bounced address is
  residue from before ``bounce_service`` split consent from deliverability, and
  the address is undeliverable regardless, so there is nothing to win by
  touching it.

Re-classification is done with the **rules only**, never the model: this has to
be deterministic, reproducible and readable in a diff. That is also exactly the
path that caused the damage — ``classify_reply_detailed`` settles UNSUBSCRIBE
from the rule score before any model call.

    # what would happen, changing nothing (the default)
    python scripts/repair_false_opt_outs.py

    # do it
    python scripts/repair_false_opt_outs.py --apply

Run it on the box, as the service user, with the venv that runs the app::

    ssh root@$HOST 'cd /opt/TalentPing/backend && sudo -u talentping env \
        "$(grep -m1 "^DATABASE_URL=" ../.env)" PYTHONPATH=/opt/TalentPing/backend \
        .venv/bin/python scripts/repair_false_opt_outs.py'

Idempotent: a message whose intent has been corrected is no longer UNSUBSCRIBE,
so a second run finds nothing.
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
from app.models.email import Email, EmailDirection, ReplyIntent  # noqa: E402
from app.models.email_thread import EmailThread  # noqa: E402
from app.models.recruiter import DeliveryState, Recruiter  # noqa: E402
from app.services.reply_classifier import DECISIVE, _rule_based, score  # noqa: E402
from app.services.reply_text import visible_text  # noqa: E402


def still_reads_as_opt_out(body: str | None) -> bool:
    """Whether the fixed rules still settle this message as an unsubscribe.

    Mirrors ``classify_reply_detailed``'s automatic path exactly: visible text,
    rule score, decisive or not. No model call — see the module docstring.
    """
    visible = visible_text(body)
    if not visible:
        return False
    return score(visible).get(ReplyIntent.UNSUBSCRIBE, 0.0) >= DECISIVE


def plan(db) -> dict:
    """What would change, per contact, with the reason for each verdict."""
    rows = db.execute(
        select(Email, Recruiter)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .join(Recruiter, Application.recruiter_id == Recruiter.id)
        .where(
            Email.direction == EmailDirection.RECEIVED,
            Email.intent == ReplyIntent.UNSUBSCRIBE,
        )
        .order_by(Email.id)
    ).all()

    by_recruiter: dict[int, dict] = {}
    for email, recruiter in rows:
        entry = by_recruiter.setdefault(
            recruiter.id,
            {
                "recruiter_id": recruiter.id,
                "email": recruiter.email,
                "opted_out": recruiter.opted_out,
                "hard_bounced": recruiter.delivery_state == DeliveryState.HARD_BOUNCED,
                "messages": [],
            },
        )
        misread = not still_reads_as_opt_out(email.body_text)
        entry["messages"].append(
            {
                "email_id": email.id,
                "misread": misread,
                "reclassified_as": _rule_based(visible_text(email.body_text)).value,
                "excerpt": " ".join((visible_text(email.body_text) or "").split())[:90],
            }
        )

    repair, leave = [], []
    for entry in by_recruiter.values():
        if entry["hard_bounced"]:
            entry["verdict"] = "left alone: address hard-bounced, opt-out is not this bug"
            leave.append(entry)
        elif not all(m["misread"] for m in entry["messages"]):
            entry["verdict"] = "left alone: still reads as a genuine opt-out"
            leave.append(entry)
        else:
            entry["verdict"] = "repair"
            repair.append(entry)

    return {"repair": repair, "leave": leave}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually clear the flags. Without it, nothing is written.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = plan(db)
        if args.apply:
            for entry in result["repair"]:
                recruiter = db.get(Recruiter, entry["recruiter_id"])
                if recruiter is not None:
                    recruiter.opted_out = False
                for message in entry["messages"]:
                    email = db.get(Email, message["email_id"])
                    if email is not None:
                        # The stored intent drove the opt-out and is still what
                        # the inbox shows. Correcting the flag and leaving the
                        # verdict behind would be half a repair.
                        email.intent = ReplyIntent(message["reclassified_as"])
            db.commit()
        result["dry_run"] = not args.apply
    finally:
        db.close()

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    verb = "would repair" if result["dry_run"] else "repaired"
    messages = sum(len(e["messages"]) for e in result["repair"])
    print(
        f"{verb} {len(result['repair'])} contact(s) across {messages} misread "
        f"message(s)"
    )
    print()
    for entry in result["repair"]:
        print(f"  {entry['email']}  (was opted_out={entry['opted_out']})")
        for message in entry["messages"]:
            print(
                f"      #{message['email_id']:>5}  UNSUBSCRIBE -> "
                f"{message['reclassified_as']:<15} {message['excerpt']}"
            )
    if result["leave"]:
        print(f"\nleft alone ({len(result['leave'])}):")
        for entry in result["leave"]:
            print(f"  {entry['email']}  — {entry['verdict']}")
    if result["dry_run"]:
        print("\nNothing was written. Re-run with --apply to act on this.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
