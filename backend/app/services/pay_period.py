"""One pay-period table, because both sides of a salary comparison need it.

Every salary figure this product stores is a **year's** pay. Nothing in the
schema says so — ``profiles.salary_min`` and ``job_postings.salary_min`` are
bare integers with no period beside them — so it is a convention, and it has to
be enforced at every place a number gets in:
:func:`app.services.jd_parser.extract_salary` reading an employer's band,
:func:`app.services.resume_parser.extract_salary_expectation` reading a
candidate's floor, and
:func:`app.services.reply_agent.extract_salary_figures` reading a recruiter's
offer.

This module exists so those cannot disagree, and it is the twin of
:mod:`app.services.currency` — the same convention, the same enforcement
problem, and the same reason it cannot live in either parser: `jd_parser`
already reads `resume_parser` for its skill extractor, so a table living in one
of them could not be reached from the other without a cycle. That cycle is why
the resume side spent this product's whole life reading periods as though they
were not there. An employer's "€8.000 - €10.000 per month" was annualised to
$132,000; the candidate who wrote "Expected salary: €8.000 per month" on their
resume had it read as a floor of €8,000 a *year* — and being under
``_SALARY_MIN_PLAUSIBLE``, dropped, and replaced by a band inferred from their
seniority that nobody chose.
"""
from __future__ import annotations

import re

# How often the figure beside the band is paid, and what a year of it comes to.
#
# A band means nothing without its period, and the parser used to drop the
# period on the floor: "$85 - $105 per hour" — a $218,400 contract — was read as
# a band of 85,000-105,000 and scored against the candidate's floor as though
# the employer had offered eighty-five thousand dollars a year. European
# postings quote monthly and came off worse: "€8.000 - €10.000 per month" is
# €120,000 and was read as €10,000, which is below every floor and every market
# median this product models. `fit_scorer.score_salary` scored that dimension
# 0.11 under the note "below your 90,000 floor", and `salary_service` called it
# 92% under market. Both sentences are false, and both are shown to the user.
#
# 2080 hours is 40 × 52 — the full-time-equivalent year every US contract rate
# is quoted against. Days are 5 × 52. These are conventions rather than
# measurements, and they are the same conventions the recruiter quoting the rate
# is using; the alternative on the table was to keep refusing to read the figure
# at all, which is how the product arrived at numbers that were wrong instead of
# absent.
#
# `fortnight` and `semimonth` are the two US payroll frequencies, and they are
# here because they are written *around* the plain period word they contain:
# "bi-weekly" holds "weekly" and "semi-monthly" holds "monthly", so a table
# without them did not fail to read those postings — it read them as the inner
# word and was wrong by exactly a factor of two, in both directions. Bi-weekly
# is 26 pay periods and was annualised at 52: "$3,000 bi-weekly" is $78,000 and
# was stored as $156,000. Semi-monthly is 24 and was annualised at 12: "$4,000
# semi-monthly" is $96,000 and was stored as $48,000.
#
# The high one is the dangerous one, for the reason `_CLAUSE_BREAK` gives
# below: an inflated band clears every floor a candidate can set,
# `fit_scorer.score_salary` scores the dimension a confident 1.0, and that score
# gates autopilot sending. The low one is merely wrong out loud — $96,000 shown
# as "below your 90,000 floor" and called under market.
PERIOD_MULTIPLIERS: dict[str, int] = {
    "hour": 2080,
    "day": 260,
    "week": 52,
    "fortnight": 26,
    "semimonth": 24,
    "month": 12,
    "year": 1,
}

