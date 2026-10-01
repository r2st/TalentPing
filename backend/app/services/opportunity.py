"""What we know about one inbound opportunity, normalised for reading.

:mod:`app.services.recruiter_classifier` reads seven facts off every recruiter
email in the same call that classifies it — the role, the company, where it is,
whether it is remote, the pay it names, the level, and the direct questions it
asks — and :meth:`Classification.extracted` stores them verbatim on the row.

Two of those seven ever reached a screen. The inbox printed
``role_title · company`` as a grey subtitle and dropped the rest, so a candidate
triaging thirty messages could not see which one was remote, which one named a
number, or what any of them had actually asked without opening and reading each
in full. The facts were paid for and thrown away.

This module is the read side of that data. Two entry points, split by cost:

:func:`intel`
    Pure. Normalises the stored blob and parses the pay string into figures.
    Cheap enough to run per row on a list page.

:func:`market_context`
    Hits the database, and may write — :func:`salary_service.get_benchmark`
    caches the band it models. One message at a time only.

Both derive at **read** time rather than at classification time, which is the
whole reason this is a module and not four more columns: prod already holds
messages classified months ago, and a derivation gives them the same panel as a
message that arrives tomorrow without a migration or a backfill.

``extracted`` stays on the wire beside the normalised view. It is the audit
record of what the classifier said, in the classifier's words, and the row is
the audit trail as much as the work item — see
:mod:`app.models.recruiter_email`. This view is what the words *mean*, which is
a different thing and allowed to change without rewriting history.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.profile import Profile
from app.models.recruiter_email import RecruiterEmail
from app.models.user import User
from app.services import salary_service

logger = logging.getLogger(__name__)

#: Cap on the questions surfaced. The classifier already truncates to five; this
#: holds if a row was written by something that didn't.
_MAX_ASKS = 5

#: Length caps, matching what the classifier applies on the way in. A row from a
#: forgiving older writer cannot push an unbounded string into the response.
_LIMITS = {
    "role_title": 255,
    "company": 255,
    "location": 255,
    "salary_text": 255,
    "seniority": 50,
}


def _text(value: Any, limit: int) -> str | None:
    """A clean display string, or ``None`` — never the *word* "unknown".

    A model that answers ``"N/A"`` for the location has told us it does not
    know, and rendering that on the card is worse than rendering nothing: an
    absent line reads as absent, where "N/A" reads as a fact about the job.
    Mirrors ``recruiter_classifier._coerce_str`` so a row normalises the same
    way whichever side of the wire it is read on.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "n/a", "unknown"):
        return None
    return text[:limit]


