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


# Link-preview crawlers and prefetchers. When a chat app is handed a URL it
# fetches the page itself to build the little preview card — so the very act of
# sending an invitation produces a request for it, seconds before the recipient
# has seen anything. Counting that as "Opened" turns the one honest signal we
# have into a lie: on the first WhatsApp send it recorded an open 0.4s *before*
# the message finished sending.
#
# Matched as lowercase substrings of the User-Agent. Over-matching costs us an
# undercount, which the design already accepts; under-matching invents opens
# that never happened, which it does not.
_AUTOMATED_UA = (
    "whatsapp",              # WhatsApp's own preview fetch — the one that bit us
    "facebookexternalhit",   # Messenger/Instagram, and Meta link previews
    "facebookcatalog",
    "telegrambot",
    "twitterbot",
    "slackbot",              # also "Slack-ImgProxy"
    "slack-imgproxy",
    "discordbot",
    "linkedinbot",
    "skypeuripreview",
    "redditbot",
    "pinterest",
    "vkshare",
    "embedly",
    "quora link preview",
    "bingpreview",
    "googlebot",
    "bingbot",
    "applebot",
    "yandexbot",
    "duckduckbot",
    "ia_archiver",
    # Generic tokens, last: anything self-describing as a robot isn't a guest.
    "bot/",
    "crawler",
    "spider",
    "preview",
    "python-requests",
    "curl/",
    "wget/",
    "headlesschrome",
)

# Browsers and proxies that say outright "this is a speculative fetch".
_PREFETCH_HEADERS = (
    ("sec-purpose", ("prefetch", "prewarm", "prerender")),
    ("purpose", ("prefetch", "preview")),
    ("x-purpose", ("prefetch", "preview")),
    ("x-moz", ("prefetch",)),
)


def is_automated_fetch(user_agent: str | None, headers: dict | None = None) -> bool:
    """True when a request is a machine looking, not a person reading.

    Used to keep the "Opened" signal honest. The page is still served normally —
    a chat app's preview is useful to the recipient — we simply don't record it.
    """
    ua = (user_agent or "").lower()
    if any(token in ua for token in _AUTOMATED_UA):
        return True
    if not headers:
        return False
    lowered = {str(k).lower(): str(v).lower() for k, v in headers.items()}
    return any(
        any(value in lowered.get(name, "") for value in values)
        for name, values in _PREFETCH_HEADERS
    )