# The period as postings write it in the languages this feed is not in.
#
# Every branch of :data:`PERIOD_RE` below was English-only, and "no period
# named" means annual — so a monthly band written in any other language was
# read as a year's pay and came out **twelve times low**. That is the direction
# that empties a feed rather than the one that floods it:
# `salary_service.meets_floor` drops the posting outright, and
# `fit_scorer.score_salary` reports whatever survives as far under the
# candidate's floor.
#
# It stayed invisible because those postings mostly failed earlier. Half of
# Europe groups thousands with a space, no money shape could span one, and a
# band that matches nothing has no period left to get wrong. Teaching
# :data:`app.services.currency.SPACE_GROUPED` to read "15 000 - 20 000 PLN" is
# what put "miesięcznie" — Polish for "monthly", and how a Polish posting
# quotes pay — in front of this table for the first time.
#
# Only words that mean a pay period and nothing else. French "horaire" is
# "hourly" and also "timetable", Italian "orario" the same, Spanish "diario" is
# "daily" and also "newspaper" — and all three sit near a figure in postings
# about working hours rather than pay. So the pay-only spellings are listed
# ("taux horaire", "de l'heure", "al día") and the bare adjective is not. The
# same call `reply_agent._COMP_WORDS` makes about "rate" inside "corporate",
# and it is asymmetric for the reason the header gives: a missed period costs a
# factor of twelve downward, a false "hourly" costs a factor of 2080 upward,
# and only the second kind of error clears every floor and scores the dimension
# that gates autopilot a confident 1.0.
#
# Annual words earn their place even though an unnamed period is already
# annual: `period_near` takes the *nearest* period, so "45 000 € par an, versé
# en 12 mois" needs "par an" to be readable or the month behind it wins.
#
# No compound periods. "Bi-weekly" and "semi-monthly" are US payroll
# frequencies that nothing outside that payroll writes, and Spanish "quincenal"
# carries exactly the two-readings-four-times-apart ambiguity that
# :data:`_AMBIGUOUS` exists to refuse.
_LOCALISED: dict[str, tuple[str, ...]] = {
    "hour": (
        r"\bpar\s+heure\b", r"\bde\s+l['’]\s*heure\b", r"\btaux\s+horaire\b",
        r"\bpro\s+Stunde\b", r"\bst[uü]ndlich\w*", r"/\s*Std\.?",
        r"\bpor\s+hora\b",
        r"\ball['’]\s*ora\b",
        r"\bper\s+uur\b",
        r"\b(?:za|na)\s+godzin[ęe]\b", r"\bgodzinowo\b",
        r"\bper\s+timme\b", r"\bi\s+timmen\b",
        r"\bza\s+hodinu\b", r"\bhodinov[ěe]\b",
        r"\b[oó]r[aá]nk[eé]nt\b",
        r"\bв\s+час\b",
    ),
    "day": (
        r"\bpar\s+jour\b", r"\bjournalier\w*",
        r"\bpro\s+Tag\b", r"\bt[aä]glich\w*",
        r"\b(?:al|por)\s+d[ií]a\b",
        r"\bal\s+giorno\b",
        r"\bper\s+dag\b",
        r"\bdziennie\b",
        r"\bв\s+день\b",
    ),
    "week": (
        r"\bpar\s+semaine\b", r"\bhebdomadaire\b",
        r"\bpro\s+Woche\b", r"\bw[oö]chentlich\w*",
        r"\ba\s+la\s+semana\b", r"\bsemanal(?:es)?\b",
        r"\ba\s+settimana\b", r"\bsettimanale\b",
        r"\btygodniowo\b",
        r"\bper\s+vecka\b", r"\bi\s+veckan\b",
        r"\bt[yý]dn[ěe]\b",
        r"\bв\s+неделю\b",
    ),
    "month": (
        r"\bpar\s+mois\b", r"\bmensuel\w*", r"/\s*mois\b",
        r"\b(?:pro|im)\s+Monat\b", r"\bmonatlich\w*", r"\bMonatsgehalt\b",
        r"\b(?:al|por)\s+mes\b", r"\bmensual(?:es)?\b",
        r"\bal\s+mese\b", r"\bmensil[ei]\b",
        r"\b(?:por|ao)\s+m[eê]s\b", r"\bmensal(?:mente)?\b",
        r"\bper\s+maand\b", r"\bmaandelijks\b",
        r"\bmiesi[ęe]cznie\b", r"\bna\s+miesi[ąa]c\b",
        r"\bper\s+m[åa]nad\b", r"\bi\s+m[åa]naden\b", r"\bm[åa]nadsl[öo]n\b",
        r"\bper\s+m[åa]ned\b", r"\bm[åa]nedlig\w*",
        r"\bm[ěe]s[íi][čc]n[ěe]\b",
        r"\bhavonta\b", r"\bhavi\b",
        r"\bkuukaudessa\b",
        r"\bв\s+месяц\b",
    ),
    "year": (
        r"\bpar\s+an(?:n[ée]e)?\b", r"\bannuel\w*", r"/\s*an\b",
        r"\b(?:pro|im)\s+Jahr(?:e|es)?\b", r"\bj[aä]hrlich\w*",
        r"\bJahresgehalt\b",
        r"\b(?:al|por)\s+a[ñn]o\b", r"\banual(?:es)?\b",
        r"\ball['’]\s*anno\b", r"\bannu[oi]\b", r"\bannuale\b",
        r"\b(?:por|ao)\s+ano\b",
        r"\bper\s+jaar\b", r"\bjaarlijks\b",
        r"\brocznie\b", r"\bna\s+rok\b",
        r"\bper\s+[åa]r\b", r"\b[åa]rsl[öo]n\b", r"\b[åa]rlig\w*",
        r"\bro[čc]n[ěe]\b",
        r"\b[ée]vente\b", r"\b[ée]vi\b",
        r"\bvuodessa\b",
    ),
}


