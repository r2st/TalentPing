"""When a given message should go out — the recipient's morning, not ours.

Two schedulers used to decide send timing independently and neither knew where
the recipient was. ``enqueue_campaign_sends`` spaced messages 90-600s apart from
whenever the user pressed start, so a campaign launched at 23:00 trickled out
through the night. ``follow_up_service.optimal_send_time`` snapped follow-ups to
the next Tue-Thu 06:00-10:00 slot in **UTC** — which is 22:00 the previous day in
San Francisco, reliably the worst slot available for a Bay Area recruiter.

This module is the single answer to "when should this message go out?", and both
schedulers now call it:

* :func:`resolve_timezone` — the recipient's IANA zone, from the best location
  data available, cached back onto the recruiter row.
* :func:`next_slot` — the next weekday 09:00-11:00 in that zone, returned in UTC.

No network, no new dependency: ``zoneinfo`` ships with Python and the IANA
database comes with the platform. Resolution is a deterministic table lookup over
free-text location strings, which is worth being honest about — it covers the
cities and countries this product's users actually target, and falls through to
UTC rather than guessing when it doesn't recognise something.
"""
from __future__ import annotations

import logging
import random
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.places import fold_diacritics

logger = logging.getLogger(__name__)

WEEKDAYS = (0, 1, 2, 3, 4)  # Monday..Friday

# A slot is never scheduled further out than this. Unreachable in practice — a
# weekday morning is always within four days — but a bad timezone must not be
# able to park a Celery task in the broker for a month.
MAX_SEND_DELAY_SECONDS = 7 * 24 * 3600


