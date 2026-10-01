"""Is this inbound message a recruiter, and what job is it about?

Two stages, cheap first.

**Stage 1 — deterministic pre-filter.** A candidate's inbox is mostly LinkedIn
digests and ATS acknowledgements, and none of those need a model to recognise:
they come from a handful of no-reply addresses and carry unmistakable subjects.
Catching them here is the difference between the feature costing a few model
calls a day and a few dozen, and it is also more accurate than the model on
exactly the cases it covers.

**Stage 2 — the LLM**, for what survives. Classification and extraction are one
call rather than two: the model has already read the message, and asking a second
time for fields it just parsed doubles the cost of the highest-volume path in the
feature for no accuracy gain.

Two rules make the failure modes safe:

* **JSON or nothing.** The free tier's reasoning models return ``content: null``
  with the answer parked in ``reasoning`` (see :mod:`openrouter_client`), so a
  bare word is unreliable and a JSON object is not — ``extract_json_object``
  finds it inside a scratchpad.
* **Uncertainty is ``UNKNOWN``, never a guess.** The same rule
  :func:`app.services.reply_classifier.classify_reply` follows when it returns
  ``OTHER``. ``UNKNOWN`` can never be replied to, so a degraded classifier costs
  the user speed and cannot cost them an embarrassing email.

**"Costs the user speed" was only true once something asked again.** For a long
time nothing did: a verdict reached during an outage was the verdict forever, and
because :func:`app.services.reply_routing.decide` multiplies confidence by the
match score, a fallback reading could not clear the auto bar at any fit at all.
The speed cost was unbounded — 455 of production's 822 messages, 223 of them real
recruiter mail nobody ever answered. Hence :func:`is_degraded` and the sweep in
``recruiter_reply_tasks.retry_degraded_classifications`` that reads it.

Nothing here touches the database or Gmail: a message dict in, a verdict out.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.models.recruiter_email import RecruiterEmailKind
from app.services import untrusted
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion_detailed,
    extract_json_object,
)
from app.services.places import fold_diacritics
from app.services.reply_classifier import looks_like_auto_reply

logger = logging.getLogger(__name__)

# How much of a message the model reads. Recruiter mail is short; this never
# truncates a real one and stops a misfiled newsletter blowing the context.
MAX_BODY_CHARS = 4000

_VALID_KINDS = {k.value for k in RecruiterEmailKind}

# ``classified_by`` markers for the three keyless paths. They used to share the
# bare string "rules", and that conflation is what made a degraded reading
# indistinguishable from a deterministic one — see :func:`is_degraded`.
#
# BY_PREFILTER   the no-model path that is *meant* to be the answer. A LinkedIn
#                digest is a digest whatever the model chain is doing.
# BY_FALLBACK    the model was wanted and could not be had. A guess, and the
#                only one of the three worth asking again later.
# BY_EMPTY       there was nothing to read. Asking again cannot help.
BY_PREFILTER = "rules"
BY_FALLBACK = "rules:fallback"
BY_EMPTY = "rules:empty"


@dataclass
class Classification:
    """What the message is, how sure we are, and what job it describes."""

    kind: RecruiterEmailKind = RecruiterEmailKind.UNKNOWN
    confidence: float = 0.0
    # One of the ``BY_*`` markers when no model decided, otherwise
    # "<provider>:<model>". See those constants for why there are three.
    classified_by: str = BY_PREFILTER
    role_title: str | None = None
    company: str | None = None
    location: str | None = None
    remote: bool | None = None
    salary_text: str | None = None
    seniority: str | None = None
    # Direct questions the recruiter asked, which the reply is told to address —
    # and which alone decide whether the brief is QUESTION or INTERESTED. Checked
    # against the message before they get here: see `_grounded_asks`.
    asks: list[str] = field(default_factory=list)
    reason: str = ""
    # Did a provider actually return something, whatever we made of it?
    #
    # Distinct from ``classified_by`` and not derivable from it: a model that
    # answers with prose instead of JSON leaves the same ``BY_FALLBACK`` verdict
    # as a chain that never answered. The retry sweep has to tell those apart —
    # one means "come back later", the other means "later will not help" — and
    # conflating them lets two unparseable messages block a whole backlog. False
    # on the paths that never ask (the pre-filter, an empty message).
    model_answered: bool = False

    @property
    def is_actionable(self) -> bool:
        return self.kind in (
            RecruiterEmailKind.RECRUITER_OUTREACH,
            RecruiterEmailKind.HIRING_MANAGER,
        )

    def extracted(self) -> dict[str, Any]:
        """The JSON blob stored on the row — only the opportunity fields."""
        return {
            "role_title": self.role_title,
            "company": self.company,
            "location": self.location,
            "remote": self.remote,
            "salary_text": self.salary_text,
            "seniority": self.seniority,
            "asks": list(self.asks),
        }

    def as_job_text(self, body: str, subject: str | None = None) -> str:
        """Render the message as something the JD parser can read.

        A recruiter's email *is* a job description, just an informal one. Giving
        the parser the extracted fields first and the prose after means the
        deterministic scorer sees a title and a location where the model found
        them, and still has the full text to pull skills out of.
        """
        lines = []
        if self.role_title:
            lines.append(self.role_title)
        if self.company:
            lines.append(f"Company: {self.company}")
        if self.location:
            lines.append(f"Location: {self.location}")
        if self.remote:
            lines.append("This is a remote role.")
        if self.salary_text:
            lines.append(f"Salary: {self.salary_text}")
        if self.seniority:
            lines.append(f"Seniority: {self.seniority}")
        if subject and not self.role_title:
            lines.append(subject)
        lines.append("")
        lines.append(body or "")
        return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# Stage 1: the deterministic pre-filter                                        #
# --------------------------------------------------------------------------- #

# Senders that are structurally incapable of being a person writing to you.
_NOREPLY_MARKERS = (
    "noreply@", "no-reply@", "no_reply@", "donotreply@", "do-not-reply@",
    "notifications@", "notification@", "mailer@", "bounce@",
)

# Job boards, by the address their digests actually come from.
_JOB_ALERT_SENDERS = (
    "jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com",
    "@indeed.com", "@indeedemail.com", "@glassdoor.com", "@ziprecruiter.com",
    "@monster.com", "@dice.com", "@wellfound.com", "@angel.co",
    "@otta.com", "@welcometothejungle.com",
)
_JOB_ALERT_PHRASES = (
    "new jobs for you", "jobs you may be interested in", "job alert",
    "recommended for you", "new opportunities matching", "your job alert",
    "top job picks", "jobs similar to",
)

# Applicant tracking systems, which mail you about applications you made.
_ATS_SENDERS = (
    "@greenhouse.io", "@myworkday.com", "@myworkdayjobs.com", "@lever.co",
    "@hire.lever.co", "@ashbyhq.com", "@smartrecruiters.com", "@icims.com",
    "@taleo.net", "@successfactors.com", "@jobvite.com", "@workable.com",
    "@breezy.hr", "@recruitee.com", "@bamboohr.com",
)
_ATS_PHRASES = (
    "we have received your application", "thank you for applying",
    "your application has been received", "application received",
    "thanks for your interest in", "we received your application",
    "your application to", "application confirmation",
)

# The pre-filter's confidence. High but not 1.0 — an in-house recruiter really
# can mail you from an ATS domain, and the number should leave room for that.
_PREFILTER_CONFIDENCE = 0.95


def is_degraded(classified_by: str | None, confidence: float | None) -> bool:
    """Was this verdict a guess made because no model could be reached?

    The one question the retry sweep asks of a stored row. It is deliberately a
    function of what is *on the row* rather than of what the classifier returned,
    because the rows that need it most were written months before the markers
    above existed.

    **The legacy arm is the interesting half.** Rows written before ``BY_FALLBACK``
    all say ``"rules"``, which conflates the pre-filter with the fallback. They
    are still separable, and by construction rather than by guesswork: every
    ``_prefilter`` verdict carries 0.9 or 0.95, and no ``_rule_based`` path can
    return more than ``_RULE_CONFIDENCE_CEILING``. So the ceiling *is* the
    boundary, and it holds for every row production has. Checked against the
    constant rather than against a literal, so raising the ceiling cannot
    silently start re-reading job-board digests.

    An unrecorded ``classified_by`` reads as degraded: not knowing how a verdict
    was reached is not a reason to trust it, and one wasted model call is the
    whole cost of being wrong here.
    """
    if classified_by is None:
        return True
    if classified_by == BY_FALLBACK:
        return True
    if classified_by == BY_PREFILTER:
        return float(confidence or 0.0) <= _RULE_CONFIDENCE_CEILING
    # BY_EMPTY, or a real "<provider>:<model>". Neither is worth asking again.
    return False


def _is_noreply(address: str | None) -> bool:
    return any(marker in (address or "").lower() for marker in _NOREPLY_MARKERS)


def _prefilter(
    from_address: str, subject: str, body: str, reply_to: str | None = None
) -> Classification | None:
    """Classify the obvious automated mail without a model call.

    Returns ``None`` when the message needs a real look, which is the only case
    that costs anything.
    """
    sender = (from_address or "").lower()
    haystack = f"{subject or ''}\n{body or ''}".lower()

    # An automatic responder, first, because it is the one verdict here whose
    # cost of being missed is a *sent* message rather than a mislabelled row.
    #
    # The thread poller has read this signal since it learned to answer mail on
    # its own: ``reply_classifier.classify_reply_detailed`` asks it before it
    # reads a word of the body, and an OUT_OF_OFFICE is in
    # ``thread_reply_policy.NEVER_AUTO`` precisely so a responder cannot be
    # answered by a machine that then answers it again. This pipeline never
    # asked. Its input is *unsolicited* mail, so an "Automatic reply: Senior
    # Backend Engineer at Northwind" — the subject a vacationing recruiter's
    # Exchange writes, quoting the role out of the original — reads to a model
    # exactly like a recruiter writing about a role, classifies
    # ``RECRUITER_OUTREACH`` at high confidence, and on a good profile match
    # routes ``AUTO``: a reply sent in the candidate's name to a robot, which
    # answers it, which is detected as another new message tomorrow.
    #
    # Only the *subject* is read here — it is what the responding mail server
    # writes and the vacationing human does not, and it is all a stored row
    # keeps. The unambiguous half of the signal is the headers, which are only
    # in hand during the scan; :func:`app.services.inbound_scanner.scan` reads
    # them there and never stores the message at all.
    if looks_like_auto_reply(subject=subject):
        return Classification(
            kind=RecruiterEmailKind.NOT_RECRUITER,
            confidence=_PREFILTER_CONFIDENCE,
            reason="An automatic out-of-office reply, not a person writing.",
        )

    if any(marker in sender for marker in _JOB_ALERT_SENDERS) or any(
        phrase in haystack for phrase in _JOB_ALERT_PHRASES
    ):
        return Classification(
            kind=RecruiterEmailKind.JOB_ALERT,
            confidence=_PREFILTER_CONFIDENCE,
            reason="Job-board digest, matched without a model call.",
        )

    if any(marker in sender for marker in _ATS_SENDERS) or any(
        phrase in haystack for phrase in _ATS_PHRASES
    ):
        return Classification(
            kind=RecruiterEmailKind.ATS_AUTOMATED,
            confidence=_PREFILTER_CONFIDENCE,
            reason="Automated application acknowledgement.",
        )

    # A no-reply address is not a person. Checked last so a job board or ATS gets
    # its more specific label first.
    #
    # Unless the sender named a human to reply to. Gem, Loxo, Bullhorn and most
    # in-house mail merges send from `noreply@` and put the recruiter in
    # `Reply-To` — real, personal outreach that this rule was discarding on the
    # strength of an address the recruiter never chose. A ``Reply-To`` pointing
    # somewhere answerable means the message gets read properly instead.
    if _is_noreply(sender):
        if reply_to and not _is_noreply(reply_to):
            return None
        return Classification(
            kind=RecruiterEmailKind.NOT_RECRUITER,
            confidence=0.9,
            reason="Sent from an unmonitored address.",
        )

    return None


# --------------------------------------------------------------------------- #
# Stage 2: the model                                                           #
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT = (
    "You triage a job seeker's inbox. Decide whether an email is a real person "
    "contacting them about a specific job opportunity.\n\n"
    "Kinds:\n"
    "- RECRUITER_OUTREACH: an agency or in-house recruiter writing personally "
    "about a role.\n"
    "- HIRING_MANAGER: someone on the hiring team itself (engineering manager, "
    "founder, department head) writing personally about a role.\n"
    "- JOB_ALERT: an automated digest of listings from a job board.\n"
    "- ATS_AUTOMATED: an automated message about an application already made "
    "(received, rejected, scheduled by a system).\n"
    "- NOT_RECRUITER: anything else — newsletters, sales, personal mail, spam.\n\n"
    "Rules:\n"
    "1. A mass mailing is not outreach, however personalised the greeting.\n"
    "2. Return null for any field the email does not state. Do NOT infer the "
    "company from the sender's domain, and do NOT guess seniority from tone.\n"
    "3. `asks` lists direct questions the sender asked the candidate, verbatim.\n"
    "4. `confidence` is your confidence in `kind`, 0.0-1.0. Below 0.5 means "
    "genuinely unsure — use it rather than committing to a guess.\n\n"
    "Return ONLY a JSON object with these keys: kind, confidence, role_title, "
    "company, location, remote, salary_text, seniority, asks, reason."
)


def _coerce_str(value: Any, limit: int = 255) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "n/a", "unknown", ""):
        return None
    return text[:limit]


# --------------------------------------------------------------------------- #
# Questions the sender actually asked                                          #
# --------------------------------------------------------------------------- #

# The vocabulary every question shares. Not a linguistic stopword list — it is
# what is left of "What are your salary expectations?" once the *asking* is
# removed, and the remainder ("salary", "expectations") is the part that has to
# be in the message for the question to be one the sender put there.
_ASK_COMMON_WORDS = frozenset((
    "a", "about", "all", "also", "am", "an", "and", "any", "are", "as", "at",
    "be", "been", "being", "but", "by", "can", "could", "did", "do", "does",
    "doing", "for", "from", "get", "had", "has", "have", "having", "how", "i",
    "if", "in", "into", "is", "it", "its", "know", "let", "like", "may", "me",
    "might", "much", "my", "need", "of", "on", "one", "or", "our", "ours",
    "out", "please", "shall", "should", "so", "some", "tell", "than", "that",
    "the", "their", "them", "then", "there", "these", "they", "this", "those",
    "to", "told", "us", "was", "we", "were", "what", "when", "where",
    "whether", "which", "who", "whom", "whose", "why", "will", "with",
    "would", "you", "your", "yours",
))


def _words(text: str) -> list[str]:
    """The words a question and a message are compared on.

    Accents come off *before* the split, because the class below is ASCII and
    an accented letter is not in it — it is a boundary. "disponibilité" came
    out of here as ``["disponibilit"]``, "immédiate" as ``["imm", "diate"]``,
    "Verfügbarkeit" as ``["verf", "gbarkeit"]``: not words that failed to
    match, words that stopped being words.

    :func:`_grounded_asks` is a two-thirds vote over these, and the docstring
    below says outright that it draws the same line as
    :func:`app.services.job_search_service.matches_query` for the same reason.
    That function folds first — ``term_words`` runs `fold_diacritics` before
    its split — and this one did not, so the shared line was two different
    lines, and the one drawn here fell on recruiters who do not write in
    English.

    It falls in the direction that costs the candidate the reply. A French
    recruiter asking "Quelle est votre disponibilité immédiate ?" has that
    question dropped from ``asks`` the moment the model reports it with the
    spelling the language uses and the message carries it without, or the other
    way round — three of its six fragments miss, the vote fails, and the brief
    reverts from "address each one" to "express interest". The recruiter's
    actual question goes unanswered, in a reply that reads as if it had never
    been asked.

    Worse, what carried the vote in the cases that did survive was
    ``quelles``/``sont``/``votre`` — function words that
    :data:`_ASK_COMMON_WORDS` only knows how to discount in English, so they
    counted as distinctive while the accented content words, the ones that are
    the question, were the fragments that missed.
    """
    return re.findall(r"[a-z0-9]{3,}", fold_diacritics((text or "").lower()))


def _grounded_asks(asks: list[str], subject: str, body: str) -> list[str]:
    """Keep only the questions the message can be shown to contain.

    The prompt asks for the sender's questions **verbatim**, and a free model
    obliges most of the time. When it does not, the invention is not cosmetic:
    ``asks`` selects the reply template — one question turns the whole brief
    from "express interest" into "address each one" — and the brief then
    instructs the model to answer it. A hallucinated "What are your salary
    expectations?" therefore produces a reply that volunteers a number to a
    recruiter who never asked, which is the single thing the COMPENSATION
    section of that same brief exists to prevent.

    The test is :func:`app.services.reply_agent._invents_specifics` applied a
    step earlier: *is it already in the message?* Word-level rather than exact,
    because a model that renders "let me know your availability" as "What is
    your availability?" has reported a real ask in its own punctuation, and
    exact containment would throw that away.

    A two-thirds majority of the question's distinctive words must appear —
    the same line :func:`app.services.job_search_service.matches_query` draws,
    for the same reason: it survives a rephrasing without accepting a question
    that merely shares the word "role".

    Deliberately lenient about generic questions. "Are you interested?" is
    grounded by almost any recruiter email, and that is the right outcome: what
    this has to stop is the *specific* invented question, exactly as the date
    and money guards downstream only ever police concrete details.
    """
    haystack = set(_words(f"{subject} {body}"))
    kept: list[str] = []
    for ask in asks:
        words = _words(ask)
        if not words:
            # No word in it at all. `_coerce_str` will happily turn a stray `3`
            # in the model's array into the string "3", and "They asked: 3" is
            # not a question that can be grounded or answered.
            logger.info("recruiter classifier reported a non-question: %r", ask[:40])
            continue
        distinctive = [w for w in words if w not in _ASK_COMMON_WORDS]
        if not distinctive:
            # Nothing but the vocabulary of asking — "Can you let me know?".
            # There is nothing to check, and nothing to be wrong about either.
            kept.append(ask)
            continue
        hits = sum(1 for w in distinctive if w in haystack)
        if hits >= max(1, (len(distinctive) * 2 + 2) // 3):
            kept.append(ask)
        else:
            logger.info(
                "recruiter classifier reported a question the message does not "
                "contain; dropped: %r",
                ask[:120],
            )
    return kept


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "remote"):
            return True
        if lowered in ("false", "no", "onsite", "on-site"):
            return False
    return None


def _coerce_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    # Models sometimes answer on a 0-100 scale despite the instruction.
    if number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))


# Words that mark a message as human outreach when no model is available. Used
# only by the fallback — and it never produces a confidence high enough to
# auto-reply on its own, so its job is to be *sensitive*, not precise. A false
# positive here costs the candidate a draft they delete; a false negative costs
# them a job they never heard about.
_RECRUITER_HINTS = (
    "i came across your profile", "came across your resume", "came across your",
    "your background", "your profile", "your experience caught",
    "i'm a recruiter", "i am a recruiter", "technical recruiter", "talent partner",
    "talent acquisition", "recruitment consultant", "executive search",
    "hiring manager", "our client is looking", "our client is seeking",
    "we're hiring", "we are hiring", "currently hiring", "actively hiring",
    "an opportunity", "a role", "a position", "an opening", "this opening",
    "job opportunity", "career opportunity", "new opportunity",
    "would you be open to", "are you open to", "open to hearing",
    "interested in a new", "interested in exploring", "would you be interested",
    "reaching out about", "reaching out regarding", "reaching out because",
    "reaching out to see", "get in touch about",
    "your resume", "your cv", "send me your resume", "share your resume",
    "quick chat", "brief call", "schedule a call", "set up a call",
    "salary range", "compensation range", "day rate", "contract role",
    "full-time role", "permanent role", "notice period",
)

# Phrasing that says "this is bulk marketing", not a person with a job. Weighed
# against the recruiter hints rather than checked before them — see
# :func:`_rule_based` — so a newsletter that happens to say "an opportunity"
# doesn't get answered as though a recruiter wrote it, and a recruiter whose
# platform stamped "view this email in your browser" at the top still does.
_NOT_RECRUITER_HINTS = (
    "shop now", "limited time offer",
    "your order", "your invoice", "your receipt", "payment received",
    "webinar", "newsletter", "free trial", "upgrade your plan",
    "verify your email", "reset your password", "security alert",
    "% off", "black friday", "sale ends",
)

# Boilerplate a mail platform wraps around whatever the sender wrote. Not
# evidence of anything: it appears on marketing blasts and on the templated mail
# a perfectly real agency recruiter sends, so it is cut before either hint list
# reads the text. "view this email in your browser" sat in the marketing list and
# was, on its own, enough to file a genuine recruiter's email as NOT_RECRUITER.
_PREHEADER_MARKERS = (
    "view this email in your browser",
    "view in browser",
    "having trouble viewing",
    "if you cannot see this email",
    "can't see this email",
    "trouble viewing this email",
)


def strip_preheader(text: str) -> str:
    """*text* with a mail platform's "view in browser" line removed."""
    lowered = text.lower()
    for marker in _PREHEADER_MARKERS:
        found = lowered.find(marker)
        if found != -1:
            text = text[:found] + text[found + len(marker):]
            lowered = text.lower()
    return text