def _localised(period: str) -> str:
    """The non-English branches for one period, ready to splice into its group.

    Spliced *inside* the existing named group rather than added beside it:
    `period_near` reads ``match.lastgroup`` to name the period, and `re`
    refuses a second group of the same name. The table's keys are those names,
    so the two cannot drift apart.
    """
    return "|".join(_LOCALISED[period])


# The period as postings actually write it. Ordered longest-first inside each
# alternation so "per annum" is not clipped by "an". `/mo` and `/hr` need no
# word boundary in front because the slash is one.
PERIOD_RE = re.compile(
    r"(?:"
    # The compound periods, listed ahead of the plain words nested inside them.
    #
    # What actually makes them win is leftmost-match rather than this ordering:
    # English puts the qualifier in front, so "bi-", "semi-" and "every two"
    # all begin to the left of the "weekly" or "monthly" they qualify, and the
    # engine reaches the compound first whatever order the branches are in.
    # They are written first anyway, because a reader scanning for why
    # "monthly" does not claim "semi-monthly" should not have to know that.
    r"(?P<fortnight>\bbi[-\s]?weekly\b|\bfortnightly\b"
    r"|(?:per|a|each|/)\s*fortnight\b"
    r"|\bevery\s+(?:two|2|other)\s+weeks?\b)"
    r"|(?P<semimonth>\bsemi[-\s]?monthly\b"
    r"|\btwice\s+(?:(?:a|per|each)\s+)?month(?:ly)?\b)"
    r"|(?P<ambiguous>\bbi[-\s]?monthly\b"
    r"|\bevery\s+(?:two|2|other)\s+months?\b)"
    # `p/h` keeps its slash; the bare `ph` carries a guard, and it is the
    # doctorate it is guarding against. "Ph.D" is two letters, a boundary and a
    # letter — exactly the shape of the British "£15 ph" — so `\bp/?h\b` read
    # the qualification a posting asks for as the period it pays by, and no
    # other branch in this table has an innocent word that close to it.
    #
    # The cost is the whole factor of 2080 the module header calls the
    # dangerous direction. "Salary range $2,000 (Ph.D. required)" annualised to
    # $4,160,000 — under `jd_parser._IMPLAUSIBLE_ANNUAL`, so it was stored,
    # shown, and scored: a band that clears every floor a candidate can set,
    # `fit_scorer.score_salary` at a confident 1.0, and that score gates
    # autopilot sending. The same reading also drags the qualification into the
    # displayed band, because `jd_parser._with_period` appends the period words
    # it thinks it found: the card read "$2,000 (Ph".
    #
    # The guard admits the spellings that separate the two letters from the D
    # ("Ph.D", "Ph. D", "Ph D") and needs nothing for "PhD", which has no word
    # boundary after the h and never matched. "£15 ph", "$20 ph." and "£15 p/h"
    # are untouched — a real rate is not followed by a D.
    rf"|(?P<hour>(?:per|an|a|each|/)\s*(?:hour|hr)\b|\bhourly\b|/\s*hr\b"
    rf"|\bp/h\b|\bph\b(?!\.?\s*d\b)"
    rf"|{_localised('hour')})"
    rf"|(?P<day>(?:per|a|each|/)\s*day\b|\bdaily\b|\bper diem\b"
    rf"|{_localised('day')})"
    rf"|(?P<week>(?:per|a|each|/)\s*(?:week|wk)\b|\bweekly\b"
    rf"|{_localised('week')})"
    rf"|(?P<month>(?:per|a|each|/)\s*(?:month|mo)\b|\bmonthly\b|\bpcm\b"
    rf"|{_localised('month')})"
    rf"|(?P<year>(?:per|a|each|/)\s*(?:year|annum|yr)\b|\bannually\b|\byearly\b"
    rf"|\bp\.?a\.?(?!\w)|\bper annum\b"
    rf"|{_localised('year')})"
    r")",
    re.I,
)

# How far either side of the band to look for the period. Deliberately short.
# The period is written touching the figure — "$85/hr", "$85 per hour", "an
# hourly rate of $85" — and a wider window starts reading unrelated prose:
# "$120,000. We aim to close within a week" is annual pay, not weekly.
PERIOD_LOOKAHEAD = 24
PERIOD_LOOKBEHIND = 28

