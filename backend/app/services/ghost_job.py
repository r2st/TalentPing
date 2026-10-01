"""Ghost-job detection — is this posting a real, fillable opening?

Roughly a fifth of scraped postings are ghosts: roles that are expired, were
never budgeted, or exist only to farm a resume pipeline. Outreach against one
costs the candidate a send from a hard daily limit, a slot of the warm-up
budget, and — the expensive one — some of their belief that the product works,
because a ghost never replies and a run of ghosts reads exactly like a broken
sender.

Nothing here needs a new data source. Every signal is read off columns the
fetchers already fill:

``posted_at``
    Age. A role open 120 days is not being filled at the rate it is advertised.

``repost_count``
    The same fingerprint resurfacing across scans with a *fresher* ``posted_at``.
    One repost is ordinary hiring. Four is a requisition that never closes.

``description``
    Evergreen language ("general application", "join our talent community") and
    requirement lists too broad for one person to satisfy.

``source`` / ``source_urls``
    Breadth without a home. A role on four aggregators and on no company board
    is far weaker evidence of an opening than a role on the company's own
    Greenhouse — the board is the employer speaking directly, and it comes down
    when the role closes.

``location``
    One req naming many cities. A posting open in "multiple locations" is a
    requisition covering a hiring plan, not a seat someone vacated.

``salary_min`` / ``salary_max``
    A band so wide it does not describe a level. A real range is a level's
    range; $60k–$260k is three levels stapled together, which is what a
    pipeline req looks like when it has to cover whoever answers.

Deliberately a **risk score, not a verdict.** The scoring is additive, every
contribution carries the sentence that earned it, and the sentences go to the
candidate. A silently-dropped real job is the one failure mode worth avoiding,
so the default suppression bar (``ghost_default_max_risk``) sits high enough
that a posting needs several independent signals to reach it — age alone can
never get there.

There is exactly one exception, and it is not a heuristic: a posting that
*states* it has been filled or closed is not a risk estimate, it is the
employer saying the thing this module tries to infer. That signal alone clears
the suppression bar, and it is the only one that does — see ``_CLOSED_WEIGHT``.

The board signal is the only *negative* one. A posting fetched from an
employer's own ATS board is live by construction, and that outweighs an
inherited age or an unlucky phrase.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from app.services.ats_boards import PLATFORMS as _BOARD_PLATFORMS

# Risk at or above this reads as a ghost; at or above ``STALE_AT`` as stale.
GHOST_AT = 70
STALE_AT = 40

LEVEL_OK = "ok"
LEVEL_STALE = "stale"
LEVEL_GHOST = "ghost"

_BOARD_SOURCES = frozenset(_BOARD_PLATFORMS)

# Phrases that say outright that there is no specific opening behind the post.
# The highest-precision signal available and the only one that can reach the
# suppression bar with one other; kept to phrases with no innocent reading —
# "we are hiring" is not here, because every real posting says it.
_EVERGREEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"\bgeneral application\b", "asks for a general application"),
        (r"\bopen application\b", "asks for an open application"),
        (r"\bspeculative application\b", "asks for a speculative application"),
        # Guarded against the words that turn "talent pool" into the *subject*
        # of the job rather than the fate of the applicant. A recruiting-tech
        # company writes "our talent pipeline product" in a JD for a real seat,
        # and an HR org writes "our talent community newsletter"; neither is a
        # posting recruiting into a pool.
        (r"\btalent (?:pool|community|network|pipeline)\b"
         r"(?!\s+(?:product|platform|tool|software|newsletter|team|strategy"
         r"|roadmap|management|system|features?))",
         "recruits into a talent pool"),
        # "Join our talent acquisition team" is a recruiter opening — a real
        # seat, and one this phrase would otherwise suppress every time.
        (r"\bjoin our talent\b(?!\s+(?:acquisition|team|ops|operations))",
         "recruits into a talent pool"),
        (r"\bfuture (?:opportunities|openings|roles|vacancies)\b",
         "advertises future openings rather than a current one"),
        (r"\bexpression of interest\b", "collects expressions of interest"),
        (r"\bpipeline (?:req|requisition)\b", "is a pipeline requisition"),
        (r"\bevergreen (?:req|requisition|role|posting)\b", "is an evergreen requisition"),
        # "always hiring" and "always accepting" have no reading but this one.
        # "always looking for" does: half the postings on the internet say the
        # company is always looking for ways to improve something.
        (r"\balways (?:hiring|accepting)\b"
         r"|\balways looking for\b(?!\s+(?:ways|opportunities|feedback|improvements?))",
         "says it is always hiring"),
        (r"\bno specific (?:opening|role|vacancy)\b", "names no specific opening"),
        (r"\bwe hire on a rolling basis\b", "hires on a rolling basis"),
    )
)

# What happens to your details *after* this application — the clause a real
# posting closes with, and the last big source of false positives on the
# evergreen signal.
#
# "If you are not selected, we will keep your CV on file for future
# opportunities" is a live req with an open seat. The evergreen phrase is
# genuinely there, but it describes the retention of your data, not the nature
# of the posting — and at 45 points, unguarded, it needs only one age band to
# suppress a real job.
#
# Markers rather than whole sentences, because the clause is written a hundred
# ways and only ever means one thing.
_RETENTION = re.compile(
    r"\bnot (?:selected|successful|shortlisted|progress(?:ed|ing)?)\b"
    r"|\bunsuccessful\b"
    r"|\bon file\b"
    r"|\b(?:retain|keep|hold|store) (?:your|you)\b"
    r"|\bmay (?:also )?(?:be added|be considered|be kept|be retained"
    r"|contact|reach out to|consider|add|keep|retain)\b"
    r"|\bwe(?:'ll| will) (?:also )?(?:consider|keep|contact) you\b"
    r"|\byour (?:cv|resume|details|profile|data|application) (?:will|may)\b",
    re.IGNORECASE,
)

# How far either side of a phrase to look for the clause it sits in. A run of
# text this long without punctuation is not one clause, and treating it as one
# would let a retention marker at the far end excuse an evergreen phrase it has
# nothing to do with.
_CLAUSE_REACH = 200
_CLAUSE_BREAK = re.compile(r"[.!?;\n]")

# The employer saying outright that this is over. Not an inference — the only
# signal here that reports rather than estimates, which is why it is the only
# one weighted past the suppression bar.
#
# Every phrase is anchored to *this* posting, because the unanchored versions
# have innocent readings that appear in live JDs: "no longer accepting
# applications by email — use the form below" is a real sentence on a real
# opening, so the bare phrase carries a lookahead for the words that turn it
# into a routing instruction.
#
# Two guards run on every phrase here, and both exist because this weight
# suppresses a posting on its own — a false positive is a real job silently
# dropped, which the module docstring names as the one failure mode worth
# avoiding. Live JDs really do contain these sentences:
#
# ``_NOT_CONDITIONAL``
#     "We will notify all candidates *once* the position has been filled" is
#     boilerplate on an open req describing what happens later. The closure is
#     hypothetical, and the conjunction is the only thing that says so.
# ``_NOT_QUALIFIED``
#     "This position is closed *to* candidates requiring sponsorship" is a
#     restriction on who may apply, not a statement that nobody may. Same for
#     an internal-only window.
#
# Fixed-width lookbehinds, one per conjunction, because Python's ``re`` will not
# take a variable-length one. Stacked negatives read badly but keep the guard
# inside the pattern, which is where the existing "no longer accepting
# applications" guard already lives.
_NOT_CONDITIONAL = (
    r"(?<!once )(?<!when )(?<!after )(?<!until )(?<!unless )(?<!if )(?<!before )"
)
_NOT_QUALIFIED = r"(?!\s+(?:to|for)\b)"

_CLOSED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (_NOT_CONDITIONAL
         + r"\bth(?:is|e) (?:position|role|job|vacancy|req|opening) has been filled\b",
         "says the role has already been filled"),
        (_NOT_CONDITIONAL
         + r"\bth(?:is|e) (?:position|role|job|vacancy|opening) is (?:now )?closed\b"
         + _NOT_QUALIFIED,
         "says the role is closed"),
        (r"\bth(?:is|e) (?:position|role|job|vacancy|opening) is no longer available\b",
         "says the role is no longer available"),
        (r"\bth(?:is|e) (?:job )?posting has expired\b", "says the posting has expired"),
        # No qualifier guard on this one: "applications are closed *for* this
        # role" is the sentence itself, not a restriction on it.
        (_NOT_CONDITIONAL + r"\bapplications (?:for this[\w ]{0,20})?are (?:now )?closed\b",
         "says applications are closed"),
        (r"\bno longer accepting applications\b(?!\s+(?:via|through|by|at|on|from))",
         "says it is no longer accepting applications"),
        (_NOT_CONDITIONAL + r"\bwe have filled th(?:is|e) (?:position|role|vacancy)\b",
         "says the role has already been filled"),
    )
)

# Hiring by the dozen. A single posting recruiting a cohort is a pipeline, and
# whoever answers it is one of many — which is the candidate-facing difference
# a ghost score is trying to capture even when the req is technically real.
_VOLUME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"\bimmediate joiners?\b", "asks for immediate joiners"),
        (r"\bwalk[- ]?in (?:interview|drive)\b", "advertises a walk-in interview"),
        (r"\b(?:bulk|mass|volume) hiring\b", "describes itself as bulk hiring"),
        (r"\b(?:multiple|several|numerous) (?:openings|positions|vacancies|seats)\b",
         "advertises multiple openings on one posting"),
        (r"\b(?:openings|positions|vacancies)\s*:?\s*(\d{2,})\b",
         "advertises {n} openings on one posting"),
        (r"\bhiring\s+(\d{2,})\+?\s+(?:candidates|people|engineers|developers)\b",
         "is hiring {n} people at once"),
    )
)

# A req covering a hiring plan across cities rather than one seat.
#
# Split by where it is read, because the two fields carry different words. In
# the *location field* a bare "Nationwide" is the board saying exactly this, and
# it is unambiguous — nothing else is in that field. In the *description* the
# same word is usually a company describing itself: "Acme is a nationwide
# retailer" says nothing about how many seats this req covers, and scoring it
# taxes every posting by a big employer.
#
# So the description is matched only on phrases that name the posting's own
# spread, and never on a bare adjective.
_MULTI_LOCATION_FIELD = re.compile(
    r"\b(?:multiple|various|several|all|many) locations\b"
    r"|\blocations? (?:across|throughout|nationwide)\b"
    r"|\bnationwide\b"
    r"|\b(?:multiple|various) (?:cities|sites|offices)\b",
    re.IGNORECASE,
)
_MULTI_LOCATION_TEXT = re.compile(
    r"\b(?:multiple|various|several|many) locations\b"
    r"|\blocations? (?:across|throughout)\b"
    r"|\b(?:multiple|various) (?:cities|sites|offices)\b",
    re.IGNORECASE,
)

# "Our client is a leading…" — the posting is an agency's, not an employer's.
# Low weight on its own: plenty of agency postings are real mandates. It earns
# its place by stacking, because an agency posting with no named employer and
# no company board is the classic resume-farm shape.
#
# Singular only. "our clients" is what a consultancy calls the people it builds
# for — a real employer describing a real job — and the word boundary after
# "client" is what keeps the plural out.
#
# The bare "our client" also needs a trailing guard, because the same two words
# open a compound noun that means the opposite: "you will work directly with our
# client teams" is an employer describing the job's actual surface area. A
# posting that is really an agency's says "our client is…", never "our client
# teams".
_AGENCY = re.compile(
    r"\bon behalf of (?:our|a) client\b"
    r"|\bour client\b(?!\s+(?:teams?|work|base|portfolios?|projects?|sites?"
    r"|engagements?|accounts?|partners?|relationships?|services?|platforms?"
    r"|success|experience|onboarding|delivery|facing))"
    r"|\bconfidential client\b"
    r"|\ba (?:leading|major|well[- ]known) client\b",
    re.IGNORECASE,
)

# A band this wide is not a level's range. Real bands land near 1.3-1.6x;
# 3x means the posting is covering whoever turns up, which is the pipeline-req
# shape again. The floor keeps hourly and stipend figures out — "$20-$90" is a
# 4.5x span and tells us nothing.
_WIDE_BAND_RATIO = 3.0
_BAND_FLOOR = 30_000

# "5-15 years", "3 to 12+ years" — a span this wide is not one job.
_YEARS_RANGE = re.compile(
    r"(\d{1,2})\s*(?:\+)?\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*years?", re.IGNORECASE
)
# Bullet markers, counted in text that no longer has line breaks. Every
# provider builds its ``RawJob`` through ``_clean``, which collapses `\s+` to a
# single space, so a line-anchored pattern would only ever match the first
# bullet of a posting and this signal would silently never fire.
#
# A marker is therefore recognised mid-string, which costs precision back for
# the ambiguous glyphs: `-` and `*` count only when whitespace sits on both
# sides, so "e-commerce" and "3-5 years" are not bullets. The unambiguous
# glyphs need no such guard.
_BULLET = re.compile(r"(?:^|\s)(?:[•·‣▪]|[-*]|\d{1,2}[.)])\s")

# Role families that do not belong to one person. A title naming three of them
# is a req covering a team, not a seat.
#
# *Craft* families only. "management" was one of these rows — ("manager",
# "director", "head of", "lead") — and it is not a family in the sense this
# rule needs. Every word in it is a scope modifier: it says how much of a
# craft the seat owns, never which craft, so it attaches to one of the rows
# below rather than standing beside them. A managerial title therefore only
# had to name *two* real families to trip a rule written for three, and the
# titles that do that are among the commonest in the market: "Lead Product
# Manager, Growth" (product + marketing + the modifier), "Head of Growth
# Engineering" (engineering + marketing + the modifier), "Technical Product
# Manager - Growth". Each was charged ``_BROAD_WEIGHT`` and told on its own
# card that "the title covers three unrelated job families", which is the same
# false sentence about the same real seat that the ``\b``-anchoring note below
# was written to stop — arrived at from the other direction, because the
# substring was never the only way to over-count.
#
# The product already treats these words this way everywhere else:
# ``role_expansion._SENIORITY_RE`` strips "lead", "head of", "chief" and "vp
# of" before looking a title up, on the stated grounds that seniority "has no
# bearing on what a job is *called*". It has no bearing on how many jobs a
# title covers either.
#
# What it costs is a department req that spells one of its seats with a bare
# management word and only two crafts — "Manager / Developer / Designer". That
# posting still has to name three crafts to be caught, and a listing long
# enough to be a department almost always does; the ``_BULLET`` count below
# catches the rest.
_ROLE_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("engineering", ("engineer", "developer", "programmer", "sre")),
    ("data", ("data scientist", "analyst", "statistician")),
    ("design", ("designer", "ux", "ui")),
    ("product", ("product owner", "product manager")),
    ("sales", ("sales", "account executive", "business development")),
    ("support", ("support", "success", "helpdesk")),
    ("marketing", ("marketing", "growth", "seo")),
)

# Anchored at the *start* of a word, not matched anywhere in the string.
#
# ``"ux" in title`` is true of "Linux". So "Linux Support Engineer" — an
# ordinary seat that one ordinary person fills — counted as design *and* support
# *and* engineering, tripped the three-family rule, and was told on its own card
# that "the title covers three unrelated job families". "Redux Developer" had
# the same problem. That is a real job scored toward suppression on a substring,
# which the module docstring names as the one failure worth avoiding.
#
# Leading boundary only, with a free tail: a title says "Engineering Manager"
# and "Designers" as readily as "engineer" and "designer", and requiring a
# trailing boundary would lose both. The leading ``\b`` is what does the work —
# there is none before the "ux" in "Linux" — and it is what makes a two-letter
# token safe to keep in the list. "UI/UX Lead" still matches on both, since
# ``/`` is a boundary.
_FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile("|".join(rf"\b{re.escape(word)}\w*" for word in words)))
    for name, words in _ROLE_FAMILIES
)

# --- Weights -------------------------------------------------------------- #
# Additive, and chosen so no single signal reaches ``GHOST_AT`` alone. Age is
# the noisiest input (plenty of boards never publish a real date and plenty of
# real roles stay open a quarter), so it is capped below the bar on purpose.
_AGE_BANDS: tuple[tuple[int, int, str], ...] = (
    (120, 42, "posted over 4 months ago"),
    (75, 32, "posted over 2 months ago"),
    (45, 20, "posted over 6 weeks ago"),
    (21, 8, "posted over 3 weeks ago"),
)
_REPOST_BANDS: tuple[tuple[int, int, str], ...] = (
    (4, 30, "reposted {n} times since we first saw it"),
    (3, 22, "reposted {n} times since we first saw it"),
    (2, 12, "reposted twice since we first saw it"),
)
_EVERGREEN_WEIGHT = 45
_BROAD_WEIGHT = 15
_AGGREGATOR_ONLY_WEIGHT = 12
_BOARD_CREDIT = -25
# Past the suppression bar on its own, and the only weight here that is. The
# posting is not being scored at this point — it is being read. See the module
# docstring; the phrases behind it are anchored precisely because this weight
# leaves no room for a false positive to be argued down by anything else.
_CLOSED_WEIGHT = 90
_VOLUME_WEIGHT = 20
_MULTI_LOCATION_WEIGHT = 12
_AGENCY_WEIGHT = 10
_WIDE_BAND_WEIGHT = 15
# Aggregator breadth only counts as a signal once the role is genuinely
# everywhere; two boards is normal syndication.
_AGGREGATOR_BREADTH = 3


class _PostingLike(Protocol):
    """The shape both a stored ``JobPosting`` and a freshly fetched job share.

    Declared read-only, as properties rather than attributes. A mutable
    protocol attribute is invariant, which would reject ``RawJob`` — it types
    ``source`` as ``str`` where a stored row allows ``str | None``, and that is
    a narrowing this function is happy to accept because it only ever reads.
    """

    @property
    def title(self) -> str | None: ...
    @property
    def description(self) -> str | None: ...
    @property
    def source(self) -> str | None: ...
    @property
    def posted_at(self) -> datetime | None: ...
    @property
    def location(self) -> str | None: ...


@dataclass(frozen=True)
class GhostAssessment:
    """How likely this posting is to be a ghost, and why."""

    risk: int
    level: str
    reasons: list[str] = field(default_factory=list)


def level_for(risk: int) -> str:
    if risk >= GHOST_AT:
        return LEVEL_GHOST
    if risk >= STALE_AT:
        return LEVEL_STALE
    return LEVEL_OK


def _age_days(posted_at: datetime | None, now: datetime) -> int | None:
    if posted_at is None:
        return None
    # Rows written before the column was tz-aware, and a few providers, hand
    # back naive datetimes. Treat them as UTC rather than crashing the scan.
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    return max(0, (now - posted_at).days)


def _clause_around(text: str, at: int) -> str:
    """The clause *at* sits in, bounded by punctuation and by ``_CLAUSE_REACH``."""
    lo = max(0, at - _CLAUSE_REACH)
    hi = min(len(text), at + _CLAUSE_REACH)
    breaks = [m.end() for m in _CLAUSE_BREAK.finditer(text, lo, at)]
    start = breaks[-1] if breaks else lo
    after = _CLAUSE_BREAK.search(text, at, hi)
    return text[start : after.start() if after else hi]


def _evergreen_reason(text: str) -> str | None:
    """An evergreen phrase, read in the clause that carries it.

    ``finditer`` rather than ``search``: a posting may say "talent pool" twice,
    once in its retention boilerplate and once as what it actually is, and the
    first occurrence is not allowed to speak for the second.
    """
    for pattern, label in _EVERGREEN_PATTERNS:
        for match in pattern.finditer(text):
            if not _RETENTION.search(_clause_around(text, match.start())):
                return label
    return None


def _closed_reason(text: str) -> str | None:
    """The employer saying this is over, in words that admit no other reading."""
    for pattern, label in _CLOSED_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _volume_reason(text: str) -> str | None:
    """Hiring a cohort off one posting."""
    for pattern, label in _VOLUME_PATTERNS:
        match = pattern.search(text)
        if match:
            # Only two of these patterns capture a count; the label knows which
            # by whether it has a placeholder, so an uncaptured match is not a
            # KeyError waiting for the first "bulk hiring" posting to arrive.
            count = match.group(1) if match.groups() else None
            return label.format(n=count) if count and "{n}" in label else label
    return None


def _wide_band_reason(salary_min: int | None, salary_max: int | None) -> str | None:
    """A range too wide to be one level's range."""
    if not salary_min or not salary_max or salary_min < _BAND_FLOOR:
        return None
    if salary_max / salary_min < _WIDE_BAND_RATIO:
        return None
    return (
        f"the salary range spans {salary_min:,}-{salary_max:,}, "
        "wider than one level"
    )


