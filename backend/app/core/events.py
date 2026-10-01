"""The few things this product actually *does*, written down as they happen.

There is a difference between a log that records faults and a log that records
work, and this application only had the first. Every failure path had a line;
the success paths had none. A mailbox that sent forty messages today and one
that sent none produced identical output — silence — so the question an
operator asks first, *is it working*, could only be answered by querying the
database, and the question they ask second, *when did it stop*, could not be
answered at all.

That asymmetry is how :mod:`app.services.inbound_scanner`'s two outages lasted
as long as they did. Nothing was throwing. Both agents had simply stopped
producing, and no absence of a log line is distinguishable from a quiet week.

So the handful of events that constitute the product — a message left, a
campaign began, a recruiter answered, an application went in — are emitted
here, through one function, for three reasons that a bare ``logger.info`` at
each call site would not give:

**One field name per concept.** ``campaign_id`` everywhere, never
``campaign``, ``cid`` or an id interpolated into a sentence. A dashboard is
built on field names agreeing across the call sites that emit them, and nothing
enforces that when each site writes its own line.

**A closed set of event names.** :data:`EVENTS` is the list, and
``tests/test_observability.py`` holds every emitter to it, so an event cannot
be renamed on one side of a query and not the other.

**Never a raise.** An outreach send that completed must not be turned into a
failure by the line that records it having completed. :func:`emit` swallows
everything, because the alternative is observability that causes outages.

What is deliberately *not* here: recipients. The events carry ids and a
recipient domain, never an address or a name. Bounce and throttling questions
are domain-shaped ("is Outlook deferring us?"), which the domain answers; the
address adds nothing operational and would copy the contact list of every user
on the deployment into a log aggregator with a different retention policy and a
different set of people able to read it.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.event")

# --------------------------------------------------------------------------
# The closed set.
# --------------------------------------------------------------------------

#: An outreach message was accepted by Gmail. The one event that costs money,
#: burns reputation, and cannot be undone.
EMAIL_SENT = "email.sent"

#: A message will not be sent and has been written off. Distinct from a retry:
#: this is terminal.
EMAIL_FAILED = "email.failed"

#: The reputation gate would not release a message, so it became a draft for a
#: human. Not a failure — the work survives — but it is the event behind
#: "Needs review" growing, which nothing announced.
EMAIL_PARKED = "email.parked"

#: A campaign moved from configured to running.
CAMPAIGN_LAUNCHED = "campaign.launched"

#: A campaign reached the end of its work — every message it queued has been
#: sent, written off, or discarded.
#:
#: The other end of :data:`CAMPAIGN_LAUNCHED`, and its absence made the pair
#: useless for the question they exist to answer. A launch was recorded and a
#: completion was not, so a campaign that started and then stopped dead looked
#: exactly like one still working through its contacts — for as long as anyone
#: cared to wait. "How long do campaigns take" and "did that one ever finish"
#: were both unanswerable from the log, which is where the second of them is
#: asked.
CAMPAIGN_COMPLETED = "campaign.completed"

#: A campaign stopped because it could not proceed. Terminal, and distinct from
#: completion: no more mail will go out and the work did *not* get done.
CAMPAIGN_FAILED = "campaign.failed"

#: Inbound mail was matched to a thread and stored. The other half of the
#: product, and previously the quieter one.
REPLY_RECEIVED = "reply.received"

#: An application was submitted to an employer — by form fill or by mail.
APPLICATION_SUBMITTED = "application.submitted"

#: Every name above. Import this rather than re-listing them.
EVENTS = frozenset(
    {
        EMAIL_SENT,
        EMAIL_FAILED,
        EMAIL_PARKED,
        CAMPAIGN_LAUNCHED,
        CAMPAIGN_COMPLETED,
        CAMPAIGN_FAILED,
        REPLY_RECEIVED,
        APPLICATION_SUBMITTED,
    }
)

#: Long enough for a reason sentence, short enough that no single field can
#: dominate a line. Reasons are the one free-text field these events carry and
#: they arrive from places — a Gmail error body, an LLM refusal — with no
#: length discipline of their own.
_MAX_VALUE = 300


def emit(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Record that *event* happened, with *fields* as queryable attributes.

    The message is the event name itself. It reads well enough in a terminal
    (``app.event  email.sent``) and means the human-readable and the
    machine-readable halves of the record cannot drift apart, which they do the
    moment a prose message is maintained alongside an ``event`` field.

    ``None`` values are dropped rather than emitted as nulls: a campaign id is
    absent for a one-off reply, and a field that is present-but-null on a third
    of records makes "group by campaign_id" quietly wrong in a way an absent
    field does not.
    """
    try:
        payload = {"event": event}
        payload.update(
            {k: _clip(v) for k, v in fields.items() if v is not None}
        )
        logger.log(level, "%s", event, extra=payload)
    except Exception:  # noqa: BLE001 - see the module docstring
        # Deliberately not `logger` — if the logger is what is broken, the
        # recovery must not go through it in the same shape that just failed.
        logging.getLogger(__name__).warning(
            "could not emit event %r", event, exc_info=True
        )


def recipient_domain(address: str | None) -> str | None:
    """The part of an address worth keeping.

    See the module docstring for why the rest is dropped. Lowercased so
    ``Gmail.com`` and ``gmail.com`` are one bucket rather than two, which is
    the whole point of grouping by it.
    """
    if not address or "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip().lower()
    return domain or None


def _clip(value: Any) -> Any:
    return value[:_MAX_VALUE] if isinstance(value, str) else value