#: Extra characters read past each window, so a period word is never cut in half.
#:
#: The two windows above bound how *far* from the band a period may be written.
#: They were also — accidentally — bounding how much of the word the regex could
#: *see*: the text was sliced to the window and matched inside the slice, so a
#: word straddling the cut was read as whatever fragment survived it. Both
#: directions land on the dangerous error the module header describes.
#:
#: Forwards, it takes a guard away. ``\bph\b(?!\.?\s*d\b)`` refuses the "Ph"
#: of "Ph.D" by looking at what follows it, and twenty-two characters of
#: ordinary prose put those two letters at the very end of the lookahead:
#: "Salary $2,000 for a candidate with Ph.D. required" sliced to "...with Ph",
#: the lookahead saw end-of-string, found no D to refuse, and priced a
#: doctorate as a British hourly rate. $2,000 x 2080 is $4,160,000, which is
#: under ``jd_parser._IMPLAUSIBLE_ANNUAL`` and so was stored, shown, and scored
#: — and ``_with_period`` put "for a candidate with Ph" on the card beside it.
#:
#: Backwards, it takes the qualifier off a compound. "Bi-weekly" and
#: "semi-monthly" are in :data:`PERIOD_RE` precisely because they *contain*
#: "weekly" and "monthly", and a cut inside the qualifier hands the plain word
#: straight back: "Paid bi-weekly at a fixed rate of $3,000" is $78,000 and was
#: read as $156,000. The same cut can also lose a word altogether — "hourly"
#: sliced to "ourly" matches nothing, and an unfound period means annual.
#:
#: Wide enough for the longest alternative in the table plus its guard, and the
#: distance rules below do the actual bounding, so the exact number only has to
#: be generous.
_WINDOW_MARGIN = 32

#: What ends the clause the band belongs to. A period word on the far side of
#: one of these is describing something else.
#:
#: The comma is here because a posting states its hours and its pay in one
#: sentence far more often than in two: "Full-time, 40 hours per week, salary
#: $95,000" is the ordinary way to write it, and without the comma the nearest
#: period word to that band is "per week". Fifty-two times $95,000 is
#: $4,940,000 — under `jd_parser`'s implausibility ceiling, so it was stored,
#: shown, and scored. It clears every floor a candidate can set,
#: `fit_scorer.score_salary` gives the dimension a confident 1.0, and that
#: score gates autopilot sending. The same posting written with a full stop
#: instead of a comma parsed correctly, which is why nothing caught it.
#:
#: The cost is a band that writes a comma between itself and its own period —
#: "Hourly, $85". Nobody does; every real spelling ("$85/hr", "$85 per hour",
#: "hourly rate: $85") puts nothing but space or a colon in the gap. Note that
#: this is only ever searched in the *gap* between a period word and the band,
#: so the commas inside "$95,000" are not in scope.
_CLAUSE_BREAK = re.compile(r"[.,;!?\n•|]|\s[-–—]\s")

#: The group name for a period that names a real frequency but not which one.
#:
#: "Bi-monthly" is twice a month to half the people who write it and every
#: second month to the other half, and those two readings are *four times*
#: apart. The posting carries nothing that picks between them, so this is the
#: one place where the module's usual preference — a read figure over an absent
#: one — does not hold: guessing here is a coin flip on a four-fold error,
#: which is worse than either wrong reading the plain-word bug produced.
#:
#: Matching it and then discarding it is not the same as leaving it out of
#: :data:`PERIOD_RE`. Left out, "bi-monthly" would fall through to the `month`
#: branch inside it and be annualised at 12 — a confident answer, one of the
#: two readings, picked for no reason. Matched, it shadows that branch and the
#: figure falls back to the bare-number rule instead.
_AMBIGUOUS = "ambiguous"


def _resolve(group: str | None) -> str | None:
    """The period a matched group names, or None when it names no single one."""
    return None if group == _AMBIGUOUS else group


def _match_ahead(tail: str) -> re.Match[str] | None:
    """The period written *after* a band, as one match — or None.

    Split out because two callers need the same answer about the same string
    and were computing it two different ways.
    :func:`period_near` prices the band from it;
    :func:`app.services.jd_parser._with_period` copies the employer's own words
    off it onto the display string, so that a card annualised behind the scenes
    still says what period it was annualised from. A tail the two bounded
    differently is a card reading "$4,000" beside a stored $48,000 — the exact
    failure the display string exists to prevent, arriving from inside it.
    """
    window = tail[: PERIOD_LOOKAHEAD + _WINDOW_MARGIN]
    match = PERIOD_RE.search(window)
    if match is None or match.start() >= PERIOD_LOOKAHEAD:
        return None
    if _CLAUSE_BREAK.search(window[: match.start()]):
        return None
    return match