# The fallback's ceiling, raised from 0.6 to 0.8 so that a mailbox is not
# unanswerable for the duration of a provider outage. At 0.6 the fallback could
# not clear the auto band at *any* match score, which meant every message in a
# degraded week waited for the user by construction.
#
# What 0.8 buys, against the shipped ``recruiter_reply_auto_threshold`` of 65:
# the band is cleared only at ``0.8 × 82``, so nothing short of the top of the
# hint scale (5+ distinct recruiter phrases) *and* a match in the low 80s can be
# answered unread. Three hints score 0.6 and cannot reach it at a perfect match.
# Everything below that drafts.
#
# Read the arithmetic against the setting rather than against a number written
# here: an operator who lowers ``recruiter_reply_auto_threshold`` lowers the bar
# a keyword-only reading has to clear, and this ceiling is the only thing between
# that setting and mail sent in the user's name on the strength of phrase counts.
_RULE_CONFIDENCE_CEILING = 0.8


def _rule_based(subject: str, body: str) -> Classification:
    """The keyless / all-providers-down path.

    This is not a rare path. In production every LLM provider rate-limited at
    once for days, and *this function* was the classifier — so "returns UNKNOWN
    unless two strong phrases match" meant almost every real recruiter email was
    recorded as unclassifiable and never answered.

    It now grades rather than gates: one hint is enough to call it outreach, and
    more hints buy more confidence, up to a ceiling that still cannot auto-send.
    The candidate gets a draft to approve instead of silence.

    The two hint lists are **weighed against each other** rather than one
    short-circuiting the other. Marketing used to be checked first and win on a
    single phrase, which meant one line of platform boilerplate outvoted every
    job word in the message — the same shape of bug as the unsubscribe-footer
    filter, and with the same effect: a real recruiter's email discarded on
    words the recruiter did not write.

    Marketing has to *outweigh*, not merely appear. A sales blast says "shop
    now" and "sale ends" and "40% off" and, at most, brushes one recruiter
    phrase; an agency email says several recruiter things and at most brushes
    one marketing one. A tie goes to drafting, which is the bias this whole
    module is built on: a false positive costs the candidate a draft they
    delete, a false negative costs them a job they never heard about.
    """
    haystack = f"{subject or ''}\n{strip_preheader(strip_footer(body))}".lower()

    hits = sum(1 for hint in _RECRUITER_HINTS if hint in haystack)
    marketing = sum(1 for phrase in _NOT_RECRUITER_HINTS if phrase in haystack)

    if marketing > hits:
        return Classification(
            kind=RecruiterEmailKind.NOT_RECRUITER,
            confidence=0.55,
            classified_by=BY_FALLBACK,
            reason="Reads as bulk marketing rather than a person (no model available).",
        )

    if hits >= 1:
        # 1 hint -> 0.4, 2 -> 0.5, 3 -> 0.6, 4 -> 0.7, 5+ -> 0.8. See
        # _RULE_CONFIDENCE_CEILING for what the top of that scale can and
        # cannot reach.
        confidence = min(_RULE_CONFIDENCE_CEILING, 0.3 + 0.1 * hits)
        return Classification(
            kind=RecruiterEmailKind.RECRUITER_OUTREACH,
            confidence=round(confidence, 2),
            classified_by=BY_FALLBACK,
            reason=(
                f"Matched {hits} recruiter phrase{'s' if hits > 1 else ''} "
                "(no model available) — drafted for you to check."
            ),
        )

    return Classification(
        kind=RecruiterEmailKind.UNKNOWN,
        confidence=0.0,
        classified_by=BY_FALLBACK,
        reason="No model available and the text was inconclusive.",
    )


