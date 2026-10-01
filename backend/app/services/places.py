"""Are two free-text place names the same place?

One question, asked from two directions, and it was answered wrongly in both.
The fit scorer asks it to decide whether a posting sits somewhere the candidate
said they'd work — and the autopilot gate treats a "yes" as permission to send.
The deduper asks it to decide whether two postings from one employer are one
req — and a wrong "yes" marks a real job as a duplicate, which hides it from the
candidate's feed entirely.

Both used to answer it by intersecting the words of the two names, counting any
word longer than two characters. Half the gazetteer is a qualifier plus a name,
so that made "San Antonio" the same place as "San Francisco", "New Delhi" the
same as "New York", and "Kansas City" the same as "New York City".

The rule here is that the shared word has to actually name somewhere. Kept in
its own module because the two callers sit at different levels — the deduper is
deliberately free of service dependencies — and because a second copy of the
qualifier list is how the two would drift apart.
"""
from __future__ import annotations

import re
import unicodedata

_NON_WORD = re.compile(r"[^a-z0-9]+")

# Letters that carry no combining mark to strip, so :func:`fold_diacritics`
# cannot reach them by decomposing. Each is a letter in its own right in the
# alphabet it belongs to, and each is written as the ASCII letter below by every
# board that does not have it.
# Both cases, because this is a public helper now and a caller need not have
# lowercased first — both of the ones in this repo do.
_TRANSLITERATIONS = str.maketrans(
    {
        "ß": "ss", "ø": "o", "æ": "ae", "œ": "oe", "đ": "d",
        "ð": "d", "þ": "th", "ł": "l", "ŀ": "l", "ı": "i",
        "ẞ": "SS", "Ø": "O", "Æ": "AE", "Œ": "OE", "Đ": "D",
        "Ð": "D", "Þ": "TH", "Ł": "L", "Ŀ": "L", "İ": "I",
    }
)


def fold_diacritics(text: str) -> str:
    """"zürich" -> "zurich"; "montréal" -> "montreal"; "łódź" -> "lodz".

    :data:`_NON_WORD` keeps ``a-z0-9`` and turns *everything else* into a
    space, which for an accented letter meant cutting the word in half. Not
    failing to match — matching something else: "Zürich" tokenised to
    ``{"rich"}``, "München" to ``{"nchen"}``, "Montréal" to ``{"montr"}`` and
    "Łódź" to nothing at all, because each fragment left over was either a
    different word or too short to keep.

    Both callers act on the result and both were wrong in the silent direction.
    "Montréal, QC" against "Montreal, Canada" shares no token, so the deduper
    saw two reqs where an employer posted one — and :func:`fit_scorer
    .score_location` told a candidate in Montreal that a job in Montreal was
    somewhere else. The fragments cut the other way too: ``{"rich"}`` is a real
    word, and a token that short matching by accident is how a wrong "yes"
    happens.

    Boards emit both spellings of the same city interchangeably — the employer
    writes "München" and the aggregator that scraped it writes "Munich" — so
    the ASCII form is the one every table in this module is written in, and it
    is the one to fold onto. That reaches the subdivision tables too: "Québec"
    decomposed to "qu bec" and named no province.
    """
    # An ASCII string has nothing to fold, and answering that in C is what makes
    # this affordable on the hot paths that now call it — `matches_query` folds
    # every posting *body* a scan looks at, several thousand characters each,
    # hundreds of postings per run. The work below is a Python-level loop over
    # every character; the check above skips it for the majority of the feed.
    if text.isascii():
        return text
    decomposed = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATIONS))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _words(value: str | None) -> str:
    """*value* lowercased, unaccented, and with punctuation flattened to spaces."""
    return _NON_WORD.sub(" ", fold_diacritics((value or "").lower()))

