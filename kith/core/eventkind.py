"""Is this an invitation or just a card? — pure, and the single source of truth.

Kith Post sends two quite different things through the same machinery. Something
that asks for a reply, or is pinned to a date, is an **invitation**. An image with
a title and a few words — a holiday card, a thank-you, a "love you" — is a
**card**, and calling it an invitation makes the product sound like it wasn't
paying attention.

The rule lived in one host-facing helper and was implicit everywhere else, which
is exactly how a WhatsApp message came to read "You're invited to Love you."
"""

from __future__ import annotations

from datetime import date


def is_invitation(blocks: dict | None, event_date: date | None) -> bool:
    """True when this card asks something of the recipient, or names a day."""
    b = blocks or {}
    return bool(b.get("rsvp") or (b.get("date") and event_date))


def noun(blocks: dict | None, event_date: date | None) -> str:
    """"invitation" or "card", for copy that needs to name the thing."""
    return "invitation" if is_invitation(blocks, event_date) else "card"
