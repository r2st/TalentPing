"""Opt-out links that can't be forged, and the one-click POST that must accept them.

Two defects in the previous link are fixed here, and they are worth naming
separately because only one of them is about deliverability.

**The link was a bare address.** ``/unsubscribe?email=talent@acme.com`` opted out
every recruiter row matching that address, across every user, with no proof the
click came from a message we sent. Anyone who guessed an address could burn a
contact — and the address is in the ``To:`` header of a message they were
holding, so guessing was not required. Worse, an address is not a secret and
never expires, so a link leaked once revoked that contact forever.

The link now carries an HMAC of the address, keyed on ``jwt_secret``. It proves
the URL came from us without a database round-trip and without storing a row per
send: unlike the digest's ``unsubscribe_token``, there is no ``Recruiter``-side
column to hang a random secret off, and a recruiter is contacted by more than
one user. Signing the address itself gets the same property for free.

**We advertised one-click and served a 405.** Every unsolicited send carries
``List-Unsubscribe-Post: List-Unsubscribe=One-Click`` (RFC 8058), which is a
promise to the receiving provider that the HTTPS URL beside it accepts a POST.
Gmail and Yahoo both take that promise literally — Gmail's native "Unsubscribe"
button POSTs — and the route was ``@router.get`` only, so it 405'd. The provider
sees a failed unsubscribe against a sender that claimed to support it, which is
precisely the signal the header exists to earn credit for. A header we cannot
honour is worse than no header: it converts a working opt-out into a complaint.

Legacy links stay live. Messages sent before this are sitting in mailboxes with
an unsigned ``?email=`` URL and will be clicked months from now; refusing them
would strand a real human trying to opt out, which is the one failure this
module exists to prevent. An unsigned link is honoured and the fact recorded —
see :func:`verify`.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import quote

from app.core.config import settings

# Truncated to 16 bytes / 32 hex chars. A forger needs a preimage of a keyed
# hash, not a collision, so 128 bits is far past sufficient — and the URL ends
# up in a plain-text footer that a human may have to read aloud or retype.
_SIG_BYTES = 16


def _normalize(address: str) -> str:
    return (address or "").strip().lower()


def sign(address: str) -> str:
    """The signature for one recipient address."""
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        _normalize(address).encode("utf-8"),
        sha256,
    ).hexdigest()[: _SIG_BYTES * 2]


def build_url(address: str) -> str:
    """The opt-out URL to put in a message's footer and List-Unsubscribe header."""
    normalized = _normalize(address)
    return (
        f"{settings.compliance_unsubscribe_base_url}"
        f"?email={quote(normalized, safe='')}"
        f"&sig={sign(normalized)}"
    )


@dataclass(frozen=True)
class UnsubscribeRequest:
    """A parsed opt-out click: who, and how much we trust the link."""

    address: str
    signed: bool

    @property
    def valid(self) -> bool:
        """Whether this request should be honoured at all.

        An empty address is the only thing refused. A bad signature is *not*
        refused — see :func:`verify`.
        """
        return bool(self.address)


def verify(address: str, signature: str | None) -> UnsubscribeRequest:
    """Check an opt-out link's signature, without ever refusing a real human.

    ``signed`` is False for a missing or wrong signature, and the caller opts
    the address out anyway. That is deliberate, and it is the asymmetry that
    decides it: honouring a forged unsubscribe costs one contact that the user
    can re-add, while refusing a genuine one means continuing to mail somebody
    who pressed the button — a CAN-SPAM violation, a spam complaint, and a
    provider-level reputation hit against the mailbox.

    The flag is what the signature buys: an unsigned click is logged as such, so
    a flood of them is visible as the abuse it would be, rather than looking
    like a sudden collapse in message quality.
    """
    normalized = _normalize(address)
    if not normalized or not signature:
        return UnsubscribeRequest(normalized, signed=False)
    return UnsubscribeRequest(
        normalized, signed=hmac.compare_digest(signature.strip(), sign(normalized))
    )


__all__ = ["UnsubscribeRequest", "build_url", "sign", "verify"]
