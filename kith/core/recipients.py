"""Parse a free-text recipient list — pure, side-effect-free.

Two parsers, one per delivery channel, because the compose form asks for the two
lists separately: which channel a person is on is something the host states, not
something we infer from the shape of what they typed. Both accept entries
separated by commas/newlines, optionally as "Name <address>", and both return
deduped valid entries plus the chunks that didn't parse, so the UI can gently
flag them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kith.core import phones

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NAMED_RE = re.compile(r"^(.*?)<([^>]+)>$")

# Delivery channels. Absent/NULL in the DB means email, since every recipient
# written before the WhatsApp channel existed predates the column.
CHANNEL_EMAIL = "email"
CHANNEL_WHATSAPP = "whatsapp"


@dataclass(frozen=True)
class Parsed:
    """One parsed person. ``phone`` set = WhatsApp; otherwise email.

    A WhatsApp entry carries ``email=""`` to match the Recipient/Contact columns,
    which are NOT NULL and can't be loosened in place on SQLite.
    """

    name: str | None
    email: str
    phone: str | None = None

    @property
    def channel(self) -> str:
        return CHANNEL_WHATSAPP if self.phone else CHANNEL_EMAIL

    @property
    def identity(self) -> str:
        """Stable key for dedup and reconciliation across both channels.

        The email when there is one, else "tel:<e164>" — namespaced so a number
        can never collide with an address. Email-first matters for the address
        book, where one person can hold both: adding a number to a known contact
        has to find that contact, not fork a second copy of them. Event
        recipients only ever carry one or the other, so for them it's just
        whichever they have.
        """
        return self.email if self.email else f"tel:{self.phone}"


def normalize(email: str) -> str:
    """Canonical form for matching/dedup: trimmed + lower-cased."""
    return (email or "").strip().lower()


def identity_of(email: str | None, phone: str | None) -> str:
    """The :attr:`Parsed.identity` of a stored row (recipient or contact)."""
    e = normalize(email or "")
    return e if e else f"tel:{phone}"


def _split(text: str) -> list[str]:
    return [c.strip() for c in re.split(r"[,\n;]", text or "") if c.strip()]


def _named(chunk: str) -> tuple[str | None, str]:
    """Split "Name <address>" into its parts; a bare address has no name."""
    m = _NAMED_RE.match(chunk)
    if not m:
        return None, chunk
    return (m.group(1).strip() or None), m.group(2).strip()


def parse_recipients(text: str) -> tuple[list[Parsed], list[str]]:
    """Parse the email recipient list."""
    valid: list[Parsed] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for chunk in _split(text):
        name, raw = _named(chunk)
        email = normalize(raw)
        if not _EMAIL_RE.match(email):
            invalid.append(chunk)
            continue
        if email in seen:
            continue
        seen.add(email)
        valid.append(Parsed(name=name, email=email))
    return valid, invalid


def parse_phones(text: str) -> tuple[list[Parsed], list[str]]:
    """Parse the WhatsApp recipient list: E.164 numbers, or "Name <+1555...>".

    Numbers without a country code are rejected rather than guessed at (see
    ``core.phones``); they come back in the invalid list so the UI can ask for
    the "+1" instead of quietly messaging the wrong country.
    """
    valid: list[Parsed] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for chunk in _split(text):
        name, raw = _named(chunk)
        e164 = phones.normalize(raw)
        if e164 is None:
            invalid.append(chunk)
            continue
        if e164 in seen:
            continue
        seen.add(e164)
        valid.append(Parsed(name=name, email="", phone=e164))
    return valid, invalid