def classify(
    *,
    from_address: str,
    subject: str | None,
    body: str | None,
    reply_to: str | None = None,
) -> Classification:
    """Classify one inbound message and pull the opportunity out of it."""
    subject = subject or ""
    body = body or ""

    prefiltered = _prefilter(from_address, subject, body, reply_to)
    if prefiltered is not None:
        return prefiltered

    if not body.strip() and not subject.strip():
        return Classification(
            kind=RecruiterEmailKind.UNKNOWN,
            confidence=0.0,
            classified_by=BY_EMPTY,
            reason="Message had no readable content.",
        )

    try:
        completion = chat_completion_detailed(
            [
                {"role": "system", "content": untrusted.guarded(_SYSTEM_PROMPT)},
                {
                    "role": "user",
                    # Headers and body alike: the From line is as much a
                    # stranger's writing as the message under it, and a display
                    # name is a perfectly good place to put a sentence aimed at
                    # the model.
                    "content": untrusted.fence(
                        f"From: {from_address}\n"
                        + (f"Reply-To: {reply_to}\n" if reply_to else "")
                        + f"Subject: {subject}\n\n"
                        f"{body[:MAX_BODY_CHARS]}",
                        label="inbound email",
                    ),
                },
            ],
            model=settings.recruiter_classifier_model,
            temperature=0.0,
            max_tokens=600,
        )
    except OpenRouterError as exc:
        logger.info("recruiter classification fell back to rules: %s", exc)
        return _rule_based(subject, body)

    data = extract_json_object(completion.text)
    if not data:
        # A provider answered; we just could not use it. Marked so the retry
        # sweep counts this as a turn taken rather than as the chain being down.
        logger.warning("recruiter classifier returned unparseable output")
        verdict = _rule_based(subject, body)
        verdict.model_answered = True
        return verdict

    raw_kind = str(data.get("kind") or "").strip().upper()
    if raw_kind not in _VALID_KINDS or raw_kind == RecruiterEmailKind.UNKNOWN.value:
        # A kind we don't recognise is not a licence to guess.
        return Classification(
            kind=RecruiterEmailKind.UNKNOWN,
            confidence=0.0,
            classified_by=f"{completion.provider}:{completion.model}",
            reason="Classifier returned an unrecognised kind.",
        )

    asks = data.get("asks")
    questions = (
        [_coerce_str(a, 300) for a in asks][:5] if isinstance(asks, list) else []
    )
    # Checked against the message before anything is allowed to act on them —
    # see `_grounded_asks` for why an invented question is not a cosmetic
    # error here.
    questions = _grounded_asks([q for q in questions if q], subject, body)

    return Classification(
        kind=RecruiterEmailKind(raw_kind),
        confidence=_coerce_confidence(data.get("confidence")),
        classified_by=f"{completion.provider}:{completion.model}",
        role_title=_coerce_str(data.get("role_title")),
        company=_coerce_str(data.get("company")),
        location=_coerce_str(data.get("location")),
        remote=_coerce_bool(data.get("remote")),
        salary_text=_coerce_str(data.get("salary_text")),
        seniority=_coerce_str(data.get("seniority"), 50),
        asks=questions,
        reason=_coerce_str(data.get("reason"), 500) or "",
    )


