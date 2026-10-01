"""Content-level spam risk — what the *words* do to a personal mailbox.

:mod:`app.services.reputation_service` governs how *much* is sent and
:mod:`app.services.bounce_service` reacts to what came back. Neither one reads
the message. That left the one lever a cold-outreach sender actually controls
unattended: a filter's first judgement is on the content, and it is made before
any volume signal exists.

The stakes here are unusual, and they are why this module is not optional
polish. TalentPing sends from the candidate's **own Gmail**. A message that
trips a filter does not cost a campaign its open rate — it teaches Gmail
something about a mailbox the user needs for the rest of their life. There is no
sending-domain to burn and re-provision.

What is scored
--------------
The signals a receiving filter is cheap enough to actually run inline, and that
a job-search email has no legitimate reason to trip:

* **Marketing vocabulary** — "act now", "limited time", "risk-free". A person
  writing to a recruiter about a role never reaches for these. Their presence
  means the composer drifted into sales register, which is exactly the register
  bulk filters are tuned on.
* **Shouting** — an all-caps subject, or runs of ``!``/``$``. The oldest
  heuristic there is, still weighted heavily by every filter, and free to avoid.
* **Link load** — a cold first-contact email carrying five URLs looks like a
  campaign whatever the URLs are. Two (a portfolio and a LinkedIn) is normal.
* **Obfuscation** — ``F R E E``, ``V1agra``-style character substitution in a
  spam term. Innocent mail does not do this; its presence is close to proof the
  text came from somewhere it should not have.
* **Bare brevity** — a body under ``_MIN_BODY_CHARS`` with a link in it is the
  shape of a drive-by, and it is also a composer failure worth catching.

Deliberately *not* scored: the unsubscribe footer, tracking pixels, and the
List-Unsubscribe headers. Those are added downstream by
:mod:`app.services.unsubscribe` and the sender, they are the same on every
message, and scoring them would put a constant in every total — which moves the
threshold and tells the user nothing.

A risk score, not a verdict
---------------------------
Same posture as :mod:`app.services.ghost_job`, for the same reason. Scoring is
additive, every contribution carries the sentence that earned it, and those
sentences are what reach the user. No message is ever discarded here: past
``BLOCK_THRESHOLD`` an email that would have auto-sent becomes a ``DRAFT``
instead, which is the identical treatment :mod:`app.services.send_policy` gives
a paused account. The work is preserved, the user can read it and send it by
hand, and the only thing withheld is the right to skip a human.

That is what makes it safe to keep the bar low enough to be useful: the cost of
a false positive is one email the user has to click, and the cost of a false
negative is a permanent mark on their personal mailbox.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Sales register. Each phrase is one a recruiter-facing email has no reason to
# contain, which is what keeps the list short — a word that is merely common in
# spam ("offer", "opportunity", "apply") is also common in a real job email, and
# scoring it would tax every honest message equally.
#
# "work from home" was on this list and failed that test in the plainest way
# available: it is how :data:`jd_parser._REMOTE_MARKERS` and
# :data:`salary_service._REMOTE_WORDS` both recognise a remote posting. The
# product reads the phrase as naming a work arrangement in two places and
# charged the candidate 22 points for writing it in a third, so "I'm looking
# for work from home roles" — a sentence this product exists to help people
# send — plus the three links of an ordinary signature reached
# BLOCK_THRESHOLD exactly, and the review queue told them their email read as
# marketing copy.
#
# Nothing is lost by dropping it. What makes the spam version of that sentence
# spam is the earnings claim beside it, and ``earn $X per week`` scores that on
# its own.
_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"\bact now\b", "act now"),
        (r"\blimited time\b", "limited time"),
        (r"\bdon'?t miss (?:out|this)\b", "don't miss out"),
        (r"\bonce[- ]in[- ]a[- ]lifetime\b", "once-in-a-lifetime"),
        (r"\brisk[- ]free\b", "risk-free"),
        (r"\bno (?:strings|obligation|catch)\b", "no obligation"),
        (r"\b100% (?:free|guaranteed|satisfied)\b", "100% guaranteed"),
        (r"\bmoney[- ]back\b", "money-back"),
        (r"\bguaranteed? (?:income|salary|placement|job|results?)\b",
         "guaranteed results"),
        (r"\bearn \$?\d+\s*(?:k|,\d{3})?\s*(?:per|a|/)\s*(?:day|week|month)\b",
         "earn $X per week"),
        (r"\bmake money (?:fast|online|quickly)\b", "make money fast"),
        (r"\bclick here\b", "click here"),
        (r"\border now\b", "order now"),
        (r"\bspecial promotion\b", "special promotion"),
        (r"\bthis is not spam\b", "this is not spam"),
        (r"\bcall now\b", "call now"),
        (r"\bwhile supplies last\b", "while supplies last"),
        (r"\burgent(?:ly)? (?:reply|respond|action) (?:required|needed)\b",
         "urgent action required"),
    )
)

# Spaced-out or digit-substituted spam words — "F R E E", "M0NEY". Matching the
# technique rather than any particular word: the technique itself is the signal,
# because nothing written in good faith is obfuscated.
_SPACED_OUT = re.compile(
    r"\b(?:[A-Za-z]\s+){3,}[A-Za-z]\b(?!\s*[A-Za-z])"
)
#
# Every alternative below requires at least one *substituted* character. The
# character classes used to admit the un-substituted letter too — ``fr[3e]{2}``
# matches "free", ``c[a4]sh`` matches "cash" — so the plain English spellings
# all tripped it. "Are you free for a quick call?" is close to the commonest
# sentence in this product's outbox, and it scored a flat 40: exactly
# BLOCK_THRESHOLD, because obfuscation is weighted to stand alone. The email was
# held back from auto-send and the user was told, in the review queue, that
# their message contained "text disguised to slip past a filter". "money",
# "cash" and "click" did the same to anyone writing to a fintech.
#
# "viagra" keeps both spellings: it is the one word here with no innocent
# reading in a job email, so there is nothing for the substitution requirement
# to protect.
_LEETSPEAK = re.compile(
    r"\b(?:"
    r"fr(?:3[3e]|e3)"  # fr3e, fre3, fr33 — never "free"
    r"|m(?:0n[3e]y|on3y)"  # m0ney, mon3y, m0n3y — never "money"
    r"|c4sh"  # never "cash"
    r"|cl1ck"  # never "click"
    r"|v[i1]agr[a4]"
    r")\b",
    re.IGNORECASE,
)

# One match per link. The two alternatives used to be bare prefixes, and
# ``https://www.jane.dev`` satisfies both — so the commonest shape a real link
# takes was counted twice. A portfolio and a LinkedIn profile, the exact two the
# allowance below exists to permit, read as four links: an 18-point charge on an
# ordinary email, and a sentence in the review queue telling the user they wrote
# something they didn't. The scheme alternative comes first so it wins at the
# same position, and ``\S+`` consumes the rest of the URL so nothing inside it
# can match again.
_URL = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)
_EXCLAMATIONS = re.compile(r"!{2,}")
_CURRENCY_RUN = re.compile(r"\${2,}")
# Shouting is a *run* of capitals, not a word that happens to be capitalised.
#
# The old rule counted all-caps words of four letters or more anywhere in the
# text and called two of them shouting, on the stated assumption that an
# engineer's acronyms are shorter than that — "API, SQL, AWS, iOS". Half of
# them are not: REST, GRPC, SAML, JSON, HTML, SOAP, SDLC, GDPR, HIPAA, KAFKA
# and REDIS are all four letters or more, and two of them in one subject line
# is not a spammer, it is the job. "Backend Engineer — REST and GRPC at scale"
# scored 25, and with the three links an engineer's signature carries it
# reached BLOCK_THRESHOLD: the email was held back from auto-send and the user
# was told, in the review queue, that their subject was shouting in capitals.
#
# What separates the two is adjacency, not length. Acronyms arrive punctuated
# and surrounded by ordinary prose — "SAML/OAUTH integration", "AWS, SQL, GCP",
# "REST and GRPC" — because they are nouns inside a sentence. Shouting is
# contiguous: "LIMITED TIME OFFER", "URGENT ACTION REQUIRED", "MAKE MONEY
# FAST". So the signal is three or more all-caps words in a row with nothing
# but spaces between them. Two in a row is not enough, because "REST API" and
# "GRPC API" are things people write.
#
_SHOUTED_RUN = re.compile(r"\b[A-Z]{2,}(?:[ \t]+[A-Z]{2,}){2,}\b")

# The same question asked of a subject that is *entirely* capitals, where the
# run test above has nothing to compare against — every word is a candidate.
#
# `subject.isupper()` was the whole test, and it is true of a subject that
# contains no lowercase letter at all rather than one that is shouting. A
# subject line is where an engineer's acronyms are densest, because it is where
# the stack goes: "SDE II - AWS, GCP, SQL", "ETL / SQL / AWS" and even "RE: SDE
# II" have no lowercase in them and none of them is raising its voice. Each
# scored a flat 25, and with the three links an engineer's signature carries
# that reaches BLOCK_THRESHOLD: the email was held back from auto-send and the
# user was told, in the review queue, that their subject was shouting in
# capitals.
#
# What separates the two is the same thing it is in `_SHOUTED_RUN` —
# adjacency — plus the observation that acronyms are short. A stack list is
# punctuated ("AWS, GCP, SQL", "ETL / SQL / AWS") and its words are two to four
# letters; shouting is words running together, and at least one of them is the
# length of an actual word. "LIMITED TIME OFFER", "FREE MONEY" and "URGENT
# ACTION REQUIRED" all clear that; "KAFKA / REDIS / MYSQL" does not, which a
# length rule on its own could not manage.
#
# The cost is a one-word all-caps subject, which no longer scores on its own.
# It is a word rather than a claim — the sentences worth catching are in
# `_PHRASES`, which reads the subject too.
_SHOUTED_PAIR = re.compile(
    r"\b(?:[A-Z]{4,}[ \t]+[A-Z]{2,}|[A-Z]{2,}[ \t]+[A-Z]{4,})\b"
)

# Weights. Sized so no single mechanical signal blocks on its own — an
# enthusiastic "!!" is not a spam email — while two independent ones do. Only
# obfuscation is heavy enough to stand alone, because unlike the others it has
# no innocent reading.
_PHRASE_WEIGHT = 22
_OBFUSCATION_WEIGHT = 40
_SHOUTING_SUBJECT_WEIGHT = 25
_SHOUTING_BODY_WEIGHT = 12
_PUNCTUATION_WEIGHT = 14
_LINK_WEIGHT = 18
_THIN_BODY_WEIGHT = 20

# Links a genuine first-contact email might carry: a portfolio and a profile.
_LINK_ALLOWANCE = 2
# Under this, with a link, the message is a pointer rather than a note.
_MIN_BODY_CHARS = 180

# At or above this, an email may not skip human review. Two independent
# mechanical signals, or one phrase plus anything, reaches it.
BLOCK_THRESHOLD = 40


@dataclass(frozen=True)
class SpamAssessment:
    """What the content will look like to a filter, and why."""

    risk: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def should_review(self) -> bool:
        """True when this must not auto-send. Never means "discard"."""
        return self.risk >= BLOCK_THRESHOLD

    @property
    def summary(self) -> str | None:
        """One line for the review queue, or None when the content is clean."""
        if not self.reasons:
            return None
        return "; ".join(self.reasons)


def screen(
    subject: str | None, body: str | None, *, auto_send: bool
) -> tuple[bool, SpamAssessment]:
    """``(may still skip review, what the content scored)``.

    The one place the "risky content does not auto-send" rule is applied, rather
    than the three places that each had to remember it. Only the campaign
    pipeline did: :mod:`app.services.auto_apply_service` and
    :mod:`app.services.follow_up_service` compose outreach the same way, queue it
    the same way, and never asked. Auto-apply is the *main* send path in V2 — the
    one that runs unattended off a saved search — so the guard covered the
    pipeline a user launches by hand and left the one that sends while they are
    asleep unguarded, which is exactly backwards.

    Follow-ups matter for a second reason. They are the messages a filter sees
    two, three and four of from the same sender to the same recipient, so a
    phrase that scores once scores repeatedly, against a mailbox whose warm-up
    ramp is still being established.

    Returns the flag rather than mutating anything: the callers each hold their
    own notion of a "held" message — a status, a note, a campaign log line — and
    this has no business picking one.
    """
    content = assess(subject, body)
    return (auto_send and not content.should_review), content


def _shouted(text: str) -> bool:
    """Whether *text* contains a run of capitals rather than an acronym.

    See :data:`_SHOUTED_RUN`. One emphasised word is how people write emphasis
    and filters do not chase it either; two capitalised words in a row are as
    likely to be "REST API" as anything else.
    """
    return _SHOUTED_RUN.search(text) is not None


def _all_caps_shouting(subject: str) -> bool:
    """Whether a subject with no lowercase in it is shouting or listing a stack.

    See :data:`_SHOUTED_PAIR`. Both halves are needed: the pair test alone
    would charge "We need SENIOR ENGINEERS" — emphasis inside a sentence, which
    is not what this scores — and `isupper` alone charged every acronym.
    """
    return subject.isupper() and _SHOUTED_PAIR.search(subject) is not None


def assess(subject: str | None, body: str | None) -> SpamAssessment:
    """Score one composed message for how a spam filter will read it.

    Takes the text rather than an ``Email`` row so the composer can screen a
    message *before* it is stored, and the review queue can screen one the user
    has since edited by hand. Neither caller has the same object.
    """
    subject = subject or ""
    body = body or ""
    risk = 0
    reasons: list[str] = []

    hits = [label for pattern, label in _PHRASES if pattern.search(f"{subject}\n{body}")]
    if hits:
        # Charged once however many phrases matched, then scaled by how many —
        # three marketing phrases is meaningfully worse than one, but six is not
        # six times worse than one, and a long body should not accumulate risk
        # simply for being long.
        risk += _PHRASE_WEIGHT + _PHRASE_WEIGHT // 2 * min(len(hits) - 1, 2)
        shown = ", ".join(f"“{h}”" for h in hits[:3])
        more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""
        reasons.append(f"Reads as marketing copy: {shown}{more}")

    if _LEETSPEAK.search(f"{subject}\n{body}") or _SPACED_OUT.search(subject):
        risk += _OBFUSCATION_WEIGHT
        reasons.append("Contains text disguised to slip past a filter")

    if subject and (_shouted(subject) or _all_caps_shouting(subject)):
        risk += _SHOUTING_SUBJECT_WEIGHT
        reasons.append("The subject line is shouting in capitals")
    elif _shouted(body):
        risk += _SHOUTING_BODY_WEIGHT
        reasons.append("The body shouts in capitals")

    if _EXCLAMATIONS.search(f"{subject}\n{body}") or _CURRENCY_RUN.search(body):
        risk += _PUNCTUATION_WEIGHT
        reasons.append("Uses repeated exclamation marks or currency symbols")

    links = len(_URL.findall(body))
    if links > _LINK_ALLOWANCE:
        risk += _LINK_WEIGHT
        reasons.append(
            f"Carries {links} links; a first email rarely needs more than "
            f"{_LINK_ALLOWANCE}"
        )

    if links and len(body.strip()) < _MIN_BODY_CHARS:
        risk += _THIN_BODY_WEIGHT
        reasons.append("Almost nothing but a link")

    return SpamAssessment(risk=min(risk, 100), reasons=reasons)


__all__ = ["BLOCK_THRESHOLD", "SpamAssessment", "assess", "screen"]