def _zone(name: str | None) -> ZoneInfo | None:
    """A ZoneInfo for *name*, or None when it isn't a real IANA zone."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def default_timezone() -> ZoneInfo:
    """The configured fallback zone, degrading to UTC on a bad setting."""
    zone = _zone(settings.default_send_timezone)
    if zone is None:
        logger.warning(
            "DEFAULT_SEND_TIMEZONE %r is not a valid IANA zone; using UTC",
            settings.default_send_timezone,
        )
        return ZoneInfo("UTC")
    return zone


# --------------------------------------------------------------------------- #
# Location -> timezone                                                         #
# --------------------------------------------------------------------------- #

# Free-text location tokens mapped to IANA zones, in three tiers. The tier is
# what resolves a string that names more than one place: "Los Angeles,
# California, United States" names all three, and only the city is the answer.
# Matching is on word boundaries so "Bangor" doesn't match "ban", and
# longest-first inside a tier so "Portland, Oregon" isn't caught by a bare "or".
#
# **Written unaccented, because the lookup folds the accents off the string.**
# The word boundary is ``(?<![a-z0-9])`` — an accented letter is not in that
# class, so "Montréal" carried a boundary in the middle of itself and the key
# "montreal" matched nothing. Every European and Latin American city in this
# table has an accent in its own spelling, and a board that scraped the
# employer's own wording emits that spelling: "Genève", "Kraków", "Düsseldorf",
# "Bogotá", "Québec".
#
# What that costs is the whole point of the module. An unresolved location
# falls through to `default_timezone`, `next_slot` puts the send in the 09:00
# window of *that* zone, and the recipient gets it at 04:00 — the exact failure
# the docstring above opens with, arriving through the spelling instead of
# through the scheduler.
#
# Three keys used to be spelt with their accents ("são paulo", "münchen",
# "zürich"), added one at a time as each city presumably came up. Two were
# exact duplicates of the ASCII key beside them and the third is kept as
# "munchen", which is now what the fold produces. A per-city patch is what a
# table does instead of folding, and it stops at the cities somebody noticed.
#
# Folding the accents off is only half of it, though: "Wien" and "Vienna" have
# no letter in common to fold. Where a city has an English exonym the table was
# written in the exonym alone, and the endonym is what the employer writes on
# their own careers page — which is where `_zone_from_company_profile` reads
# the headquarters from. So each of those keys carries its own name beside the
# English one: wien, praha, warszawa, lisboa, roma, milano, bruxelles, geneve,
# kobenhavn, bucuresti, ciudad de mexico.
#
# Only for cities already in the table, and only where the two names mean the
# same city — this adds no zone and makes no new judgement about where anywhere
# is. A bare endonym used to resolve to nothing; "Wien, Österreich" happened to
# work, but only because the *country* tier caught it three tiers down.
#
# Tier 1 — cities. The most specific thing a location string can name.
_CITY_ZONES: dict[str, str] = {
    # ---- North America ----
    "san francisco": "America/Los_Angeles",
    "sf bay area": "America/Los_Angeles",
    "bay area": "America/Los_Angeles",
    "silicon valley": "America/Los_Angeles",
    "palo alto": "America/Los_Angeles",
    "mountain view": "America/Los_Angeles",
    "san jose": "America/Los_Angeles",
    "oakland": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "san diego": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "portland": "America/Los_Angeles",
    "vancouver": "America/Vancouver",
    "denver": "America/Denver",
    "boulder": "America/Denver",
    "salt lake city": "America/Denver",
    "phoenix": "America/Phoenix",
    "austin": "America/Chicago",
    "dallas": "America/Chicago",
    "houston": "America/Chicago",
    "chicago": "America/Chicago",
    "minneapolis": "America/Chicago",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "brooklyn": "America/New_York",
    "boston": "America/New_York",
    "cambridge, ma": "America/New_York",
    "philadelphia": "America/New_York",
    "washington": "America/New_York",
    "atlanta": "America/New_York",
    "miami": "America/New_York",
    "toronto": "America/Toronto",
    "ottawa": "America/Toronto",
    "montreal": "America/Toronto",
    "mexico city": "America/Mexico_City",
    "ciudad de mexico": "America/Mexico_City",
    "cdmx": "America/Mexico_City",
    # ---- South America ----
    "sao paulo": "America/Sao_Paulo",
    "rio de janeiro": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "bogota": "America/Bogota",
    "santiago": "America/Santiago",
    # ---- Europe ----
    "london": "Europe/London",
    "manchester": "Europe/London",
    "edinburgh": "Europe/London",
    "cambridge, uk": "Europe/London",
    "dublin": "Europe/Dublin",
    "lisbon": "Europe/Lisbon",
    "lisboa": "Europe/Lisbon",
    "porto": "Europe/Lisbon",
    "madrid": "Europe/Madrid",
    "barcelona": "Europe/Madrid",
    "paris": "Europe/Paris",
    "amsterdam": "Europe/Amsterdam",
    "rotterdam": "Europe/Amsterdam",
    "brussels": "Europe/Brussels",
    "bruxelles": "Europe/Brussels",
    "brussel": "Europe/Brussels",
    "berlin": "Europe/Berlin",
    "munich": "Europe/Berlin",
    "munchen": "Europe/Berlin",
    "hamburg": "Europe/Berlin",
    "frankfurt": "Europe/Berlin",
    "zurich": "Europe/Zurich",
    "geneva": "Europe/Zurich",
    "geneve": "Europe/Zurich",
    "vienna": "Europe/Vienna",
    "wien": "Europe/Vienna",
    "milan": "Europe/Rome",
    "milano": "Europe/Rome",
    "rome": "Europe/Rome",
    "roma": "Europe/Rome",
    "copenhagen": "Europe/Copenhagen",
    "kobenhavn": "Europe/Copenhagen",
    "stockholm": "Europe/Stockholm",
    "oslo": "Europe/Oslo",
    "helsinki": "Europe/Helsinki",
    "warsaw": "Europe/Warsaw",
    "warszawa": "Europe/Warsaw",
    "krakow": "Europe/Warsaw",
    "prague": "Europe/Prague",
    "praha": "Europe/Prague",
    "budapest": "Europe/Budapest",
    "bucharest": "Europe/Bucharest",
    "bucuresti": "Europe/Bucharest",
    "athens": "Europe/Athens",
    "istanbul": "Europe/Istanbul",
    # ---- Middle East / Africa ----
    "tel aviv": "Asia/Jerusalem",
    "jerusalem": "Asia/Jerusalem",
    "dubai": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    "cairo": "Africa/Cairo",
    "nairobi": "Africa/Nairobi",
    "lagos": "Africa/Lagos",
    "cape town": "Africa/Johannesburg",
    "johannesburg": "Africa/Johannesburg",
    # ---- Asia / Pacific ----
    "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "gurgaon": "Asia/Kolkata",
    "gurugram": "Asia/Kolkata",
    "pune": "Asia/Kolkata",
    "chennai": "Asia/Kolkata",
    "karachi": "Asia/Karachi",
    "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
    "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "shenzhen": "Asia/Shanghai",
    "seoul": "Asia/Seoul",
    "tokyo": "Asia/Tokyo",
    "osaka": "Asia/Tokyo",
    "jakarta": "Asia/Jakarta",
    "manila": "Asia/Manila",
    "bangkok": "Asia/Bangkok",
    "ho chi minh": "Asia/Ho_Chi_Minh",
    "saigon": "Asia/Ho_Chi_Minh",
    "hanoi": "Asia/Ho_Chi_Minh",
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth",
    # Adelaide, Canberra, Hobart and Darwin are the other four capitals, and
    # the table stopped at four so all four resolved to nothing: an Australian
    # recruiter fell through to `default_timezone` and was written to in the
    # sender's morning, which is the middle of their night.
    #
    # Darwin also has to be here for a second reason — see "nt" in
    # :data:`_AMBIGUOUS_SUBDIVISION_CODES`. It is the city that settles the
    # code, so without it the fix there resolves nothing.
    "adelaide": "Australia/Adelaide",
    "canberra": "Australia/Sydney",
    "hobart": "Australia/Hobart",
    "darwin": "Australia/Darwin",
    "gold coast": "Australia/Brisbane",
    "wollongong": "Australia/Sydney",
    "geelong": "Australia/Melbourne",
    "auckland": "Pacific/Auckland",
    "wellington": "Pacific/Auckland",
    "christchurch": "Pacific/Auckland",
}

# Tier 2 — states, regions and timezone abbreviations. Coarser than a city and
# often spanning several zones, so a city named alongside one wins.
_REGION_ZONES: dict[str, str] = {
    "california": "America/Los_Angeles",
    "washington state": "America/Los_Angeles",
    "oregon": "America/Los_Angeles",
    "nevada": "America/Los_Angeles",
    "colorado": "America/Denver",
    "utah": "America/Denver",
    "arizona": "America/Phoenix",
    "texas": "America/Chicago",
    "illinois": "America/Chicago",
    "minnesota": "America/Chicago",
    "new york state": "America/New_York",
    "massachusetts": "America/New_York",
    "new jersey": "America/New_York",
    "pennsylvania": "America/New_York",
    "georgia": "America/New_York",
    "florida": "America/New_York",
    "north carolina": "America/New_York",
    "virginia": "America/New_York",
    "pacific time": "America/Los_Angeles",
    "eastern time": "America/New_York",
    "central time": "America/Chicago",
    "mountain time": "America/Denver",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "est": "America/New_York",
    "edt": "America/New_York",
    "cst": "America/Chicago",
    "gmt": "Europe/London",
    "bst": "Europe/London",
    "cet": "Europe/Berlin",
    "ist": "Asia/Kolkata",
}

# Tier 3 — countries, the last thing to fall back on. The bare abbreviations
# matter more than they look: "Remote (US)" and "Remote, UK" are two of the most
# common location strings on a posting, and neither carries a city to match on.
# Word-boundary matching keeps them from firing inside longer words.
_COUNTRY_ZONES: dict[str, str] = {
    "united states": "America/New_York",
    "usa": "America/New_York",
    "u.s.": "America/New_York",
    "us": "America/New_York",
    "uk": "Europe/London",
    "u.k.": "Europe/London",
    "canada": "America/Toronto",
    "mexico": "America/Mexico_City",
    "brazil": "America/Sao_Paulo",
    "argentina": "America/Argentina/Buenos_Aires",
    "united kingdom": "Europe/London",
    "england": "Europe/London",
    "scotland": "Europe/London",
    "wales": "Europe/London",
    "ireland": "Europe/Dublin",
    "portugal": "Europe/Lisbon",
    "spain": "Europe/Madrid",
    "france": "Europe/Paris",
    "netherlands": "Europe/Amsterdam",
    "belgium": "Europe/Brussels",
    "germany": "Europe/Berlin",
    "switzerland": "Europe/Zurich",
    "austria": "Europe/Vienna",
    "italy": "Europe/Rome",
    "denmark": "Europe/Copenhagen",
    "sweden": "Europe/Stockholm",
    "norway": "Europe/Oslo",
    "finland": "Europe/Helsinki",
    "poland": "Europe/Warsaw",
    "czechia": "Europe/Prague",
    "czech republic": "Europe/Prague",
    "hungary": "Europe/Budapest",
    "romania": "Europe/Bucharest",
    "greece": "Europe/Athens",
    "turkey": "Europe/Istanbul",
    "israel": "Asia/Jerusalem",
    "united arab emirates": "Asia/Dubai",
    "egypt": "Africa/Cairo",
    "kenya": "Africa/Nairobi",
    "nigeria": "Africa/Lagos",
    "south africa": "Africa/Johannesburg",
    "india": "Asia/Kolkata",
    "pakistan": "Asia/Karachi",
    "china": "Asia/Shanghai",
    "south korea": "Asia/Seoul",
    "japan": "Asia/Tokyo",
    "indonesia": "Asia/Jakarta",
    "philippines": "Asia/Manila",
    "thailand": "Asia/Bangkok",
    "vietnam": "Asia/Ho_Chi_Minh",
    "malaysia": "Asia/Kuala_Lumpur",
    "australia": "Australia/Sydney",
    "new zealand": "Pacific/Auckland",
    "nz": "Pacific/Auckland",
}

# Country-code TLDs unambiguous enough to guess from an email address alone.
# Nothing generic (.com/.io/.ai/.co) — those say nothing about location.
_TLD_ZONES: dict[str, str] = {
    "uk": "Europe/London",
    "ie": "Europe/Dublin",
    "de": "Europe/Berlin",
    "fr": "Europe/Paris",
    "nl": "Europe/Amsterdam",
    "be": "Europe/Brussels",
    "es": "Europe/Madrid",
    "pt": "Europe/Lisbon",
    "it": "Europe/Rome",
    "ch": "Europe/Zurich",
    "at": "Europe/Vienna",
    "dk": "Europe/Copenhagen",
    "se": "Europe/Stockholm",
    "no": "Europe/Oslo",
    "fi": "Europe/Helsinki",
    "pl": "Europe/Warsaw",
    "cz": "Europe/Prague",
    "gr": "Europe/Athens",
    "tr": "Europe/Istanbul",
    "il": "Asia/Jerusalem",
    "ae": "Asia/Dubai",
    "za": "Africa/Johannesburg",
    "ng": "Africa/Lagos",
    "ke": "Africa/Nairobi",
    "in": "Asia/Kolkata",
    "sg": "Asia/Singapore",
    "hk": "Asia/Hong_Kong",
    "cn": "Asia/Shanghai",
    "jp": "Asia/Tokyo",
    "kr": "Asia/Seoul",
    "au": "Australia/Sydney",
    "nz": "Pacific/Auckland",
    "ca": "America/Toronto",
    "mx": "America/Mexico_City",
    "br": "America/Sao_Paulo",
    "ar": "America/Argentina/Buenos_Aires",
}

# --------------------------------------------------------------------------- #
# "City, ST" — the subdivision tail                                            #
# --------------------------------------------------------------------------- #

# Half the towns in America are named after somewhere in Europe, and the tier
# table above had no way to know it. "Rome, GA" resolved to Europe/Rome and
# "Melbourne, FL" to Australia/Melbourne — a fourteen-hour error, which is not a
# bad morning but the middle of the night. "Vienna, VA", "Berlin, NH",
# "Manchester, NH", "Dublin, OH", "Athens, GA" and "London, ON" all failed the
# same way, and "Portland, ME" landed in Oregon's timezone because Portland is a
# city key and Maine was in no table at all.
#
# The state on the end of the string is the evidence that settles it, so this
# table maps every US state and Canadian province — full name and postal code —
# to the zone most of its population lives in. Coarse by construction: Tennessee
# and Florida each straddle two zones, and the answer here is the one that is
# right for most of the state.
_SUBDIVISION_ZONES: dict[str, str] = {
    # ---- United States ----
    "alabama": "America/Chicago", "al": "America/Chicago",
    "alaska": "America/Anchorage", "ak": "America/Anchorage",
    "arizona": "America/Phoenix", "az": "America/Phoenix",
    "arkansas": "America/Chicago", "ar": "America/Chicago",
    "california": "America/Los_Angeles", "ca": "America/Los_Angeles",
    "colorado": "America/Denver", "co": "America/Denver",
    "connecticut": "America/New_York", "ct": "America/New_York",
    "delaware": "America/New_York", "de": "America/New_York",
    "florida": "America/New_York", "fl": "America/New_York",
    "georgia": "America/New_York", "ga": "America/New_York",
    "hawaii": "Pacific/Honolulu", "hi": "Pacific/Honolulu",
    "idaho": "America/Denver", "id": "America/Denver",
    "illinois": "America/Chicago", "il": "America/Chicago",
    "indiana": "America/New_York", "in": "America/New_York",
    "iowa": "America/Chicago", "ia": "America/Chicago",
    "kansas": "America/Chicago", "ks": "America/Chicago",
    "kentucky": "America/New_York", "ky": "America/New_York",
    "louisiana": "America/Chicago", "la": "America/Chicago",
    "maine": "America/New_York", "me": "America/New_York",
    "maryland": "America/New_York", "md": "America/New_York",
    "massachusetts": "America/New_York", "ma": "America/New_York",
    "michigan": "America/New_York", "mi": "America/New_York",
    "minnesota": "America/Chicago", "mn": "America/Chicago",
    "mississippi": "America/Chicago", "ms": "America/Chicago",
    "missouri": "America/Chicago", "mo": "America/Chicago",
    "montana": "America/Denver", "mt": "America/Denver",
    "nebraska": "America/Chicago", "ne": "America/Chicago",
    "nevada": "America/Los_Angeles", "nv": "America/Los_Angeles",
    "new hampshire": "America/New_York", "nh": "America/New_York",
    "new jersey": "America/New_York", "nj": "America/New_York",
    "new mexico": "America/Denver", "nm": "America/Denver",
    "new york": "America/New_York", "ny": "America/New_York",
    "north carolina": "America/New_York", "nc": "America/New_York",
    "north dakota": "America/Chicago", "nd": "America/Chicago",
    "ohio": "America/New_York", "oh": "America/New_York",
    "oklahoma": "America/Chicago", "ok": "America/Chicago",
    "oregon": "America/Los_Angeles", "or": "America/Los_Angeles",
    "pennsylvania": "America/New_York", "pa": "America/New_York",
    "rhode island": "America/New_York", "ri": "America/New_York",
    "south carolina": "America/New_York", "sc": "America/New_York",
    "south dakota": "America/Chicago", "sd": "America/Chicago",
    "tennessee": "America/Chicago", "tn": "America/Chicago",
    "texas": "America/Chicago", "tx": "America/Chicago",
    "utah": "America/Denver", "ut": "America/Denver",
    "vermont": "America/New_York", "vt": "America/New_York",
    "virginia": "America/New_York", "va": "America/New_York",
    "washington": "America/Los_Angeles", "wa": "America/Los_Angeles",
    "west virginia": "America/New_York", "wv": "America/New_York",
    "wisconsin": "America/Chicago", "wi": "America/Chicago",
    "wyoming": "America/Denver", "wy": "America/Denver",
    "district of columbia": "America/New_York", "dc": "America/New_York",
    # ---- Canada ----
    "alberta": "America/Edmonton", "ab": "America/Edmonton",
    "british columbia": "America/Vancouver", "bc": "America/Vancouver",
    "manitoba": "America/Winnipeg", "mb": "America/Winnipeg",
    "new brunswick": "America/Moncton", "nb": "America/Moncton",
    "newfoundland": "America/St_Johns", "nl": "America/St_Johns",
    "nova scotia": "America/Halifax", "ns": "America/Halifax",
    "northwest territories": "America/Edmonton", "nt": "America/Edmonton",
    "nunavut": "America/Iqaluit", "nu": "America/Iqaluit",
    "ontario": "America/Toronto", "on": "America/Toronto",
    "prince edward island": "America/Halifax", "pe": "America/Halifax",
    "quebec": "America/Toronto", "qc": "America/Toronto",
    "saskatchewan": "America/Regina", "sk": "America/Regina",
    "yukon": "America/Whitehorse", "yt": "America/Whitehorse",
    # ---- Australia ----
    # An Australian posting writes its location the same way an American one
    # does — "Newcastle, NSW", "Geelong, VIC", "Townsville, QLD" — and none of
    # those codes were here, so the tail said nothing and the string fell
    # through to whatever city happened to be listed. Most of these towns are
    # not in the city table, so most of them resolved to nothing at all.
    #
    # "vic" rather than "victoria", and no bare "sa". Victoria is a city in
    # British Columbia before it is a state, and "SA" is South Africa to as
    # many people as it is South Australia — "Durban, SA" has no city here to
    # correct it, so admitting the code would move a Johannesburg recruiter to
    # Adelaide. Adelaide is reached through the city instead, which is the same
    # route Perth already took.
    "new south wales": "Australia/Sydney", "nsw": "Australia/Sydney",
    "vic": "Australia/Melbourne",
    "queensland": "Australia/Brisbane", "qld": "Australia/Brisbane",
    "south australia": "Australia/Adelaide",
    "western australia": "Australia/Perth",
    "tasmania": "Australia/Hobart", "tas": "Australia/Hobart",
    "northern territory": "Australia/Darwin",
    "australian capital territory": "Australia/Sydney",
}

# The postal codes that are also somebody's country code. "Berlin, DE" is in
# Germany and "Wilmington, DE" is in Delaware, and no table can separate those
# on the code alone — so these read as the subdivision only when nothing else in
# the string was recognised. Everything not listed here is decisive, which is
# what lets "Rome, GA" beat the city named Rome.
#
# The cost of that is symmetric and worth stating: a two-letter code from
# somewhere with its own states loses too. "Chennai, TN" is read as Tennessee,
# though "Chennai, TN, India" is not, because the country is then named outright
# and the city wins on tier.
#
# "WA" is here for Western Australia, which is fifteen hours from Washington
# State. It costs nothing to yield: "Vancouver, WA" then resolves through the
# city to America/Vancouver, which keeps the same Pacific offset all year.
#
# "NT" is the Northern Territory and it is also the Northwest Territories, and
# the code was decisive for the Canadian one. "Darwin, NT" — a normal way to
# write the location of a normal Australian job — scheduled every send for
# America/Edmonton, fourteen and a half hours out, so a message meant for
# Tuesday morning left on Monday evening. Yielding costs Canada nothing that
# was working: no Northwest Territories town is in the city table, so
# "Yellowknife, NT" still falls back to the code and still resolves to
# Edmonton. Darwin is in the table, so it wins.
_AMBIGUOUS_SUBDIVISION_CODES = frozenset(
    {"ar", "ca", "co", "de", "il", "in", "ma", "nl", "nt", "pe", "sk", "wa"}
)

# Segments that name the country rather than the subdivision, dropped from the
# end before the tail is read: "Seattle, WA, United States" ends in WA.
_NATIONAL_TAIL_SEGMENTS = frozenset(
    {
        "usa", "u.s.a.", "u.s.", "us", "united states",
        "united states of america", "america", "canada",
    }
)

_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)")
_POSTCODE_RE = re.compile(r"\b\d[\d-]*\b")
# A tail segment may only be letters, spaces and dots. "on-site" must not be
# read as Ontario, and a segment carrying anything else is not a state name.
_TAIL_SEGMENT_RE = re.compile(r"[a-z][a-z. ]*")


def subdivision_tail(location: str) -> tuple[str | None, bool]:
    """The US state or Canadian province a "City, ST" string ends in.

    Returns ``(zone name, ambiguous)``. A bare "Georgia" is not a tail — a
    subdivision only outranks a city when the string names the city first.

    Folded like :func:`zone_for_location`, and for the same reason twice over:
    :data:`_TAIL_SEGMENT_RE` admits only ``[a-z. ]``, so an accented tail was
    not a tail at all — "Trois-Rivières, Québec" named no province — and this
    is also called with a raw string by callers of its own.
    """
    stripped = _PARENTHETICAL_RE.sub("", fold_diacritics(location))
    segments = [_POSTCODE_RE.sub("", part).strip() for part in stripped.split(",")]
    segments = [part for part in segments if part]
    while segments and segments[-1] in _NATIONAL_TAIL_SEGMENTS:
        segments.pop()
    if len(segments) < 2:
        return None, False
    tail = segments[-1]
    if not _TAIL_SEGMENT_RE.fullmatch(tail):
        return None, False
    key = tail.rstrip(".")
    zone_name = _SUBDIVISION_ZONES.get(key)
    if zone_name is None:
        return None, False
    return zone_name, key in _AMBIGUOUS_SUBDIVISION_CODES


# Every subdivision spelt out in full is also a region in its own right, so a
# string that names one without a city ("Remote - Ohio") resolves too. Skipped
# where the name is already a key elsewhere: "Washington" and "New York" are
# cities first, and the tail above is what tells Tacoma from the District.
#
# A *spelt-out* name, which is what the length test is for. It read "> 2" while
# every code in the table was two letters, so the two rules agreed by accident;
# the Australian codes are three ("NSW", "VIC", "QLD", "TAS") and would have
# broken the tie the wrong way. A postal abbreviation earns its meaning from
# the city in front of it — that is what :func:`subdivision_tail` is — and
# turning one loose in tier 2 lets it match anywhere in any string. Nothing
# already in the table is three letters, so this changes no existing answer.
_REGION_ZONES.update(
    {
        name: zone
        for name, zone in _SUBDIVISION_ZONES.items()
        if len(name) > 3
        and name not in _CITY_ZONES
        and name not in _REGION_ZONES
        and name not in _COUNTRY_ZONES
    }
)


_LOCATION_ZONES: dict[str, str] = {**_CITY_ZONES, **_REGION_ZONES, **_COUNTRY_ZONES}

# Every key with its tier and a compiled word-boundary pattern, longest key
# first inside each tier so "new york state" is tried before "new york".
# Compiled once: this runs on every scheduled send.
_LOCATION_PATTERNS: tuple[tuple[int, str, re.Pattern[str]], ...] = tuple(
    (tier, key, re.compile(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])"))
    for tier, table in enumerate((_CITY_ZONES, _REGION_ZONES, _COUNTRY_ZONES))
    for key in sorted(table, key=len, reverse=True)
)


def zone_for_location(location: str | None) -> ZoneInfo | None:
    """The IANA zone a free-text location names, or None if unrecognised.

    Postings and recruiter profiles name places from the inside out — "Los
    Angeles, California, United States" is the format LinkedIn and every major
    ATS emit. Picking the longest matching key out of one flat table read that
    as *United States* and scheduled the send for Eastern, three hours before
    the recipient's morning. So the winner is chosen by **tier** first — a city
    beats the state it sits in, which beats the country — and only then by
    position, which settles a string naming two cities ("Seattle, Washington")
    in favour of the more specific one the format puts first.

    A key matched entirely inside a longer one is dropped before any of that, so
    the "washington" in "Washington State" doesn't get to vote as a city.

    Ahead of all of it sits :func:`subdivision_tail`: a US state or Canadian
    province on the end of "City, ST" outranks every tier, because half the
    towns in America are named after a European city and only the state says
    which one this is. See that function for the codes it can't settle.

    "Remote" resolves to nothing on purpose: a remote role is not in a timezone,
    but the recruiter reading the mail is, so the caller should fall through to
    the company's headquarters rather than settle for a default here.

    The accents come off first, with the same helper
    :func:`app.services.places.place_tokens` folds place names with. The tables
    below are written in the ASCII spelling every board that lacks the letter
    uses, and the word-boundary class this matches on treats an accent as a
    boundary — so without the fold, "Genève" was two tokens and named nowhere.
    """
    if not location:
        return None
    text = fold_diacritics(re.sub(r"\s+", " ", location.strip().lower()))
    if not text:
        return None

    tail_zone, tail_ambiguous = subdivision_tail(text)
    if tail_zone is not None and not tail_ambiguous:
        return _zone(tail_zone)

    # (tier, start, end, key) for every key the string mentions.
    hits: list[tuple[int, int, int, str]] = []
    for tier, key, pattern in _LOCATION_PATTERNS:
        found = pattern.search(text)
        if found is not None:
            hits.append((tier, found.start(), found.end(), key))
    if not hits:
        # Nothing else recognised, so an ambiguous code is the only evidence
        # left: "Wilmington, DE" is in Delaware because no Germany was named.
        return _zone(tail_zone) if tail_zone is not None else None

    standalone = [
        hit
        for hit in hits
        if not any(
            (other[1], other[2]) != (hit[1], hit[2])
            and other[1] <= hit[1]
            and hit[2] <= other[2]
            for other in hits
        )
    ]
    winner = min(standalone, key=lambda hit: (hit[0], hit[1]))
    return _zone(_LOCATION_ZONES[winner[3]])


def zone_for_email(address: str | None) -> ZoneInfo | None:
    """A zone guessed from an unambiguous country-code TLD, or None."""
    if not address or "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip().lower().rstrip(".")
    parts = domain.split(".")
    if len(parts) < 2:
        return None
    # "acme.co.uk" -> uk; "acme.de" -> de.
    return _zone(_TLD_ZONES.get(parts[-1]))


def resolve_timezone(
    db: Session | None,
    recruiter,
    *,
    application=None,
    cache: bool = True,
) -> ZoneInfo:
    """The best available timezone for *recruiter*, cached back onto the row.

    Order, best source first: the cached column, the company's researched
    headquarters, the targeted posting's location, the email's country TLD, then
    the configured default. A resolution that lands on the default is never
    cached — a ``CompanyProfile`` written later should still be able to improve
    it.
    """
    if recruiter is None:
        return default_timezone()

    cached = _zone(getattr(recruiter, "timezone", None))
    if cached is not None:
        return cached

    resolved: ZoneInfo | None = None

    # 2. The company's researched headquarters — the global research cache is
    #    already populated for any employer the product has looked at.
    if db is not None and recruiter.company:
        resolved = _zone_from_company_profile(db, recruiter.company)

    # 3. The specific posting this outreach is about.
    if resolved is None and application is not None:
        posting_id = getattr(application, "job_posting_id", None)
        if posting_id is not None and db is not None:
            from app.models.job import JobPosting

            posting = db.get(JobPosting, posting_id)
            if posting is not None:
                resolved = zone_for_location(posting.location)

    # 4. The email's country TLD.
    if resolved is None:
        resolved = zone_for_email(recruiter.email)

    if resolved is None:
        return default_timezone()

    if cache and hasattr(recruiter, "timezone"):
        recruiter.timezone = str(resolved)
    return resolved


def _zone_from_company_profile(db: Session, company: str) -> ZoneInfo | None:
    """The zone of a researched company's headquarters, if we have one."""
    from sqlalchemy import select

    from app.models.company_profile import CompanyProfile
    from app.services.job_dedup import normalize_company

    normalized = normalize_company(company)
    if not normalized:
        return None
    profile = db.scalar(
        select(CompanyProfile).where(CompanyProfile.normalized_name == normalized)
    )
    if profile is None:
        return None
    return zone_for_location(profile.headquarters)