# Where a bulk-mail footer starts. Everything from here down is boilerplate the
# sender's platform appended, not words they chose, and matching an opt-out
# inside it is what made this filter discard almost every recruiter email that
# arrived (see ``looks_like_opt_out``).
_FOOTER_MARKERS = (
    "to unsubscribe",
    "if you no longer wish",
    "if you would like to unsubscribe",
    "you are receiving this",
    "you're receiving this",
    "you received this email because",
    "this email was sent to",
    "manage your preferences",
    "email preferences",
    "update your preferences",
    "opt out of these emails",
    "unsubscribe from this list",
    "click here to unsubscribe",
    "no longer want to receive",
    "sent you this email because",
)


def strip_footer(body: str | None) -> str:
    """*body* with the bulk-mail footer removed.

    Cuts at the earliest footer marker. A recruiter who actually wants us to stop
    says so in the part they typed, which is above the cut.
    """
    text = body or ""
    lowered = text.lower()
    cut = len(text)
    for marker in _FOOTER_MARKERS:
        found = lowered.find(marker)
        if found != -1:
            cut = min(cut, found)
    return text[:cut]


# An opt-out is a *person telling us to stop*, in the first person. A footer's
# "click here to unsubscribe" is the sender's mail platform talking, and a
# recruiter's own signature block routinely carries one.
#
# This is deliberately much narrower than the previous
# ``\b(unsubscribe|remove me|stop contacting)\b``, which matched that footer in
# every templated recruiter email and silently discarded 98% of the mailbox
# before anything read it.
_OPT_OUT_RE = re.compile(
    r"\b(?:"
    r"unsubscribe\s+me"
    r"|(?:please\s+)?(?:remove|delete|take)\s+me\s+(?:from|off)"
    r"|remove\s+my\s+(?:name|email|address)"
    r"|take\s+me\s+off"
    r"|opt\s+me\s+out"
    r"|stop\s+(?:contacting|emailing|messaging|sending)\s+me"
    r"|(?:do\s+not|don't|dont)\s+(?:contact|email)\s+me"
    r"|no\s+longer\s+(?:wish|want)\s+to\s+(?:be\s+contacted|hear\s+from)"
    r")\b",
    re.I,
)

