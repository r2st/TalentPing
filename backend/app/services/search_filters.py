"""The three criteria a candidate states that a fit score cannot express.

The scorer is a weighted average, and a weighted average is the wrong shape for
a *constraint*. "I will not work for an agency", "I want a startup, not a bank",
"I need a permanent role, not a six-month contract" are not preferences to be
outvoted by a strong skills match — they are the difference between an
application that could become a job and one that wastes everybody's afternoon.
:mod:`app.services.auto_apply_service` already learned this twice, for roles
(:func:`~app.services.auto_apply_service.relevance_gate`) and for places
(:func:`~app.services.auto_apply_service.location_gate`). This module is the
same lesson for the three criteria that had no home at all:

* **Employment type** — permanent, contract, part-time, internship. Nothing in
  the schema carried it, so a candidate looking for a staff role got automatic
  outreach about three-month contracts and had no control that would stop it.
  Read off the posting's own words, because no board we ingest from puts it in
  a field.
* **Company size** — read off :class:`~app.models.company_profile.CompanyProfile`,
  the research cache, since a posting almost never says. Stated as *tiers*
  (startup / scaleup / midsize / enterprise) rather than as the cache's own
  headcount buckets: "1-10 but not 11-50" is not an opinion anyone holds, and
  offering it invites a filter so narrow it silently empties the feed.
* **Excluded companies** — the one list every candidate has and no product asks
  for: a current employer, a former one, the agency that burned them. Matched
  on :func:`~app.services.job_dedup.normalize_company`, so "Acme, Inc." on the
  posting is excluded by "acme" in the list.

**Unknown is handled differently per gate, on purpose.** For employment type an
unstated posting passes: the words are absent from most ads, and refusing them
all would reject the majority of the feed to enforce a preference the employer
never contradicted. For company size the same, and for a sharper reason — the
size comes from *our* optional research cache rather than from the employer, so
"unknown" means we did not look, and a gate must never punish a candidate for
our own missing homework. This is the opposite of ``location_gate``, where an
unstated location is the *employer's* omission on a field they had every reason
to fill, and where the cost of being wrong is an application to another
continent. The exclusion list has no unknown case: a posting either names a
company we were told to avoid or it does not.

Every gate returns a *sentence*, not a boolean, for the reason the rest of the
pipeline does: the reason is stored on the posting
(``screened_out_reason``) and shown to the user, and "screened out" with no
explanation is how a filter turns into a bug report.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company_profile import CompanyProfile
from app.services.job_dedup import normalize_company
from app.services.places import fold_diacritics

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.job import JobPosting
    from app.services.fit_scorer import Targeting


# --------------------------------------------------------------------------- #
# Employment type                                                             #
# --------------------------------------------------------------------------- #

#: The types a candidate can ask for, and the only values the API accepts.
EMPLOYMENT_TYPES: tuple[str, ...] = (
    "full_time",
    "part_time",
    "contract",
    "internship",
    "temporary",
)

# Phrases that name a type in a job ad, most specific first. Order matters: a
# posting saying "full-time contract" is a contract, and "part-time internship"
# is an internship, so the narrower category has to win. Matched against a
# whitespace-normalized lower-cased haystack with hyphens folded to spaces, so
# "Full-Time", "full time" and "FULLTIME" all land on the same needle.
_ENGLISH_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "internship",
        ("internship", "intern position", "summer intern", "working student", "praktikum"),
    ),
    (
        "contract",
        (
            "contract role",
            "contract position",
            "contractor",
            "freelance",
            "b2b contract",
            "fixed term",
            "day rate",
            "outside ir35",
            "inside ir35",
        ),
    ),
    (
        "temporary",
        ("temporary position", "temp role", "maternity cover", "parental cover", "interim"),
    ),
    ("part_time", ("part time", "parttime")),
    ("full_time", ("full time", "fulltime", "permanent position", "permanent role")),
)

# The same statement in the languages the rest of this feed is already read in.
#
# Every needle above is English, and "praktikum" was the single exception —
# added, one guesses, because a German internship happened to come up. Nothing
# else did. So an employment-type gate that a candidate switched on to keep
# fixed-term work out of their inbox let through every French CDD, every German
# befristeter Vertrag, every Polish umowa na czas określony and every Italian
# contratto a tempo determinato, because `detect_employment_type` returned None
# for all of them and this module treats None as harmless.
#
# It is the same ceiling :data:`app.services.pay_period._LOCALISED` was written
# against, from the same direction: the parser that reads the money off a
# European posting has understood "miesięcznie" for a while now, and the gate
# standing next to it could not tell a permanent role from a three-month one in
# any language but its own.
#
# The bar for a needle here is higher than for an English one, and deliberately
# so — the asymmetry this module's docstring describes runs the other way for a
# *wrong* answer. An unread posting passes the gate; a misread one is vetoed,
# and the candidate never sees the job or the reason. So every entry below
# names an engagement and nothing else in its own language:
#
# * "CDI" and "CDD" are how LinkedIn and Indeed label a French posting, not
#   words that happen to appear in one.
# * Spanish "autónomo" is *not* here. It is "freelance" and it is also
#   "autonomous", and "equipo autónomo" is a sentence about a team. Same call
#   `pay_period` makes about "horaire", and for the same reason.
# * Bare "stage" is not here either, in French or Dutch: "early-stage startup"
#   is one of the commonest phrases in the feed. "stagiaire", "stage de" and
#   "stagiair" carry the meaning without the collision.
# * Bare "b2b" is not here. Half the postings in this feed are at B2B
#   companies; "umowa b2b" is the contract.
#
# Written unaccented because `_haystack` folds the accents off before any of
# these are looked for — see :func:`app.services.places.fold_diacritics`.
_LOCALISED_PHRASES: dict[str, tuple[str, ...]] = {
    "internship": (
        # fr, de, es, it, pt, nl, pl
        "stagiaire", "stage de", "contrat de stage", "convention de stage",
        "praktikant", "praktikantin", "werkstudent",
        "becario", "becaria", "en practicas", "practicas profesionales",
        "tirocinio", "stagista",
        "estagio", "estagiario", "estagiaria",
        "stagiair", "stageplaats",
        "staz", "stazysta", "praktykant", "praktyki studenckie",
    ),
    "contract": (
        "cdd", "contrat a duree determinee", "mission freelance",
        "portage salarial",
        "freiberuflich", "werkvertrag", "auf honorarbasis",
        "obra y servicio",
        "partita iva", "tempo determinato", "collaborazione a progetto",
        "contrato a termo", "prestacao de servicos",
        "zzp", "opdrachtbasis", "bepaalde tijd",
        "umowa b2b", "kontrakt b2b", "umowa zlecenie", "umowa o dzielo",
        "czas okreslony",
        "visstidsanstallning", "konsultuppdrag",
    ),
    "temporary": (
        "en remplacement",
        "zeitarbeit", "elternzeitvertretung", "krankheitsvertretung",
        "contrato temporal",
        "sostituzione maternita",
        "uitzendkracht", "uitzendbasis",
        "zastepstwo",
        "vikariat",
    ),
    "part_time": (
        "temps partiel",
        "teilzeit",
        "media jornada", "jornada parcial", "tiempo parcial", "jornada reducida",
        "tempo parziale",
        "meio periodo",
        "deeltijd",
        "niepelny etat", "niepelnym etacie",
        "deltid",
        "osa aikainen",
    ),
    "full_time": (
        "temps plein", "cdi", "contrat a duree indeterminee",
        "vollzeit", "festanstellung",
        "jornada completa", "tiempo completo", "contrato indefinido",
        "tempo pieno", "tempo indeterminato",
        "tempo integral", "periodo integral",
        "voltijd", "vast dienstverband", "onbepaalde tijd",
        "pelny etat", "pelnym etacie", "umowa o prace", "czas nieokreslony",
        "heltid", "tillsvidareanstallning",
        "fast stilling", "fast ansettelse",
        "kokoaikainen", "vakituinen",
    ),
}

# Four of those pairs are one word inside another, and all four are safe for the
# same reason: a needle is matched space-padded, so " bepaalde tijd " cannot
# match "onbepaalde tijd", " czas okreslony " cannot match "czas nieokreslony",
# " tempo determinato " cannot match "tempo indeterminato", and the German
# pattern below cannot match "unbefristet". The narrower category is checked
# first in every case anyway, which is what makes the ordering the table's own
# safeguard rather than a coincidence.
_TYPE_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (kind, needles + _LOCALISED_PHRASES.get(kind, ()))
    for kind, needles in _ENGLISH_PHRASES
)

# A fixed term is a *number* of months, and the two that were written out by
# hand were 6 and 12. Every other length came back unknown and sailed through
# the gate — including the three-month contract this module's docstring opens
# with as the thing a candidate looking for a staff role should never have been
# emailed about.
#
# Kept to months, and to the same shape as the needles it replaces: a job ad
# says "6-month contract", and a company blurb saying "we signed a three year
# contract with our largest customer" is prose about a customer, not about the
# engagement. `_haystack` has already folded the hyphen, so "6-month",
# "6 month" and "6 months" all arrive here spelt the same way.
#
# German declines its adjectives, and the two that matter here are adjectives:
# "befristeter Vertrag", "befristete Anstellung", "unbefristetes
# Arbeitsverhältnis". A phrase table matching whole space-padded tokens reads
# each ending as a different word, so the fixed-term posting every German board
# labels this way came back unknown. The stem plus its ending is one pattern
# instead of six needles, and the leading space is what keeps "befristet" from
# claiming "unbefristet" — which is its own opposite, and the one reading that
# would veto a permanent role for a candidate who asked for permanent roles.
_TYPE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "contract": (
        re.compile(r" \d{1,2} months? contract "),
        re.compile(r" befristet\w* "),
    ),
    "full_time": (re.compile(r" unbefristet\w* "),),
}

# The other number a posting states its type as: the *hours*. The phrase table
# carried exactly two of them — "20 hours per week" and "40 hours per week" —
# and a working week is not written that way anywhere but the United States.
# "37.5 hours per week" (the UK), "35 hours per week" (France), "38 hours per
# week" (Australia) and every part-time number that is not exactly 20 all came
# back unknown, which this module treats as harmless and lets straight through
# the gate. So a candidate who asked for full-time work was emailed about a
# 24-hour-a-week role, and one who asked for part-time saw nothing but the
# postings that happened to say "part time" in words.
#
# 35 is the split, because 35 is the split: it is what the US BLS, the UK ONS
# and Eurostat all call the floor of a full-time week, and it sits below every
# national standard week and above every part-time one.
_FULL_TIME_HOURS = 35.0

# ...but only when the number could be a working week at all. A benefits
# paragraph says "four hours per week of learning time" and an on-call rota
# says "five hours per week", and reading either as the engagement would badge
# a permanent role part-time on the strength of a perk. Below the floor the
# sentence is far more likely to be about something other than the job, and a
# genuine sub-10-hour post is rare enough that unknown is the better answer.
_HOURS_MIN = 10.0
_HOURS_MAX = 60.0

#: "37.5 hours per week", "40 hrs a week", "35 hour week", "30 Stunden pro
#: Woche". `_haystack` has already folded the slash in "hours/week" and the
#: hyphen in "35-hour week" to a space, so every spelling arrives here as
#: whitespace-separated tokens; the decimal point survives because
#: `_SENTENCE_DOT` keeps a dot with a digit on both sides.
#
# The unit and the week word are both listed in the languages the phrase table
# above now reads, and for the same reason: the hours are the *fallback*, the
# thing a posting states when it does not name its type in words, so a feed
# whose types are unreadable leans on them hardest. "35 heures par semaine" —
# the statutory French week, and the number this constant was chosen around —
# matched nothing at all.
#
# Two shapes were missing, and between them they are how most of Europe writes
# a working week at all.
#
# **The unit is one letter.** "35h", "40h/week", "30h/Woche", "37,5h par
# semaine" — the commonest spelling in French, German, Spanish and Italian
# postings, and not on the unit list, so every one of them came back unknown
# and sailed through the gate. It is safe to add precisely because the unit is
# followed by a mandatory space and then a week word: "40 hectares per week"
# cannot match `h`, because what follows the `h` is "ectares" and not a space.
#
# **The number and the unit touch.** Nobody writes "40 h/week"; they write
# "40h/week". The space between figure and unit is optional now, which costs
# nothing — "40hrs a week" and "35heures par semaine" were unreadable for the
# same reason and by the same rule.
#
# Polish loses its suffix for the same reason the hour does: boards write
# "40 godz./tydzień", and `godzin\w*` needs the whole stem.
_HOURS_RE = re.compile(
    r"(?<![\d.])(\d{1,2}(?:\.\d{1,2})?) ?"
    r"(?:hours?|hrs?|stunden|std|heures?|horas?|ore|ora"
    r"|godz\w*|hodin\w*|uur|uren|timer|timmar|tuntia|h) "
    r"(?:per |a |an |pro |each |every |in a |par |por |al |alla |la |na |i |w )?"
    r"(?:weekly|week|wk|wochen|woche|semaines|semaine|semanales|semanais"
    r"|semanal|semana|settimanali|settimanale|settimana|tygodniowo|tydzien"
    r"|tydne|viikossa|veckan|vecka|uken|uke|ugen|uge)(?![a-z])"
)

#: The same statement written as a share of a full week: "0.8 FTE", "80% FTE",
#: and the reversed order Dutch and Nordic postings use, "FTE: 0.8". Anything
#: short of a whole one is a fraction of a full-time role by the employer's own
#: arithmetic, so it is part-time however the fraction is spelt.
_FTE_RE = re.compile(r"(?<![\d.])(\d(?:\.\d{1,2})?) fte(?![a-z])")
_FTE_REVERSED_RE = re.compile(r"\bfte (\d(?:\.\d{1,2})?)(?![\d.])")
_FTE_PCT_RE = re.compile(r"(?<![\d.])(\d{1,3}) ?% ?(?:fte|position|role|contract)(?![a-z])")


def _weekly_commitment(source: str) -> str | None:
    """``part_time``/``full_time`` read off a stated week, or ``None``.

    Consulted only after the phrase table has had its turn, so a posting that
    names its type in words keeps that answer: "6 month contract, 40 hours per
    week" is a contract, not a full-time role.
    """
    for match in _HOURS_RE.finditer(source):
        hours = float(match.group(1))
        if not _HOURS_MIN <= hours <= _HOURS_MAX:
            continue
        return "full_time" if hours >= _FULL_TIME_HOURS else "part_time"

    for pattern in (_FTE_RE, _FTE_REVERSED_RE):
        for match in pattern.finditer(source):
            fraction = float(match.group(1))
            if not 0 < fraction <= 1:
                continue
            return "full_time" if fraction == 1 else "part_time"

    for match in _FTE_PCT_RE.finditer(source):
        percent = float(match.group(1))
        if not 0 < percent <= 100:
            continue
        return "full_time" if percent == 100 else "part_time"

    return None

# "Contract" on its own is not evidence — every permanent offer letter is a
# contract, and half of all job ads use the word about the paperwork rather than
# the engagement. Only the phrases above count, which is why this module reads
# "contract role" and not "contract".

# Everything that is not a letter, digit, dot or percent folds to a space. The
# two survivors are needed by needles ("0.5 fte", "50% fte"); everything else —
# brackets, commas, hyphens, slashes, non-breaking spaces — becomes a boundary.
# That folding is what lets "(6 month contract)", "Full-time internship," and
# "part/time" all reduce to the same phrase. An earlier version folded only
# hyphens, and every posting that put its type in brackets or before a comma
# came back unknown, which is the failure mode this whole module is built to
# treat as harmless — so it would have gone unnoticed.
_PUNCT = re.compile(r"[^a-z0-9.%]+")

# Which is why the accents have to come off *first*. `_PUNCT` keeps `a-z0-9`
# and turns everything else into a space, and an accented letter is everything
# else: "intérim" folded to the two fragments "int rim", "durée" to "dur e",
# "pełny" to "pe ny". Not a needle that failed to match — a word that stopped
# being a word, in every language this feed carries but English.
#
# "Interim" is already on the temporary list, in that exact spelling, and it is
# the French word for the engagement as well as the English one. A French
# agency posting said so outright and this module could not read its own needle,
# so a candidate who had asked for permanent work only was emailed about a
# stand-in role — the failure `employment_gate` exists to prevent, arriving
# through the punctuation rather than through the table.
#
# `fold_diacritics` is the same helper :func:`app.services.places.place_tokens`
# folds place names with, for the identical reason and after the identical bug.
# Shared rather than copied so the transliterations ("ß", "ø", "ł") cannot drift
# into two versions.

# ...and the dot only survives when it is a decimal point. Keeping every dot is
# what the pattern above did, and a job ad ends its sentences: "This is an
# internship." folded to " this is an internship. ", and the phrase match — a
# space-padded `in`, which is what makes the padding above work — looks for
# " internship " and does not find it. So a needle at the end of a sentence was
# invisible, and *most* postings state their type in a sentence: "This role is
# full time.", "We are hiring a contractor.", "Fixed term." all came back
# unknown, which this module treats as harmless and lets straight through the
# gate.
#
# A digit on both sides is the whole test, because "0.5 fte" is the only needle
# with a dot in it and the only thing the exception was ever for.
_SENTENCE_DOT = re.compile(r"(?<!\d)\.|\.(?!\d)")

# ...and the *comma* between two digits is the same decimal point, written the
# way most of Europe writes it. `_PUNCT` folded it to a space, so "37,5 heures
# par semaine" — the standard UK-equivalent week in France and Belgium, and
# "0,8 FTE" in the Netherlands and Scandinavia — arrived as two numbers. The
# first was discarded (no unit follows it) and the second read as "5 hours",
# which `_HOURS_MIN` then rejects as a perk rather than a week. Unknown either
# way, which is the answer that lets a part-time posting through a full-time
# gate.
#
# Turned into a dot rather than merely kept, so there is one decimal spelling
# downstream and `_HOURS_RE` needs no second branch. Safe against a thousands
# separator: the hour and FTE patterns both cap the integer part at two digits
# and require a space or `%` right after, and "40,000" has neither.
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")

# How much of the description is worth reading. The type is stated in the first
# paragraph or in the benefits block; scanning 200KB of boilerplate to find
# "interim" in an unrelated sentence costs time and buys false positives.
_SCAN_LIMIT = 4000


def _haystack(*parts: str | None) -> str:
    """Lower-case, unaccented, punctuation-folded text, padded at both edges.

    The padding is what lets a needle be matched as a phrase without a regex per
    phrase — ``" full time "`` cannot match inside ``"beautifulltimes"``.

    The unaccenting has to happen before :data:`_PUNCT` rather than after, and
    that ordering is the whole fix: once the accent has become a space the word
    is already two words, and nothing downstream can put it back together.
    """
    joined = fold_diacritics(" ".join(p for p in parts if p).lower())
    joined = _DECIMAL_COMMA.sub(".", joined)
    folded = _PUNCT.sub(" ", _SENTENCE_DOT.sub(" ", joined))
    return " " + folded.strip() + " "


def detect_employment_type(
    title: str | None, description: str | None = None
) -> str | None:
    """The engagement a posting is advertising, or ``None`` when it won't say.

    The title is weighed first and on its own: a title saying "(6 month
    contract)" is decisive, and a description that later mentions "our full time
    team" should not be able to overturn it. Only when the title is silent does
    the body get a look.

    Returns ``None`` far more often than it returns a value, and that is the
    expected outcome rather than a failure — see the module docstring for what
    the gate does with it.
    """
    for source in (
        _haystack(title),
        _haystack((description or "")[:_SCAN_LIMIT]),
    ):
        if source.strip() == "":
            continue
        for kind, needles in _TYPE_PHRASES:
            if any(f" {needle} " in source for needle in needles):
                return kind
            # Checked inside the same loop, so a pattern obeys the
            # most-specific-first ordering the phrase table depends on.
            if any(pattern.search(source) for pattern in _TYPE_PATTERNS.get(kind, ())):
                return kind
        # Last, because a stated week is the weakest evidence in the source: a
        # contract and an internship both have hours, and both have already had
        # their turn above.
        commitment = _weekly_commitment(source)
        if commitment is not None:
            return commitment
    return None


def employment_gate(targeting: Targeting, posting: JobPosting) -> str | None:
    """Veto an automatic application to the wrong kind of engagement.

    Returns a skip reason, or ``None`` to let the posting through.

    A candidate who named no types is unaffected, and so is a posting that never
    said which type it is.
    """
    wanted = stated(getattr(targeting, "employment_types", None))
    if not wanted:
        return None

    kind = detect_employment_type(posting.title, posting.description)
    if kind is None or kind in wanted:
        return None
    return (
        f"reads as {_label(kind)} work, and you're looking for "
        f"{_join(_label(w) for w in wanted)}"
    )


# --------------------------------------------------------------------------- #
# Company size                                                                #
# --------------------------------------------------------------------------- #

#: The tiers a candidate picks from, and the research cache's own buckets that
#: each one covers. The buckets are :data:`app.services.company_research._SIZE_BUCKETS`
#: labels; they are restated here rather than imported so a change to the
#: research granularity is a visible edit to this mapping instead of a silent
#: change to what a user's saved filter means.
SIZE_TIERS: dict[str, tuple[str, ...]] = {
    "startup": ("1-10", "11-50"),
    "scaleup": ("51-200", "201-500"),
    "midsize": ("501-1000", "1001-5000"),
    "enterprise": ("5000+",),
}

_TIER_LABELS = {
    "startup": "startups (under 50)",
    "scaleup": "scale-ups (50-500)",
    "midsize": "mid-size companies (500-5,000)",
    "enterprise": "large companies (5,000+)",
}

_BUCKET_TO_TIER: dict[str, str] = {
    bucket: tier for tier, buckets in SIZE_TIERS.items() for bucket in buckets
}


def tier_for_bucket(bucket: str | None) -> str | None:
    """Which tier a research bucket falls in, or ``None`` for an unknown label.

    An unrecognised bucket — a research row written by a future version with a
    finer split — reads as unknown rather than as a mismatch, so a schema the
    filter has not learned yet cannot start silently rejecting postings.
    """
    if not bucket:
        return None
    return _BUCKET_TO_TIER.get(bucket.strip())


def company_size(db: Session, company: str | None) -> str | None:
    """The cached headcount bucket for *company*, if we have ever researched it.

    A plain read of the research cache — this never triggers research. A gate is
    not the place to spend an LLM call: it runs on every candidate posting in
    every run, and the answer it would buy is only ever used to *reject*.
    """
    key = normalize_company(company)
    if not key:
        return None
    return db.scalar(
        select(CompanyProfile.size).where(CompanyProfile.normalized_name == key)
    )


def company_size_gate(targeting: Targeting, bucket: str | None) -> str | None:
    """Veto an automatic application to a company of the wrong size.

    Takes the already-looked-up *bucket* rather than a session, so the caller
    controls when the read happens and the rule itself stays pure and testable.
    ``None`` — never researched, or researched and inconclusive — passes.
    """
    wanted = stated(getattr(targeting, "company_sizes", None))
    if not wanted:
        return None

    tier = tier_for_bucket(bucket)
    if tier is None or tier in wanted:
        return None
    return (
        f"{_TIER_LABELS.get(tier, tier)} — you're looking at "
        f"{_join(_TIER_LABELS.get(w, w) for w in wanted)}"
    )


# --------------------------------------------------------------------------- #
# Excluded companies                                                          #
# --------------------------------------------------------------------------- #


def _same_employer(listed: list[str], posting: list[str]) -> bool:
    """Whether two normalized company names name the same employer.

    One is a *whole-word prefix* of the other. Users type the brand and boards
    print the entity, and the entity is the brand plus a descriptive tail:
    "Meta Platforms", "Amazon Web Services", "Deloitte Consulting", "JPMorgan
    Chase". :func:`~app.services.job_dedup.normalize_company` strips the legal
    form off the end and nothing else — by design, since "Group Nine Media"
    keeps its "group" — so exact equality could never span that tail, and a
    candidate who wrote down "Meta" to avoid it kept getting outreach about
    "Meta Platforms, Inc.".

    Substring matching is still refused, and the tokens are what refuse it:
    "meta" is a prefix of "metabase" as characters and not as words, so the
    company this gate must never take out is exactly the one a word boundary
    keeps.

    Prefixes in **either** direction. The common shape is a short list entry
    against a long posting, but a candidate who pasted "Deloitte Consulting
    LLP" off their own resume should still be spared a posting a board labelled
    "Deloitte".

    The remaining cost is a shorter employer that a longer unrelated one begins
    with — "Apple" against Apple Bank. That trade is taken deliberately, and it
    runs the other way from the one this module's docstring describes for the
    *screening* gates. An over-exclusion loses one employer's postings and says
    so out loud: the reason is stored on the posting and shown to the user, who
    can read it and edit the list. An under-exclusion is silent, and what it
    lets through is an application to the employer the candidate named — sent
    from their own mailbox, to the company they currently work for.
    """
    if not listed or not posting:
        return False
    short, long = sorted((listed, posting), key=len)
    return long[: len(short)] == short


def excluded_company_gate(targeting: Targeting, posting: JobPosting) -> str | None:
    """Veto an automatic application to a company the candidate ruled out.

    Compared on normalized names, so the list the user typed does not have to
    match the board's spelling of the legal entity — see :func:`_same_employer`
    for how far that stretches and why it stops where it does.
    """
    excluded = stated(getattr(targeting, "excluded_companies", None))
    if not excluded or not posting.company:
        return None

    key = normalize_company(posting.company).split()
    if not key:
        return None
    for raw in excluded:
        listed = normalize_company(raw).split()
        if not _same_employer(listed, key):
            continue
        if listed == key:
            return f"{posting.company} is on your excluded-companies list"
        # Named, because the two spellings differ and "Meta Platforms, Inc. is
        # on your excluded-companies list" is not a sentence the user can check
        # against a list that says "Meta".
        return (
            f'{posting.company} reads as "{raw.strip()}" on your '
            f"excluded-companies list"
        )
    return None


# --------------------------------------------------------------------------- #
# Shared                                                                      #
# --------------------------------------------------------------------------- #


def stated(value: object) -> list[str]:
    """Non-empty strings from a JSON list column that may be ``None``.

    Public because a caller deciding whether a filter is worth a database read
    needs to ask the same question the gate asks, and answering it with
    ``if targeting.company_sizes`` would count a stored ``[""]`` as an opinion
    while the gate does not.

    Every caller here reads a column that is ``NOT NULL`` in the models and has
    held ``NULL`` in production (see migration ``b2d7f4c81a95``), so the guard is
    not paranoia.
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _label(kind: str) -> str:
    return kind.replace("_", "-")


def _join(items) -> str:
    parts = list(items)
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + " or " + parts[-1]


__all__ = [
    "EMPLOYMENT_TYPES",
    "SIZE_TIERS",
    "company_size",
    "company_size_gate",
    "detect_employment_type",
    "employment_gate",
    "excluded_company_gate",
    "stated",
    "tier_for_bucket",
]
