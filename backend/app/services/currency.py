"""One exchange-rate table, because both sides of a salary comparison need it.

Every salary figure this product stores is a US dollar figure. Nothing in the
schema says so — ``profiles.salary_min`` and ``job_postings.salary_min`` are
bare integers with no currency beside them — so it is a convention, and it has
to be enforced at both of the places a number gets in:
:func:`app.services.jd_parser.extract_salary` reading an employer's band, and
:func:`app.services.resume_parser.extract_salary_expectation` reading a
candidate's floor.

This module exists so those two cannot disagree. It also has no imports from
either, which is the other half of the reason: `jd_parser` already reads
`resume_parser` for its skill extractor, so a rate table living in one of them
could not be reached from the other without a cycle.
"""
from __future__ import annotations

import re

#: What one unit of each currency this module recognises is worth in US dollars.
#:
#: Handing a rupee figure to something holding a dollar one is not a smaller
#: answer, it is a wrong one, and it fails in the dangerous direction on both
#: sides. An employer's ₹12,00,000 — about fourteen thousand dollars — read as
#: one-point-two million cleared every floor a candidate could set and scored
#: `fit_scorer.score_salary` a confident 1.0, a dimension that feeds the fit
#: score, which gates autopilot sending; `salary_service.compare_to_market` ran
#: the same arithmetic the other way and called that posting eight times the
#: market median. A candidate's stated "₹18,00,000" read the same way became a
#: floor no posting on earth clears, which empties their feed.
#:
#: The rates are coarse and they are static, and both are deliberate. A live
#: rate would be a network dependency and a failure mode on code paths whose
#: whole job is reading text, and a rate that has drifted ten per cent moves a
#: fit dimension by less than its own rounding. What it replaces is not a better
#: number, it is an implicit rate of 1.0 for every currency on earth — off by a
#: factor of eighty for the rupee. Rounded hard so that nobody downstream reads
#: precision into them.
CURRENCY_TO_USD: dict[str, float] = {
    "usd": 1.0,
    "eur": 1.10,
    "gbp": 1.25,
    "cad": 0.75,
    "aud": 0.65,
    "nzd": 0.60,
    "chf": 1.10,
    "sek": 0.10,
    "nok": 0.09,
    "dkk": 0.15,
    "pln": 0.25,
    "ils": 0.27,
    "aed": 0.27,
    "sgd": 0.75,
    "hkd": 0.13,
    "cny": 0.14,
    "brl": 0.18,
    "mxn": 0.05,
    "zar": 0.055,
    "inr": 0.012,
    "jpy": 0.0067,
    "krw": 0.0007,
    # The currencies below were the same bug as the rupee above, still live:
    # every one of them was priced at an implicit 1.0, and every one of them is
    # worth well under a dollar. A ₱1,200,000 Manila salary — about twenty
    # thousand dollars — cleared every floor a candidate could set and scored
    # `fit_scorer.score_salary` a confident 1.0, which gates autopilot sending;
    # ₦25,000,000 and Rp 300,000,000 failed the same way by larger factors
    # still. These are the markets a remote-first job feed is full of.
    "php": 0.017,
    "thb": 0.028,
    "myr": 0.22,
    "idr": 0.000061,
    "vnd": 0.000039,
    "twd": 0.031,
    "try": 0.025,
    "uah": 0.024,
    "rub": 0.011,
    "czk": 0.043,
    "huf": 0.0026,
    "ron": 0.22,
    "ngn": 0.00065,
    "kes": 0.0077,
    "egp": 0.020,
    "pkr": 0.0036,
    "bdt": 0.0082,
    "lkr": 0.0033,
    "kzt": 0.0019,
    "sar": 0.27,
    "qar": 0.27,
    "cop": 0.00024,
    "clp": 0.0010,
    "ars": 0.00075,
    "pen": 0.27,
}

#: A bare "$" is read as USD rather than CAD or AUD: it is what the vast
#: majority of postings using it mean, and it is what this product assumed
#: before any of this existed.
#:
#: A *prefixed* dollar is the opposite case. A posting that writes "C$120,000"
#: has gone to the trouble of saying which dollar it means, and reading it as
#: the American one overstates a Canadian band by a third — the same direction
#: as the rupee failure above, and for the same reason: it clears every floor
#: the candidate set and scores :func:`fit_scorer.score_salary` a confident
#: 1.0. "CAD 120,000" was already priced correctly; the identical posting
#: written the way Canadian boards actually write it was not.
SYMBOL_TO_CURRENCY = {
    "$": "usd",
    "€": "eur",
    "£": "gbp",
    "₹": "inr",
    "₩": "krw",
    "₪": "ils",
    # Bare ¥ is read as yen. The glyph is shared with the renminbi, and there
    # is no way to tell them apart from the character — but an English-language
    # posting quoting the renminbi writes "CNY" or "RMB", both of which are in
    # the word list below and win outright, because an explicit marker is
    # matched as a marker. The same call the bare "$" makes, for the same
    # reason: read the dominant meaning, and let anyone who says otherwise say
    # it. Getting this wrong costs a factor of twenty; leaving ¥ unrecognised
    # cost a factor of a hundred and fifty.
    "¥": "jpy",
    "c$": "cad",
    "ca$": "cad",
    "a$": "aud",
    "au$": "aud",
    "aus$": "aud",
    "nz$": "nzd",
    "s$": "sgd",
    "sg$": "sgd",
    "hk$": "hkd",
    "r$": "brl",
    "₺": "try",
    # The peso sign is the *only* way this table can price a Philippine salary.
    # "PHP" is not in the word list below and must not be: in a job description
    # it is the programming language far more often than it is the currency,
    # and a rate of 0.017 applied because the posting listed a backend skill
    # would divide a real dollar band by sixty. So a posting that writes the
    # code rather than the sign is read as unmarked, which is the same answer
    # this module gave before the sign was here, and the safe direction.
    "₱": "php",
    "฿": "thb",
    "₫": "vnd",
    "₴": "uah",
    "₦": "ngn",
    "₽": "rub",
    "₸": "kzt",
    "rp": "idr",
    "kč": "czk",
    "zł": "pln",
}