# A bare "unsubscribe" is only an opt-out when it is the whole message — someone
# replying with that one word means it, and nobody's job offer has it as a
# subject line.
_BARE_OPT_OUT_RE = re.compile(r"^\W*(unsubscribe|remove|stop)\W*$", re.I)


def looks_like_opt_out(subject: str | None, body: str | None) -> bool:
    """True when an inbound message is asking us to stop, not offering a job.

    Only the words the sender typed count: the footer is stripped first, and the
    phrasing has to be a first-person request. Getting this wrong in the
    permissive direction is expensive — a false positive drops a real job offer
    on the floor without ever classifying it — so the bar is an explicit ask.
    """
    typed_body = strip_footer(body)
    subject_text = (subject or "").strip()

    if _BARE_OPT_OUT_RE.match(subject_text) or _BARE_OPT_OUT_RE.match(
        typed_body.strip()
    ):
        return True
    return bool(_OPT_OUT_RE.search(f"{subject_text}\n{typed_body}"))


__all__ = [
    "BY_EMPTY",
    "BY_FALLBACK",
    "BY_PREFILTER",
    "Classification",
    "MAX_BODY_CHARS",
    "classify",
    "is_degraded",
    "looks_like_opt_out",
    "strip_footer",
    "strip_preheader",
]
