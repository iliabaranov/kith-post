"""Opaque recipient tokens and RSVP status — pure, side-effect-free."""

from __future__ import annotations

import secrets
from enum import StrEnum


def new_token(nbytes: int = 24) -> str:
    """A single-purpose, URL-safe, >=128-bit opaque recipient token.

    24 bytes -> 192 bits of entropy, ~32 URL-safe chars. It reveals nothing
    about the recipient and maps to exactly one (event, recipient) pair.
    """
    return secrets.token_urlsafe(nbytes)


class RsvpStatus(StrEnum):
    queued = "queued"
    sent = "sent"
    opened = "opened"      # visited the invitation page (no tracking pixel)
    accepted = "accepted"
    declined = "declined"
    bounced = "bounced"


# Once a recipient reaches one of these, they've engaged — reminders stop.
ENGAGED: frozenset[RsvpStatus] = frozenset(
    {RsvpStatus.opened, RsvpStatus.accepted, RsvpStatus.declined}
)


def is_engaged(status: RsvpStatus) -> bool:
    """True if the recipient has engaged enough that reminders should stop."""
    return status in ENGAGED
