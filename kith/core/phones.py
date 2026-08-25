"""Phone numbers for the WhatsApp channel — pure, side-effect-free.

We deliberately require a country code (a leading ``+``). Guessing a default
region from a bare "555 123 4567" would silently mis-address an invitation to a
stranger in another country, which is worse than asking the host to type "+1".
Formatting noise (spaces, dashes, dots, parens, a leading "00" trunk prefix) is
forgiven; ambiguity is not.

E.164 allows at most 15 digits; we also reject anything under 8, which rules out
short codes and most typos without pretending to know each country's rules.
"""

from __future__ import annotations

import re

# Formatting noise we forgive: whitespace, ASCII punctuation, non-breaking
# space, and the unicode dash block (U+2010-U+2015) that numbers pasted
# from a contacts app tend to carry.
_JUNK_RE = re.compile("[\\s\\-().,/\u00a0\u2010-\u2015]")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize(phone: str) -> str | None:
    """Canonical E.164 ("+15551234567"), or None if it isn't a usable number.

    Accepts "+1 (555) 123-4567", "00 1 555 123 4567" and friends. Returns None
    for a bare national number, since we can't know the country.
    """
    s = _JUNK_RE.sub("", phone or "")
    if not s:
        return None
    if s.startswith("00"):  # international trunk prefix — the ITU spelling of "+"
        s = "+" + s[2:]
    if not s.startswith("+"):
        return None
    return s if _E164_RE.match(s) else None


def is_phone_like(text: str) -> bool:
    """True if this chunk was clearly *meant* as a phone number.

    Used to tell a malformed number ("+1 555") apart from a malformed email, so
    the UI can complain about the right thing. Intentionally loose.
    """
    s = _JUNK_RE.sub("", text or "")
    if s.startswith("00"):
        s = "+" + s[2:]
    return bool(s) and s.startswith("+") and s[1:].isdigit()


def digits(phone_e164: str) -> str:
    """Just the digits of an E.164 number — WhatsApp ids carry no "+"."""
    return phone_e164.lstrip("+")


def chat_id(phone_e164: str) -> str:
    """WhatsApp chat id for a personal number, e.g. "15551234567@c.us"."""
    return f"{digits(phone_e164)}@c.us"


def from_chat_id(chat_id_: str) -> str:
    """Inverse of :func:`chat_id` — "15551234567@c.us" -> "+15551234567"."""
    return "+" + (chat_id_ or "").split("@", 1)[0].split(":", 1)[0]


def session_name(user_id: str) -> str:
    """WAHA session name for a kith user.

    WAHA session names want to be short and alphanumeric; our ids are UUIDs, so
    strip anything else and prefix a letter ("u1a2b..."). One session per user.
    """
    safe = re.sub(r"[^a-z0-9]", "", (user_id or "").lower())
    return f"u{safe}"