#: The markers a band may carry, as a regex fragment. Shared so the patterns
#: that *find* a band and the function that prices it recognise the same set —
#: without the prefixed forms here, "C$120,000 - C$140,000" is not even seen as
#: a range: the finder matches the second "$" and loses the top of the band.
#:
#: The prefixes come first so the leftmost match is "C$" rather than the "$"
#: one character later, and they are case-sensitive inside an otherwise
#: case-insensitive pattern — "A$50,000" is Australian, and "plan a$50,000" is
#: nobody writing about money. The lookbehind is what keeps "USA$120,000" from
#: reading as Australian.
#:
#: Longer prefixes come before their own prefixes ("AUS" before "AU" before
#: "A", "CA" before "C", "SG" before "S") because Python's alternation is
#: leftmost-*first*, not leftmost-longest: with "A" listed first, "AUS$" would
#: match nothing and fall through to the bare "$".
#: A handful of ISO codes are spelt the same as ordinary words, and the
#: pattern is otherwise case-insensitive: ``\btry\b`` matches "try", ``\bron\b``
#: matches a colleague called Ron, and ``\bcop\b``, ``\bpen\b``, ``\brub\b``
#: and ``\bars\b`` all match prose. Pricing a dollar band at the Turkish lira
#: because the ad said "try our product" is the rupee failure run backwards and
#: forty times worse, so these are matched **only in upper case**, which is how
#: a posting quoting them writes them.
#:
#: The word need not be English, and ``ILS`` is the one that is not. ``ils`` is
#: the French for "they" — as common a word as this feed carries in any language
#: — so every French posting and every French recruiter mail named the Israeli
#: shekel somewhere in its prose. ``reply_agent.extract_salary_figures`` reads
#: an unmarked figure at the message's rate, and "le salaire est de 110 000",
#: written a few words before an "ils", came out as $29,700. The shekel is
#: still read from ``₪`` and from ``ILS``, which is how a posting quoting it
#: writes it; nothing quotes a currency in lower case.
#:
#: "PHP" is absent on purpose and is not recoverable by case: in a job
#: description it is the language, not the peso. See :data:`SYMBOL_TO_CURRENCY`.
_CASE_SENSITIVE_CODES = r"(?-i:TRY|RON|RUB|SAR|COP|ARS|CLP|PEN|ILS)"

CURRENCY_PATTERN = (
    r"(?<![a-z])(?-i:(?:AUS|AU|A|CA|C|NZ|SG|S|HK|R)\$)"
    r"|[$€£₹₩₪¥₺₱฿₫₴₦₽₸]"
    r"|(?<![a-z])(?-i:Kč|zł|Rp)(?![a-z])"
    r"|\b(?:usd|eur|gbp|inr|cad|aud|nzd|chf|sek|nok|dkk|pln|aed"
    r"|sgd|hkd|cny|rmb|brl|mxn|zar|jpy|krw"
    r"|thb|myr|idr|vnd|twd|uah|czk|huf|ngn|kes|egp|pkr|bdt|lkr|kzt|qar)\b"
    rf"|\b{_CASE_SENSITIVE_CODES}\b"
)

_CURRENCY_RE = re.compile(CURRENCY_PATTERN, re.I)

#: The South Asian grouping, as a regex fragment: two digits at a time above the
#: last three, so a lakh is "1,00,000" and a crore is "1,00,00,000".
#:
#: Not a variant spelling of the Western grouping — the comma positions differ —
#: and a pattern built for groups of three cannot read it.
#: ``\d{1,3}(?:[.,]\d{3})+`` fails on "15,00,000" after the first comma and
#: backtracks to whatever shorter alternative sits beside it, which matches the
#: leading "15" and stops. That is how a ₹20,00,000 posting came out as a band
#: of ₹15, with "₹15" printed on the card beside it.
#:
#: Belongs with the rates because it is the same fact about the same postings,
#: and because the two shapes cannot both match one string — so listing this
#: first, as every user of it does, costs the Western format nothing.
INDIAN_GROUPED = r"\d{1,2}(?:,\d{2})+,\d{3}"

