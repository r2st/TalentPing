"""May a reply drafted on our own thread go back out unread?

One pure function, no I/O, for the same reason
:mod:`app.services.reply_routing` is one: this is the branch that decides
whether a recruiter receives an email in the candidate's name without the
candidate having seen it. Every path is testable in a line, and turning the
capability off is one boolean rather than an unpicked pipeline.

**This is the third auto-send policy in the product, and they are not the same
question.**

* :mod:`app.services.send_policy` — may we cold-email a *stranger* unread?
  Trials, pauses and a daily ceiling; it is about volume and about the user
  building trust in the agent.
* ``recruiter_reply_service.auto_reply_allowed`` — may we answer mail that
  arrived out of the blue? A server switch, a user switch and a cap; it is about
  whether the sender is really a recruiter at all.
* **Here** — may we answer someone the user is already in a conversation with?
  There is no doubt about the sender and no volume to control. The only question
  left is whether we understood what they said, which is why this policy is
  confidence-shaped and the other two are not.

**What holds a reply back.** In the order it is asked:

1. The user turned inbox auto-reply off.
2. The intent is not one they let the agent answer. OFFER and NOT_INTERESTED are
   off by default: an offer is the email in the whole process most worth reading
   twice, and a rejection answered by a machine is how a bridge gets burned
   politely. OTHER, OUT_OF_OFFICE and UNSUBSCRIBE can never be enabled — the
   first has nothing to answer and the last two must never be answered at all.
3. The draft is the deterministic template rather than a generation. This one is
   not in the original brief and is the most important of the lot: the whole
   claim behind auto-reply is that the reply is *about* the message it answers.
   When the model chain is down, ``reply_agent`` returns a fallback that is
   contextual only in its template — a fine thing to show someone under "Scout
   suggests", and not a thing to send in their name. So an LLM outage degrades
   this feature to exactly what it was before: drafts, waiting. A row that does
   not say which it is gets the same answer, for a blunter reason: the drafts
   that predate ``Email.drafted_with`` are the ones written while the chain was
   down, so "we don't know" and "it's a template" are the same population.
4. The confidence is below the bar. The user sets the bar; some intents demand
   more than it and none may demand less, because a slider that can be dragged
   below what is safe is not a safety property.

**A hold is never a drop.** Every declining path leaves the same DRAFT the inbox
has always shown, now flagged so the user can find it — which is what makes it
safe for this module to fail closed on anything it is unsure of, and it does
throughout: a missing preference row, an unknown confidence and an unrecognised
intent all hold.

Note what this module does *not* re-check: whether the mailbox may send at all.
That is the reputation gate, it lives at send time in
``tasks.email_tasks.send_outreach_email``, and every message routed here still
passes through it. Auto-send decides that nobody needs to read this first; it
does not decide that the mail goes out now.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.autopilot import AutopilotPreference
from app.models.email import ReplyIntent

# Intents that may never auto-reply, whatever the user's list says. Not a
# default — a rule. OUT_OF_OFFICE and UNSUBSCRIBE never reach here (the inbox
# does not draft for them), and they are named anyway so that a future caller
# that does reach here cannot mail an auto-responder in a loop or answer someone
# who asked us to stop.
NEVER_AUTO = frozenset(
    {ReplyIntent.OTHER, ReplyIntent.OUT_OF_OFFICE, ReplyIntent.UNSUBSCRIBE}
)

# What the product ships answering on its own. The two intents where a fast,
# ordinary reply is worth more than a considered one: enthusiasm, and a question
# with a factual answer.
DEFAULT_INTENTS: tuple[str, ...] = ("INTERESTED", "QUESTION", "SCHEDULING")

# Confidence an intent needs *at least*, as a percentage, whatever bar the user
# set. Anything with a diary, a number or a door closing in it is here.
INTENT_FLOOR: dict[ReplyIntent, int] = {
    # Agreeing to a time commits the candidate to being somewhere.
    ReplyIntent.SCHEDULING: 95,
    # Off by default; if switched on, it answers at near-certainty or not at all.
    ReplyIntent.OFFER: 95,
    ReplyIntent.NOT_INTERESTED: 95,
}

DEFAULT_MIN_CONFIDENCE = 85

# Stable codes. The UI and the tests branch on these; the sentences beside them
# are free to be rewritten.
SENT = "auto_send"
REASON_DISABLED = "auto_reply_off"
REASON_INTENT = "intent_held"
REASON_CONFIDENCE = "low_confidence"
REASON_NO_CONFIDENCE = "confidence_unknown"
REASON_TEMPLATE = "template_fallback"
# The row does not say how it was written. Distinct from the code above
# because the sentence beside it has to stay true: "no model was reachable"
# is a claim, and we do not know it.
REASON_UNKNOWN_SOURCE = "draft_source_unknown"


@dataclass(frozen=True)
class ReplyDecision:
    """Whether this draft sends itself, and — when it does not — why not.

    ``reason`` is a sentence for the user; it is what the inbox badge says and
    what lands in ``Email.attention_reason``. ``code`` is for everything else.
    """

    auto_send: bool
    code: str
    reason: str
    # The bar this was judged against, as a percentage, so the UI can say "78%,
    # and 85% was needed" without recomputing the floors.
    threshold: int = DEFAULT_MIN_CONFIDENCE

    @property
    def needs_attention(self) -> bool:
        """A held reply is one the user is being asked to look at."""
        return not self.auto_send

    def __bool__(self) -> bool:  # pragma: no cover - convenience at call sites
        return self.auto_send


def allowed_intents(pref: AutopilotPreference | None) -> set[ReplyIntent]:
    """The intents this user lets the agent answer on its own.

    Unknown names in the stored list are dropped rather than raising: the column
    is user-editable JSON, and an intent removed from the enum must not break
    every poll for everyone who had it enabled.
    """
    names = DEFAULT_INTENTS if pref is None else (pref.inbox_auto_reply_intents or ())
    out: set[ReplyIntent] = set()
    for name in names:
        try:
            intent = ReplyIntent(str(name).upper())
        except ValueError:
            continue
        if intent not in NEVER_AUTO:
            out.add(intent)
    return out


def threshold_for(intent: ReplyIntent, pref: AutopilotPreference | None) -> int:
    """The bar *this* intent has to clear, as a percentage.

    The user's bar, raised by the intent's floor where there is one. Never
    lowered — see the module docstring.
    """
    base = DEFAULT_MIN_CONFIDENCE
    if pref is not None and pref.inbox_auto_reply_min_confidence is not None:
        base = int(pref.inbox_auto_reply_min_confidence)
    return max(base, INTENT_FLOOR.get(intent, 0))


def decide(
    intent: ReplyIntent,
    confidence: float | None,
    pref: AutopilotPreference | None,
    *,
    drafted_with: str | None = "llm",
) -> ReplyDecision:
    """Route one drafted reply.

    *confidence* is :class:`app.services.reply_classifier.Classification`'s, 0..1.
    ``None`` means nobody knows — a draft written before routing existed, or a
    classification we did not keep — and it holds.

    *drafted_with* is ``ReplyDraft.generated_with``: ``"llm"`` for a generated
    reply, anything else for the deterministic template. ``None`` means the row
    predates ``Email.drafted_with`` and nothing recorded it, which holds for the
    same reason an unknown confidence does — and with more cause, since the
    drafts that predate the column are the ones written while the model chain
    was down, which is exactly what makes a draft a template.
    """
    bar = threshold_for(intent, pref)

    if pref is not None and not pref.inbox_auto_reply:
        return ReplyDecision(
            False,
            REASON_DISABLED,
            "Drafted for your review — automatic replies are off.",
            bar,
        )
    # No preferences row at all: the user has never opted into anything. Held,
    # for the same reason ``send_policy`` treats an absent row as review mode.
    if pref is None:
        return ReplyDecision(
            False,
            REASON_DISABLED,
            "Drafted for your review — automatic replies aren't set up yet.",
            bar,
        )

    if intent not in allowed_intents(pref):
        return ReplyDecision(
            False,
            REASON_INTENT,
            f"{_label(intent)} replies are always yours to send.",
            bar,
        )

    if drafted_with is None:
        return ReplyDecision(
            False,
            REASON_UNKNOWN_SOURCE,
            "We can't tell whether a model wrote this one — worth a read.",
            bar,
        )

    if drafted_with != "llm":
        return ReplyDecision(
            False,
            REASON_TEMPLATE,
            "Written from a template because no model was reachable — worth a read.",
            bar,
        )

    if confidence is None:
        return ReplyDecision(
            False,
            REASON_NO_CONFIDENCE,
            "Couldn't tell how sure the classifier was, so this is for you to send.",
            bar,
        )

    percent = round(float(confidence) * 100)
    if percent < bar:
        return ReplyDecision(
            False,
            REASON_CONFIDENCE,
            f"Only {percent}% sure this is {_label(intent).lower()} — {bar}% is the bar.",
            bar,
        )

    return ReplyDecision(
        True, SENT, f"Sent automatically — {percent}% sure this is "
        f"{_label(intent).lower()}.", bar
    )


_LABELS: dict[ReplyIntent, str] = {
    ReplyIntent.INTERESTED: "Interest",
    ReplyIntent.NOT_INTERESTED: "Rejection",
    ReplyIntent.SCHEDULING: "Scheduling",
    ReplyIntent.QUESTION: "Question",
    ReplyIntent.OFFER: "Offer",
    ReplyIntent.OUT_OF_OFFICE: "Out-of-office",
    ReplyIntent.UNSUBSCRIBE: "Unsubscribe",
    ReplyIntent.OTHER: "Unclear",
}


def _label(intent: ReplyIntent) -> str:
    return _LABELS.get(intent, intent.value.title())


__all__ = [
    "DEFAULT_INTENTS",
    "DEFAULT_MIN_CONFIDENCE",
    "INTENT_FLOOR",
    "NEVER_AUTO",
    "REASON_UNKNOWN_SOURCE",
    "ReplyDecision",
    "allowed_intents",
    "decide",
    "threshold_for",
]