# Words that qualify a place name without identifying a place. "San" is not a
# city, and neither is "New", "Fort", "City" or "Area".
#
# The list errs long on purpose. A word wrongly listed here costs a match the
# candidate would have wanted — a posting scores as a miss, or two copies of one
# job both stay in the feed — and both of those are visible and recoverable. A
# word wrongly left off sends an application to a city they never named, or
# deletes a job they were never shown. Only the second kind is silent.
PLACE_QUALIFIERS = frozenset(
    {
        # Directional and comparative qualifiers.
        "new", "north", "south", "east", "west", "northern", "southern",
        "eastern", "western", "central", "upper", "lower", "greater", "grand",
        "great", "old", "big", "little",
        # Saint/San family and romance articles, as boards write them.
        "saint", "san", "santa", "santo", "sao", "ste", "los", "las", "les",
        "del", "des", "the",
        # Landscape words that head or tail half the gazetteer.
        "fort", "port", "mount", "lake", "cape", "valley", "beach", "springs",
        "spring", "park", "hills", "hill", "heights", "falls", "creek",
        "grove", "ridge", "harbor", "harbour", "point", "island", "islands",
        # Administrative and shape-of-the-place words.
        "city", "cities", "town", "township", "village", "area", "areas",
        "region", "county", "district", "state", "province", "metro",
        "metropolitan", "downtown", "borough", "municipality", "prefecture",
        "territory", "center", "centre",
    }
)


def place_tokens(value: str | None) -> set[str]:
    """The words in a place name worth matching on.

    Two characters or fewer are dropped, which loses US state codes ("CA") and
    is the right trade: "CA" also appears inside enough noise that matching it
    would marry San Francisco to Sacramento and to nothing useful. Full names —
    "California", "San Francisco" — carry the comparison. Punctuation goes with
    them, so the "St." of "St. Louis" is the same two-letter abbreviation as the
    "St" of "St Louis" and is dropped alike.
    """
    return {w for w in _words(value).split() if len(w) > 2}


def places_overlap(left: str | None, right: str | None) -> bool:
    """Whether two place names are plausibly the same place.

    The evidence is a shared *significant* word — one outside
    :data:`PLACE_QUALIFIERS`. "Greater London Area" and "London, UK" share
    "london"; "San Antonio" and "San Francisco" share only "san", which names
    nowhere.

    Where neither name has a significant word left — "Santa Fe", whose second
    word is too short to keep — any shared word is all there is to go on, and is
    accepted. That fallback stays narrow: it needs *both* names to be qualifiers
    all the way down, so "Santa Fe" still doesn't reach "Santa Clara".
    """
    left_tokens, right_tokens = place_tokens(left), place_tokens(right)
    shared = left_tokens & right_tokens
    if not shared:
        return False
    if _contradicts(left, right):
        return False
    if shared - PLACE_QUALIFIERS:
        return True
    return not (left_tokens - PLACE_QUALIFIERS) and not (right_tokens - PLACE_QUALIFIERS)


def _contradicts(left: str | None, right: str | None) -> bool:
    """Whether the two names place themselves in *different* countries or regions.

    A shared word is evidence, not proof, and the world reuses city names: there
    is a Cambridge in Massachusetts and one in England, a London in Ontario, a
    Paris in Texas, a Dublin in Ohio, a Birmingham in Alabama. Every one of
    those pairs shares its only significant word, so the word test alone called
    them the same place — and both callers act on that:

    * the deduper marked a company's Cambridge, UK req as a duplicate of its
      Cambridge, MA one and took a real job out of the candidate's feed, which
      is the silent failure this module's docstring names;
    * :func:`fit_scorer.score_location` scored it 1.0 and said "Both in
      Cambridge" to a candidate in Massachusetts about a job in England — a
      dimension that feeds the score gating autopilot sending.

    Only ever turns a True into a False, never the reverse, which is what keeps
    it clear of the containment trap the comment below warns about: two places
    inside one country still have to share a word of their own to match, so
    Austin and Boston are no closer than they were.

    Both sides must actually say where they are. A bare "Cambridge" contradicts
    nothing and is left to the word test, because an unqualified name is the
    ordinary way a board writes the city an employer is already known to be in.

    Nor does a disagreement that rests entirely on one of :data:`_AMBIGUOUS_TAILS`.
    "Berlin, DE" resolves to Delaware because "DE" is a state and ``locate``
    defaults that way; against "Berlin, Germany" that read as two countries and
    split one req in two. The tell is that the contested code *is* the ISO code
    of the country the other side names outright — which is much better evidence
    that the board wrote the country than that a German city moved to Delaware.
    Same for "Toronto, CA" against "Toronto, Canada", "Pune, IN" against "Pune,
    India", "Amsterdam, NL" against "Amsterdam, Netherlands".
    """
    left_country, left_region = locate(left)
    right_country, right_region = locate(right)
    if left_country and right_country and left_country != right_country:
        # Unless the contested tail is the other side's country said twice, which
        # the docstring's "Berlin, DE" case explains.
        return not (_tail_reads_as(left, right_country) or _tail_reads_as(right, left_country))
    return bool(left_region and right_region and left_region != right_region)