def _flag(value: Any) -> bool | None:
    """Tri-state remote, keeping "not stated" distinct from "not remote".

    The distinction is load-bearing: :func:`app.services.inbound_matcher.
    location_gate` deliberately lets an unstated location through and vetoes a
    stated on-site one, so collapsing the two here would put a badge on the card
    that contradicts the routing decision printed beside it.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "remote"):
            return True
        if lowered in ("false", "no", "onsite", "on-site"):
            return False
    return None


@dataclass
class OpportunityIntel:
    """The opportunity a message describes, in fields a card can render."""

    role_title: str | None = None
    company: str | None = None
    location: str | None = None
    remote: bool | None = None
    seniority: str | None = None

    # Pay exactly as the recruiter wrote it — "£90k-£110k + equity", "DOE".
    # Kept whole because the phrasing carries terms the figures don't.
    salary_text: str | None = None
    # The same string read as numbers, when it holds any. Null for "DOE".
    #
    # **A year's pay, whatever period the recruiter quoted.** This comment used
    # to say the opposite — "null for an hourly or monthly rate, because
    # `parse_offered` refuses to annualise those" — which was true when it was
    # written and has not been since that function stopped refusing (see its
    # docstring, and `app.services.pay_period` for the table it annualises
    # with). "$85 - $105 per hour" reads as 176,800-218,400 here, and
    # `test_a_rate_is_annualised_so_the_market_card_can_position_it` has
    # asserted so for as long as it has been true.
    #
    # Left uncorrected it is the most dangerous shape a comment can have in
    # this area: it tells the next reader that a rate produces no figure, which
    # is precisely the belief that would have someone "restore" the refusal —
    # and a null band is what `salary_service.meets_floor` waves through and
    # what left every contract posting with nothing to compare. Every figure
    # this product stores is a year's pay; this one is no exception.
    salary_min: int | None = None
    salary_max: int | None = None

    # Direct questions the recruiter asked, checked against the message by
    # `recruiter_classifier._grounded_asks` before they were stored. The reply
    # agent is briefed on exactly these; showing them is what lets a user check
    # that the draft below answered them.
    asks: list[str] = field(default_factory=list)

    @property
    def has_facts(self) -> bool:
        """Whether anything here is worth giving a card to."""
        return any(
            (
                self.role_title,
                self.company,
                self.location,
                self.remote is not None,
                self.seniority,
                self.salary_text,
                self.asks,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "role_title": self.role_title,
            "company": self.company,
            "location": self.location,
            "remote": self.remote,
            "seniority": self.seniority,
            "salary_text": self.salary_text,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "asks": list(self.asks),
        }


def intel(extracted: dict[str, Any] | None) -> OpportunityIntel:
    """Normalise a stored ``extracted`` blob. Pure; safe to call per row.

    Tolerant of anything the column can hold. It is JSON written by several
    generations of writer — including ``{}`` from the two paths that store a
    message before it has been classified — so a missing key, a null, a string
    where a list belongs and a non-dict altogether all yield an empty view
    rather than an exception on a list endpoint.
    """
    if not isinstance(extracted, dict):
        return OpportunityIntel()

    raw_asks = extracted.get("asks")
    asks: list[str] = []
    if isinstance(raw_asks, list):
        for item in raw_asks[:_MAX_ASKS]:
            question = _text(item, 300)
            if question:
                asks.append(question)

    salary_text = _text(extracted.get("salary_text"), _LIMITS["salary_text"])
    low, high = salary_service.parse_offered(salary_text)

    return OpportunityIntel(
        role_title=_text(extracted.get("role_title"), _LIMITS["role_title"]),
        company=_text(extracted.get("company"), _LIMITS["company"]),
        location=_text(extracted.get("location"), _LIMITS["location"]),
        remote=_flag(extracted.get("remote")),
        seniority=_text(extracted.get("seniority"), _LIMITS["seniority"]),
        salary_text=salary_text,
        salary_min=low,
        salary_max=high,
        asks=asks,
    )


@dataclass
class MarketContext:
    """The modelled band for this role, and where the message's number sits."""

    insight: salary_service.SalaryInsight
    # The matched profile's floor, when it has one. Named for the profile so the
    # card can say *whose* floor it is — a user with "Backend, remote" and "Tech
    # Lead, Berlin" has two, and only the matched one is the relevant test.
    floor: int | None = None
    floor_label: str | None = None

    @property
    def clears_floor(self) -> bool:
        """Whether the advertised band clears the matched profile's floor.

        ``salary_service.meets_floor`` and its two deliberate generosities: an
        unpublished band passes, and the *top* of a range is what's compared.
        True when there is no floor to clear, which is the common case and not
        a judgement about the pay.
        """
        return salary_service.meets_floor(
            self.insight.comparison.offered_max, self.floor
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.insight.as_dict(),
            "floor": self.floor,
            "floor_label": self.floor_label,
            "clears_floor": self.clears_floor,
        }


def market_context(
    db: Session, user: User, row: RecruiterEmail
) -> MarketContext | None:
    """The salary band for one message, or ``None`` when we can't model one.

    ``None`` rather than a generic band when the classifier found no role title.
    :func:`salary_service.classify_role` answers *something* for every input —
    an unmatched title falls through to a generic family — and a number modelled
    off nothing at all, printed in the same card style as one modelled off
    "Staff SRE, Berlin", is a figure a candidate might negotiate against. The
    absence is the honest answer.

    Separate from :func:`intel`, and reached by its own endpoint, because
    :func:`salary_service.get_benchmark` reads and may write a shared cache row.
    That is affordable once for the message on screen and not affordable per row
    of a list — the same reason ``GET /jobs/{id}/intel`` is not part of the job
    feed.
    """
    facts = intel(row.extracted)
    if not facts.role_title:
        return None

    band = salary_service.get_benchmark(
        db,
        facts.role_title,
        facts.location,
        remote=facts.remote,
        description=row.body_text,
        seniority=facts.seniority,
    )
    insight = salary_service.SalaryInsight(
        band=band,
        # The figures `intel` already read off this very string, rather than a
        # second reading of it. Same string and same parser today, so this is
        # only a saved parse — but it is also the guarantee that the card and
        # the chips beside it cannot describe the same offer differently.
        comparison=salary_service.compare_to_market(
            band,
            facts.salary_text,
            offered=(facts.salary_min, facts.salary_max),
        ),
    )

    floor: int | None = None
    floor_label: str | None = None
    if row.matched_profile_id is not None:
        profile = db.scalar(
            select(Profile).where(
                Profile.id == row.matched_profile_id, Profile.user_id == user.id
            )
        )
        if profile is not None and profile.salary_min:
            floor = profile.salary_min
            floor_label = profile.name

    return MarketContext(insight=insight, floor=floor, floor_label=floor_label)


__all__ = ["MarketContext", "OpportunityIntel", "intel", "market_context"]