# --------------------------------------------------------------------------- #
# The slot calculator                                                          #
# --------------------------------------------------------------------------- #


def _window() -> tuple[int, int]:
    """The (start, end) local hours of the send window, sanity-checked."""
    start = settings.send_window_start_hour
    end = settings.send_window_end_hour
    if not (0 <= start < end <= 24):
        logger.warning(
            "invalid send window %s-%s; falling back to 9-11", start, end
        )
        return 9, 11
    return start, end


def next_slot(
    after: datetime,
    tz: ZoneInfo | None = None,
    *,
    jitter: bool = True,
    rng: random.Random | None = None,
) -> datetime:
    """The next weekday 09:00-11:00 in *tz*, at or after *after*, in UTC.

    Never moves a time backwards: a message due on Saturday goes out Monday, not
    the preceding Friday. A moment already inside the window on a weekday is
    returned as-is — deferring a 09:40 Tuesday send to Wednesday would be worse
    timing, not better.

    With *jitter* the result is spread across the window, so fifty messages
    scheduled for the same morning don't all fire at 09:00:00 — which is both a
    burst pattern and obviously automated.
    """
    tz = tz or default_timezone()
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    start_hour, end_hour = _window()
    # The module function by default, so a test that seeds `random` gets a
    # reproducible result without having to thread an instance through.
    randint = rng.randint if rng is not None else random.randint
    local = after.astimezone(tz)

    for _ in range(14):  # bounded; a weekday morning is always within four days
        if local.weekday() in WEEKDAYS:
            if local.hour < start_hour:
                local = local.replace(
                    hour=start_hour, minute=0, second=0, microsecond=0
                )
                break
            if local.hour < end_hour:
                break  # already inside today's window
        local = (local + timedelta(days=1)).replace(
            hour=start_hour, minute=0, second=0, microsecond=0
        )
    else:  # pragma: no cover - unreachable given the 14-day sweep
        return after

    if jitter:
        # Counted from midnight rather than `.replace(hour=end_hour)`, because
        # `_window` accepts an end of 24 — the natural way to write "until the
        # end of the day" — and `replace` raises on it. Every scheduled send
        # went through this line, so that configuration turned each one into a
        # ValueError inside a Celery task rather than a late-evening slot.
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = midnight + timedelta(hours=end_hour)
        remaining = int((window_end - local).total_seconds())
        if remaining > 60:
            local = local + timedelta(seconds=randint(0, remaining - 60))

    slot = local.astimezone(UTC)
    # Jitter is only ever added forward, but a DST fold could in principle pull
    # the converted moment back behind `after`. Never return the past.
    return max(slot, after)


def delay_seconds(slot: datetime, *, now: datetime | None = None) -> int:
    """Seconds from *now* until *slot*, clamped to a sane Celery countdown."""
    now = now or datetime.now(UTC)
    if slot.tzinfo is None:
        slot = slot.replace(tzinfo=UTC)
    return max(0, min(int((slot - now).total_seconds()), MAX_SEND_DELAY_SECONDS))


__all__ = [
    "MAX_SEND_DELAY_SECONDS",
    "default_timezone",
    "delay_seconds",
    "next_slot",
    "resolve_timezone",
    "subdivision_tail",
    "zone_for_email",
    "zone_for_location",
]