def _tail_reads_as(value: str | None, country: str | None) -> bool:
    """Whether *value* ends in a code that is *country* as easily as a subdivision."""
    return bool(country) and _tail(value) == country and country in _AMBIGUOUS_TAILS


# --------------------------------------------------------------------------- #
# Does a named area contain a place?                                           #
# --------------------------------------------------------------------------- #
#
# A second question, and asking it with :func:`places_overlap` is what made a
# candidate who wrote "usa" invisible to every job in America. Overlap is
# symmetric and word-shaped — the right question for the deduper, which asks
# whether two postings are the *same* req. Targeting asks something else and
# directional: the candidate named an area, and the job sits somewhere; is the
# somewhere *inside* the area? "usa" and "Concord, CA" share no word and never
# will, so no amount of tuning the token rules reaches it. It needs to know that
# CA is a state and that the state is in the country.
#
# Deliberately not folded into ``places_overlap``. The deduper would inherit it,
# and then a role in Austin and a role in Boston would be "the same place"
# because both are in the USA — which is how a real job gets deleted from
# somebody's feed as a duplicate. The two questions keep two functions.

# Countries this recognises by name, as the tokens their aliases reduce to.
# Written token-wise because that is how the strings arrive: "U.S.A.", "U.S.",
# "United States of America" and "usa" all land on the same set — which is true
# only because :func:`fold_initials` puts the dotted forms back together first.
# Without it "U.S.A." flattened to the three separate words "u s a", matched
# nothing here, and a candidate who wrote it was offered no job in America: the
# same failure the bare-"usa" fix addressed, arriving through the punctuation
# instead of through the token rules.
#
# ``"us"`` earns its place for the same reason. It is the single most common way
# to write the country and it was the one spelling missing — "usa" was listed,
# "us" was not, so ``US`` and ``U.S.`` both fell through to False while ``UK``
# (listed) worked. Short enough to be worth checking against
# :func:`locate`'s containment test, and it is safe there: the match is on a
# whole word inside a *location* string, where "us" is never the pronoun.
_COUNTRY_ALIASES: dict[str, frozenset[str]] = {
    "us": frozenset(
        {"us", "usa", "america", "united states", "united states of america"}
    ),
    "ca": frozenset({"canada"}),
    "gb": frozenset(
        {"uk", "united kingdom", "britain", "great britain", "england",
         "scotland", "wales", "northern ireland"}
    ),
    "in": frozenset({"india"}),
    "au": frozenset({"australia"}),
    "de": frozenset({"germany", "deutschland"}),
    "fr": frozenset({"france"}),
    "ie": frozenset({"ireland"}),
    "nl": frozenset({"netherlands", "holland"}),
    "es": frozenset({"spain"}),
    "sg": frozenset({"singapore"}),
    "ae": frozenset({"uae", "united arab emirates"}),
}

# Subdivisions, for the two countries whose mail this product actually sees. The
# postal code and the full name both appear in recruiter subject lines — "AI
# Engineer, Owings Mill, MD" and "Toronto, Ontario" are the same convention seen
# from two ends — so both are indexed.
_US_STATES: dict[str, str] = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska",
    "nv": "nevada", "nh": "new hampshire", "nj": "new jersey",
    "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon",
    "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
    "dc": "district of columbia",
}
_CA_PROVINCES: dict[str, str] = {
    "ab": "alberta", "bc": "british columbia", "mb": "manitoba",
    "nb": "new brunswick", "nl": "newfoundland", "ns": "nova scotia",
    "nt": "northwest territories", "nu": "nunavut", "on": "ontario",
    "pe": "prince edward island", "qc": "quebec", "sk": "saskatchewan",
    "yt": "yukon",
}
_SUBDIVISIONS: dict[str, dict[str, str]] = {"us": _US_STATES, "ca": _CA_PROVINCES}