def _is_broad(title: str, description: str) -> str | None:
    """Requirements no one person satisfies — the classic pipeline-req tell."""
    for low, high in _YEARS_RANGE.findall(description):
        if int(high) - int(low) >= 8:
            return f"asks for {low}-{high} years of experience in one role"

    families = {name for name, pattern in _FAMILY_PATTERNS if pattern.search(title)}
    if len(families) >= 3:
        return "the title covers three unrelated job families"

    # A single posting listing this many bullets is a department, not a seat.
    if len(_BULLET.findall(description)) > 30:
        return "lists more requirements than one role could carry"
    return None


def assess(
    posting: _PostingLike,
    *,
    repost_count: int = 0,
    source_count: int = 1,
    salary_band: tuple[int | None, int | None] | None = None,
    now: datetime | None = None,
) -> GhostAssessment:
    """Score one posting for ghostliness.

    *source_count* is how many distinct boards carried this role — the length of
    ``source_urls`` for a stored row. *repost_count* is how many times the same
    role has resurfaced with a fresher date. *salary_band* is the parsed
    ``(min, max)``, passed in rather than read off the posting because the
    ingest path parses it from free text moments before calling this and a
    freshly fetched job has no parsed columns to read.

    Takes a structural protocol rather than a ``JobPosting`` so the ingest path
    can screen a posting *before* deciding to store it, which is the whole point
    of having the check: the cheapest ghost is the one that never reaches the
    feed.
    """
    now = now or datetime.now(UTC)
    risk = 0
    reasons: list[str] = []

    title = (posting.title or "").lower()
    description = posting.description or ""
    haystack = f"{title}\n{description}"
    # ``location`` joined the protocol after the fact, and a caller screening a
    # partial posting it assembled by hand should not start crashing for it.
    location = getattr(posting, "location", None) or ""

    closed = _closed_reason(haystack)
    if closed:
        risk += _CLOSED_WEIGHT
        reasons.append(closed.capitalize())

    evergreen = _evergreen_reason(haystack)
    if evergreen:
        risk += _EVERGREEN_WEIGHT
        reasons.append(evergreen.capitalize())

    volume = _volume_reason(haystack)
    if volume:
        risk += _VOLUME_WEIGHT
        reasons.append(volume.capitalize())

    # Two patterns, two fields — read the note on ``_MULTI_LOCATION_FIELD``.
    #
    # The field wins outright when it has anything in it. A board that says
    # "Berlin" has answered the question this signal asks, and a description
    # that goes on to mention the team's other offices is not arguing with it.
    # The description is consulted only when the field is empty, which is the
    # case it was always meant for: aggregators that flatten the field away.
    breadth = (
        _MULTI_LOCATION_FIELD.search(location)
        if location.strip()
        else _MULTI_LOCATION_TEXT.search(haystack)
    )
    if breadth:
        risk += _MULTI_LOCATION_WEIGHT
        reasons.append("Covers several locations rather than one seat")

    if _AGENCY.search(description):
        risk += _AGENCY_WEIGHT
        reasons.append("Posted by an agency on behalf of an unnamed client")

    wide_band = _wide_band_reason(*(salary_band or (None, None)))
    if wide_band:
        risk += _WIDE_BAND_WEIGHT
        reasons.append(wide_band.capitalize())

    age = _age_days(posting.posted_at, now)
    if age is not None:
        for threshold, weight, label in _AGE_BANDS:
            if age >= threshold:
                risk += weight
                # Capitalized like every other reason: these are read as a list
                # of sentences on the card, and one lowercase entry among them
                # looks like a rendering bug.
                reasons.append(f"{label.capitalize()} ({age} days)")
                break

    for threshold, weight, label in _REPOST_BANDS:
        if repost_count >= threshold:
            risk += weight
            reasons.append(label.format(n=repost_count).capitalize())
            break

    broad = _is_broad(title, description)
    if broad:
        risk += _BROAD_WEIGHT
        reasons.append(broad.capitalize())

    from_board = (posting.source or "").lower() in _BOARD_SOURCES
    if from_board and not closed:
        # The employer's own board still lists it, so it is open. Strong enough
        # to pull a posting back under the bar, never below zero.
        #
        # Withheld when the posting *says* it is closed, and only then. The
        # credit is an inference — "the board still carries it, so the req is
        # live" — and it is arguing with the employer's own words at that point.
        # A filled req left up on Greenhouse is the single most common way a
        # closed posting reaches a feed, so this is the case the exception is
        # for. Every other signal here is still outweighable by the board,
        # deliberately: those are guesses, and the board is evidence.
        risk += _BOARD_CREDIT
        reasons.append("Still listed on the company's own board")

    # Not the `else` of the credit above, which is the shape it had and the
    # reason it lied. Withholding the credit is a statement about the *closure*
    # — the board's word is not evidence against the employer's own — and it
    # says nothing at all about where the posting was fetched from. Chained,
    # a filled Greenhouse req that had also been syndicated three ways fell
    # through to this branch and was captioned "on 3 aggregators but on no
    # company board", on a card built from the company board's own listing.
    #
    # This signal is breadth *without a home*, per the module docstring, and
    # `from_board` is the whole question of whether there is a home. So it is
    # the condition, and the closure is not part of it.
    if not from_board and source_count >= _AGGREGATOR_BREADTH:
        risk += _AGGREGATOR_ONLY_WEIGHT
        reasons.append(
            f"on {source_count} aggregators but on no company board".capitalize()
        )

    risk = max(0, min(100, risk))
    return GhostAssessment(risk=risk, level=level_for(risk), reasons=reasons)