def period_span_after(tail: str) -> int | None:
    """Where the period phrase after a band ends in *tail*, or None if none.

    An index rather than the text, so the caller keeps the employer's exact
    spelling — "/hr" stays "/hr" and "per month" stays "per month".
    """
    match = _match_ahead(tail)
    return match.end() if match is not None else None


def period_near(head: str, tail: str, within: str = "") -> str | None:
    """Which pay period the text around a band names, or None if it doesn't.

    None also covers the case where the text names a period that does not
    resolve to a single frequency — see :data:`_AMBIGUOUS`. That answer is
    final rather than a cue to keep looking: the ambiguous word is the one
    attached to this band, and a clearer period further away belongs to
    something else.

    *tail* wins over *head*. A posting writes the period after the figure far
    more often than before it, and when it does both — "hourly rate: $85 - $105
    per hour" — they agree. Where they disagree the trailing one is the one
    attached to this band.

    *within* is the band itself, consulted between the two. It can only say
    anything at all since :data:`app.services.jd_parser._INLINE_PERIOD` — a
    range that writes its unit
    on both halves ("€8.000 per month - €10.000 per month") now spans the first
    one, and reading only the neighbours would leave the period unfound on the
    very postings that state it twice. No clause-break guard applies to it: the
    words are inside the band, which is as attached as attached gets.

    Either way the period has to be in the **same clause** as the figure.
    Postings are full of period words that are not the pay period, and the
    lookbehind is where they bite: "We work 40 hours a week. Salary: $2,500"
    put "a week" fifteen characters in front of a band it has nothing to do
    with, and multiplying by 52 read a $2,500 figure as $130,000. A sentence
    ended in between, and that is the whole signal — so a match with a clause
    break between it and the band is thrown away.

    **Distance is a rule about the match, not a cut in the string.** Both
    windows used to be taken as slices and searched inside, which meant a word
    lying across the edge was matched as the fragment that survived the cut —
    see :data:`_WINDOW_MARGIN` for the two ways that goes wrong. The regex now
    reads past the edge and the offset decides: forwards the match has to
    *begin* inside the lookahead, backwards it has to *end* inside the
    lookbehind. A word is near the figure when the part of it facing the figure
    is; nothing here reaches further for a period than it did before.
    """
    ahead = _match_ahead(tail)
    if ahead is not None:
        return _resolve(ahead.lastgroup)

    inside = PERIOD_RE.search(within)
    if inside is not None:
        return _resolve(inside.lastgroup)

    # Rightmost match in the lookbehind, not the first: the period nearest the
    # figure is the one describing it.
    window = head[-(PERIOD_LOOKBEHIND + _WINDOW_MARGIN) :]
    edge = max(0, len(window) - PERIOD_LOOKBEHIND)
    behind = None
    for match in PERIOD_RE.finditer(window):
        if match.end() > edge:
            behind = match
    if behind is not None and not _CLAUSE_BREAK.search(window[behind.end() :]):
        return _resolve(behind.lastgroup)
    return None


def annual_multiplier(text: str, start: int, end: int) -> int:
    """How many of the pay period named around ``text[start:end]`` make a year.

    ``1`` when the surrounding text names no period, which is the reading a
    bare salary figure gets everywhere in this product.

    Offsets rather than the head/tail strings :func:`period_near` wants,
    because a caller reading running text has a match object over the whole
    string rather than a span it has already cut out. That is every caller but
    :func:`app.services.jd_parser.extract_salary`, which cut the band out of
    the posting before it had the offsets to hand: a recruiter's email
    (:func:`app.services.reply_agent.extract_salary_figures`, for whom an offer
    quoted as a rate was simply not a figure) and a candidate's resume
    (:func:`app.services.resume_parser.extract_salary_expectation`).
    """
    # Cut with the margin on, because `period_near` measures distance from the
    # edge of what it is handed — a head or tail already trimmed to the bare
    # window would reintroduce exactly the cut it stopped making.
    head = text[max(0, start - PERIOD_LOOKBEHIND - _WINDOW_MARGIN) : start]
    tail = text[end : end + PERIOD_LOOKAHEAD + _WINDOW_MARGIN]
    period = period_near(head, tail, text[start:end])
    return PERIOD_MULTIPLIERS.get(period or "year", 1)


__all__ = [
    "PERIOD_LOOKAHEAD",
    "PERIOD_LOOKBEHIND",
    "PERIOD_MULTIPLIERS",
    "PERIOD_RE",
    "annual_multiplier",
    "period_near",
    "period_span_after",
]