# "City, ST" — the one place a two-letter code may be read as a subdivision.
# Anchored to a comma and the end of a segment because a bare two-letter word is
# far too cheap: "On-site in London" has an "on" in it, and reading that as
# Ontario would send a Canadian veto to a job in England.
_POSTAL_TAIL = re.compile(r",\s*([a-z]{2})\b\s*(?:[,(]|$)")

# The same slot holds ISO country codes, because boards write "Paris, FR" and
# "Dublin, IE" exactly the way they write "Austin, TX". Read as a country only
# where the code cannot also be a subdivision, so nothing that used to resolve
# to a state stops doing so.
_COUNTRY_TAILS = frozenset(
    code
    for code in _COUNTRY_ALIASES
    if code not in _US_STATES and code not in _CA_PROVINCES
)

# And the four that *are* both, which no amount of table work can separate:
# "CA" is California and Canada, "IN" Indiana and India, "DE" Delaware and
# Germany, "NL" Newfoundland and the Netherlands. :func:`locate` keeps reading
# these as the subdivision, which is the right default for this product's mail
# — most of it is American — but it is a default, not a reading, and
# :func:`_contradicts` is told so before it vetoes anything on the strength of
# one.
_AMBIGUOUS_TAILS = frozenset(_COUNTRY_ALIASES) - _COUNTRY_TAILS


def _tail(value: str | None) -> str | None:
    """The two-letter code on the end of *value*, if it has one."""
    match = _POSTAL_TAIL.search((value or "").lower())
    return match.group(1) if match else None


# A run of single letters, which is what a dotted acronym becomes once the
# punctuation is flattened. Two or more, so an ordinary short word standing
# beside a longer one is untouched.
_INITIALS = re.compile(r"(?<![a-z0-9])(?:[a-z] )+[a-z](?![a-z0-9])")


def fold_initials(text: str) -> str:
    """Put a dotted acronym back together: ``"u s a"`` -> ``"usa"``.

    ``_NON_WORD`` turns every separator into a space, which is right for
    "San Francisco, CA" and wrong for "U.S.A." — the dots are *inside* the word,
    and flattening them scattered the country into three letters that match
    nothing. Every alias table below is written in the joined form, so joining
    here is what lets a person write the country the way people write it.

    Only runs of single letters are joined, so "Owings Mill, MD" and
    "San Jose, CA" keep their shape; there is nothing to join in either.
    """
    return _INITIALS.sub(lambda m: m.group(0).replace(" ", ""), text)


def _phrases(value: str | None) -> str:
    """The string reduced to lowercase words, punctuation flattened to spaces."""
    return f" {fold_initials(_words(value).strip())} "


def names_country(value: str | None) -> str | None:
    """The country *value* names outright, or ``None`` if it names somewhere smaller.

    Strict on purpose: this answers "did the candidate say a whole country?", and
    a yes widens the gate to everything inside it. "usa" and "United States" are
    yes; "New York, USA" is not — it names a city, and the country on the end of
    it is an address, not a preference.
    """
    text = _phrases(value).strip()
    if not text:
        return None
    for code, aliases in _COUNTRY_ALIASES.items():
        if text in aliases:
            return code
    return None


# Every spelling of a subdivision that names *only* that subdivision, mapped to
# it. The bare name ("california"), and the name with its own country written
# after or before it ("california usa", "usa california").
#
# The country suffix is the whole point. Boards and humans both write an area
# from the inside out — "California, United States", "Ontario, Canada" — and
# matching the bare name alone read those as naming neither: not a region,
# because of the tail, and not a country, because :func:`names_country` is
# strict about a string that names somewhere smaller. So ``place_covers``
# returned False for every job in the state, which is exactly the failure the
# bare-"usa" fix addressed one level up, arriving through the spelling instead
# of through the token rules. A candidate who wrote "Texas, United States" was
# told that no job in Austin was one of their locations.
#
# Only the subdivision's *own* country is allowed to appear. "Ontario, USA" is
# not a place and must not resolve to Ontario; it falls through to False, which
# is what this module does with anything it cannot name.
def _region_phrases() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for country, table in _SUBDIVISIONS.items():
        for code, name in table.items():
            out[name] = (country, code)
            for alias in _COUNTRY_ALIASES.get(country, frozenset()):
                out.setdefault(f"{name} {alias}", (country, code))
                out.setdefault(f"{alias} {name}", (country, code))
    return out