def assess_posting(posting, *, now: datetime | None = None) -> GhostAssessment:
    """:func:`assess` for a stored row, reading its own repost and source counts."""
    return assess(
        posting,
        repost_count=posting.repost_count or 0,
        source_count=max(1, len(posting.source_urls or [])),
        salary_band=(posting.salary_min, posting.salary_max),
        now=now,
    )


def apply_assessment(posting, assessment: GhostAssessment) -> GhostAssessment:
    """Write an assessment onto a stored row. Returns it, for chaining."""
    posting.ghost_risk = assessment.risk
    posting.ghost_reasons = list(assessment.reasons)
    return assessment


def refresh(posting, *, now: datetime | None = None) -> GhostAssessment:
    """Re-score a stored row from its current columns and save the result."""
    return apply_assessment(posting, assess_posting(posting, now=now))


def is_repost(stored_posted_at: datetime | None, incoming_posted_at: datetime | None,
              *, min_gap_days: int = 7) -> bool:
    """Did this scan see the same role advertised with a materially newer date?

    A gap is required because providers disagree about what ``posted_at`` means
    — some hand back the crawl time, which drifts a few hours every scan and
    would otherwise register as a repost every six hours forever.
    """
    if stored_posted_at is None or incoming_posted_at is None:
        return False
    if stored_posted_at.tzinfo is None:
        stored_posted_at = stored_posted_at.replace(tzinfo=UTC)
    if incoming_posted_at.tzinfo is None:
        incoming_posted_at = incoming_posted_at.replace(tzinfo=UTC)
    return (incoming_posted_at - stored_posted_at).days >= min_gap_days


__all__ = [
    "GHOST_AT",
    "STALE_AT",
    "LEVEL_OK",
    "LEVEL_STALE",
    "LEVEL_GHOST",
    "GhostAssessment",
    "apply_assessment",
    "assess",
    "assess_posting",
    "is_repost",
    "level_for",
    "refresh",
]