#: The Swiss grouping, which separates thousands with an apostrophe:
#: "CHF 130'000". Both the typewriter apostrophe and the typographic one, since
#: a posting pasted out of a word processor carries the second.
#:
#: The same failure as the lakh grouping and worse, because Switzerland carries
#: the highest multiplier in ``salary_service``'s table. A pattern built for
#: "[.,]" separators cannot see this one, so the amount stopped at the first
#: apostrophe: "CHF 130'000 - CHF 150'000" matched "CHF 130" and lost the whole
#: top of the band, and "CHF 1'250'000" matched nothing that satisfied the
#: money shapes at all — a published band vanished, and a vanished band is the
#: one `meets_floor` waves through.
#:
#: Unlike the lakh grouping this is *not* read as a currency marker on its own.
#: A lakh-grouped figure is South Asian by construction — nobody writes dollars
#: two digits at a time — where an apostrophe-grouped one is merely usually
#: Swiss, and the postings that are write "CHF" somewhere anyway. Getting the
#: shape right is what recovers the band; guessing the currency from it would
#: buy the last ten per cent and risk being wrong about the whole figure.
APOSTROPHE_GROUPED = r"\d{1,3}(?:['\u2019]\d{3})+"

#: The space grouping, which is how France, Poland, Sweden, Norway, Finland,
#: Czechia, Hungary and Russia all write a thousand: "55 000 €", "180 000 PLN",
#: "850 000 SEK". Ordinary spaces, and the three the typesetting world uses —
#: a posting pasted out of a word processor or a CMS carries a non-breaking or
#: a thin one, and they are invisible in the text either way.
#:
#: The failure is not the one the other groupings have. A pattern built for
#: "[.,]" stops at the first space, and `_money_to_int`'s "a bare figure under
#: a thousand is really thousands" rule then rescues the common magnitudes by
#: accident: "€55 000" stopped at "€55", read 55, and scaled it to 55,000 —
#: right, for the wrong reason. It stops being an accident as soon as the
#: figure has another group: "HUF 9 000 000" read 9 and made it nine thousand
#: forint, about twenty dollars.
#:
#: And a *trailing* marker gets no rescue at all — "55 000 €", "850 000 SEK",
#: "45 000 - 55 000 €" matched nothing that satisfied the money shapes, so a
#: published band vanished, and a vanished band is the one `meets_floor` waves
#: through and `score_salary` calls "the posting doesn't publish a salary band".
#:
#: Bounded on both sides by a digit check, because unlike the other two
#: groupings this shape is a substring of ordinary prose: without the
#: lookbehind, "founded in 2019 500 people joined" offers "019 500".
SPACE_GROUPED = r"(?<!\d)\d{1,3}(?:[ \u00a0\u202f\u2009]\d{3})+(?!\d)"

#: Every character that groups digits rather than valuing them, so a caller
#: stripping separators out of a matched figure strips all of them. Missing the
#: apostrophe here turns "130'000" into the number 130 by way of `int()`.
GROUPING_CHARS = ".,'\u2019 \u00a0\u202f\u2009"


_INDIAN_GROUPED_RE = re.compile(INDIAN_GROUPED)

#: Spellings that are not the ISO code. Kept apart from
#: :data:`SYMBOL_TO_CURRENCY` because these are words, not marks.
_WORD_ALIASES = {"rmb": "cny"}


def usd_rate(text: str) -> float:
    """Dollars per unit of the currency *text* is marked with.

    ``1.0`` when *text* names no currency, or names one with no rate on file. In
    both cases the caller passes the figure through untouched, which is what
    this product did for every currency before this table existed — and which is
    right for an unmarked figure, since there is nothing to convert *from*.

    With one exception: **the lakh grouping is itself a currency marker.**
    "12,00,000" is not a formatting preference applied to some other currency;
    nobody writes dollars, euros or pounds two digits at a time. A resume line
    reading "Expected CTC 12,00,000" — no symbol, because the writer and every
    reader they had in mind knew which currency it was — is about fourteen
    thousand dollars, and passing it through as one-point-two million sets a
    floor that empties the candidate's feed. Read only when no explicit marker
    is present, so an inscription that says otherwise always wins.
    """
    match = _CURRENCY_RE.search(text)
    if not match:
        return CURRENCY_TO_USD["inr"] if _INDIAN_GROUPED_RE.search(text) else 1.0
    token = match.group(0).lower()
    token = _WORD_ALIASES.get(token, token)
    return CURRENCY_TO_USD.get(SYMBOL_TO_CURRENCY.get(token, token), 1.0)


__all__ = [
    "APOSTROPHE_GROUPED",
    "CURRENCY_PATTERN",
    "CURRENCY_TO_USD",
    "GROUPING_CHARS",
    "INDIAN_GROUPED",
    "SPACE_GROUPED",
    "SYMBOL_TO_CURRENCY",
    "usd_rate",
]