_REGION_PHRASES: dict[str, tuple[str, str]] = _region_phrases()


def names_region(value: str | None) -> tuple[str, str] | None:
    """The ``(country, subdivision)`` *value* names outright, or ``None``.

    Same contract as :func:`names_country` one level down, so a candidate who
    wrote "California" is offered San Diego — and so is one who wrote
    "California, USA", where the country on the end is an address tail rather
    than a second, wider preference. A bare postal code is *not* accepted here —
    "CA" alone is as likely to be Canada as California, and a preference row is
    somewhere a person can afford to write the word out.

    Strict in the same way :func:`names_country` is: the string has to name the
    subdivision and nothing smaller. "San Diego, California" is a city and is
    not accepted, because widening it to the state would offer the candidate
    every job 500 miles away from the one place they named.
    """
    return _REGION_PHRASES.get(_phrases(value).strip())


def locate(value: str | None) -> tuple[str | None, str | None]:
    """Where *value* is, as far as the names in it give it away.

    Returns ``(country, subdivision)``, either of which may be ``None``. The
    country is looked for first and directly — "AMD Canada" says so — and
    otherwise inferred from a subdivision, because "Owings Mill, MD" never
    mentions the country it is obviously in.
    """
    text = _phrases(value)
    if not text.strip():
        return None, None

    country: str | None = None
    for code, aliases in _COUNTRY_ALIASES.items():
        if any(f" {alias} " in text for alias in aliases):
            country = code
            break

    for owner, table in _SUBDIVISIONS.items():
        for code, name in table.items():
            if f" {name} " in text:
                return (country or owner), code

    # On the raw string rather than the flattened one: the comma is the evidence.
    code = _tail(value)
    if code:
        for owner, table in _SUBDIVISIONS.items():
            # A country already read off the string wins the tie: "Windsor, ON,
            # Canada" and "Ontario, CA" are the two ways this is ambiguous, and
            # the explicit country is the better evidence in both.
            if code in table and (country is None or country == owner):
                return (country or owner), code
        # Not a subdivision anywhere, so the same slot is free to be what the
        # rest of the world puts in it: "Paris, FR", "Dublin, IE", "Sydney, AU".
        # These used to resolve to nowhere at all, which is how "Paris, FR" and
        # "Paris, TX" stayed the same place.
        if country is None and code in _COUNTRY_TAILS:
            return code, None
    return country, None


def place_covers(wanted: str | None, actual: str | None) -> bool:
    """Whether an area the candidate named contains the place a job is in.

    Three ways to be inside somewhere, narrowest first: it is the same place; the
    candidate named a subdivision and the job is in it; the candidate named a
    country and the job is in it. Anything else is ``False`` — this widens the
    gate, so every branch has to be evidence rather than the absence of it.
    """
    if places_overlap(wanted, actual):
        return True

    region = names_region(wanted)
    if region is not None:
        return locate(actual) == region

    country = names_country(wanted)
    if country is not None:
        return locate(actual)[0] == country

    return False


