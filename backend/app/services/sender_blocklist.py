"""Sender domain blocklist — skip known non-recruiter senders before classification.

These domains are matrimonial sites, status pages, newsletters, travel deals,
banking alerts, and other services that the classifier frequently marks UNKNOWN
because they superficially mention "opportunities" or "matches". Blocking them
here avoids wasting an LLM call and prevents false-positive drafts.

Maintenance: add a domain when you see it repeatedly flagged with kind=UNKNOWN
and it is clearly not a recruiter. Prefer exact domains over wildcards; use
suffix patterns (leading dot) only for subdomains of the same service.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Exact-match domains. Compared against the full domain after the ``@``.
_BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
        # Matrimonial / dating
        "shaadi.com",
        "jeevansathi.com",
        "bharatmatrimony.com",
        # Status pages / incident alerts
        "statuspage.io",
        # Event platforms
        "user.luma-mail.com",
        "calendar.luma-mail.com",
        # Travel / shopping
        "tripjack.com",
        "eg.hotels.com",
        "updates.floweraura.com",
        "updates.easemytrip.com",
        "list.vacationstogo.com",
        # Social / newsletters
        "service.tiktok.com",
        "skool.com",
        # Banking / finance alerts
        "axis.bank.in",
        "digital.axisbankmail.bank.in",
        "email.borrowell.com",
        # Fitness / lifestyle
        "member.24hourfitness.com",
        # Sports / entertainment
        "info.rajasthanroyals.com",
        # Telecom
        "info.luckymobile.ca",
        # Ride-share marketing
        "marketing.lyftmail.com",
        # Product notifications (not recruiter)
        "email.claude.com",
        "email.neon.tech",
        "hello.livekit.io",
        "flyai.airindia.com",
        "prestigeconstructions.com",
    }
)

# Suffix patterns: if a sender's domain *ends with* any of these, it is blocked.
# Used for services that send from many subdomains of the same parent.
_BLOCKED_SUFFIXES: tuple[str, ...] = (
    ".bank.in",  # Indian banking alerts (axis, hdfc, etc.)
)


def is_blocked(from_address: str) -> bool:
    """Return ``True`` if the sender's domain is on the blocklist.

    Cheap string check — runs before any network call or LLM invocation.
    """
    if not from_address or "@" not in from_address:
        return False
    domain = from_address.rsplit("@", 1)[1].strip().lower()
    if domain in _BLOCKED_DOMAINS:
        return True
    return any(domain.endswith(suffix) for suffix in _BLOCKED_SUFFIXES)


__all__ = ["is_blocked"]