# --------------------------------------------------------------------------- #
# A location field that names an arrangement instead of a place                #
# --------------------------------------------------------------------------- #
#
# "Remote" in the city slot is not a place, and every reader of a location
# field has to know that before it can compare anything. Four places in this
# codebase were asking that question, each off a private list, and each list
# was the same four or five English words.
#
# Which made the answer English-only, and this is a remote-first job feed. A
# French board writes "Télétravail" where the city goes, a German one
# "Homeoffice", a Polish one "Praca zdalna", a Swedish one "På distans" — and
# every one of those came back "not remote", which is the *disqualifying*
# answer on two paths at once:
#
# * :func:`app.services.fit_scorer.score_location` gives a remote role 1.0, and
#   that single rule is what makes a remote-only candidate's feed work at all.
#   Without it the posting is judged against the places the candidate named,
#   and a working arrangement matches none of them: 0.15, or 0.05 for a
#   remote-only candidate.
# * :func:`app.services.auto_apply_service.location_gate` reads the same
#   answer. "Remote passes, always" never fired, so a remote-only candidate had
#   every European posting vetoed as on-site.
#
# It also reads the candidate's *own* preferred-location list, so someone who
# typed "Télétravail" there had it treated as a city that nothing matches.
#
# The two readers of a location *field* share this now —
# :func:`app.services.fit_scorer.looks_remote` and
# :func:`app.services.ats_boards._looks_remote`. The other two keep their own
# deliberately. `job_dedup._REMOTE_WORDS` collapses a location onto the single
# token "remote" so two postings can be compared, so a longer list merges more
# postings — and a wrong merge marks a real job a duplicate and hides it from
# the feed, which needs its own evidence rather than a shared table.
# `salary_service._REMOTE_WORDS` is read against free text rather than a field,
# where the words below stop being unambiguous.
#
# Matched on word boundaries rather than by containment, which is what lets the
# stems below be stems: `zdaln\w*` covers "zdalna", "zdalnie" and "zdalny"
# without `remot` quietly claiming a word that merely starts that way.
#
# Only spellings that name the arrangement and nothing else — the same bar
# :data:`app.services.search_filters._LOCALISED_PHRASES` sets, though the
# asymmetry behind it runs the other way here. This reads a location field, not
# a description: the field holds a place or an arrangement and nothing else, so
# there is no prose for a word to be borrowed from. Italian "smart working" is
# still left out, because in Italy it is written for hybrid roles as often as
# for remote ones, and hybrid is not what any caller here means.
_REMOTE_PLACE_RE = re.compile(
    # en, and the Romance spellings of the same word, which differ only in the
    # ending: "remoto", "remota", "remotamente". Spelt out rather than stemmed
    # off "remot", because a stem that short claims "remoteness" too.
    r"\bremote(?:ly)?\b|\bremot[oa]s?\b|\bremotamente\b"
    r"|\banywhere\b|\bworldwide\b|\bdistributed\b"
    r"|\bwork\s+from\s+home\b|\bwfh\b"
    # fr
    r"|\bteletravail\b|\ba\s+distance\b"
    # de
    r"|\bhome\s*-?\s*office\b|\bortsunabhangig\w*\b|\bfernarbeit\b"
    # es, pt, it — "remoto"/"remota" are already covered by the stem above.
    r"|\bteletrabajo\b|\bteletrabalho\b|\ba\s+distancia\b"
    r"|\blavoro\s+agile\b"
    # nl
    r"|\bthuiswerk\w*\b|\bop\s+afstand\b"
    # pl
    r"|\bzdaln\w*\b"
    # sv, no, da
    r"|\bdistansarbete\b|\bpa\s+distans\b|\bhjemmekontor\b"
    r"|\bhjemmefra\b|\bfjernarbeid\b"
    # fi
    r"|\betatyo\w*\b|\betana\b"
    # cs, sk
    r"|\bna\s+dalku\b|\bvzdalen\w*\b"
    # hu
    r"|\btavmunka\b"
    # ru, uk — outside the Latin alphabet. `fold_diacritics` still reaches
    # *inside* these: it decomposes, so "удалённо" folds to "удаленно" and the
    # stem needs the plain "е". What it cannot reach is Cyrillic "і" (U+0456),
    # which is a letter in its own right rather than an accented one — and it
    # is a different character from the Latin "i" it is drawn identically to.
    # Written as an escape here so the two cannot be confused by eye; a Latin
    # "i" in this word is a stem that matches nothing, silently, forever.
    r"|удален\w*|дистанц\w*|в\u0456ддален\w*",
)


def names_arrangement(location: str | None) -> bool:
    """Whether a location string is really a working arrangement.

    Folded before it is read, because half these words carry an accent and an
    ASCII pattern cannot see one: "Télétravail" lower-cases to itself, and
    every board that writes it writes it that way. The same fold every table in
    this module is written against — see :func:`fold_diacritics`.
    """
    return bool(_REMOTE_PLACE_RE.search(fold_diacritics((location or "").lower())))


__all__ = [
    "PLACE_QUALIFIERS",
    "fold_diacritics",
    "fold_initials",
    "locate",
    "names_arrangement",
    "names_country",
    "names_region",
    "place_covers",
    "place_tokens",
    "places_overlap",
]
