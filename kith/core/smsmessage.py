"""Compose the SMS text for an invitation or a nudge — pure, testable.

SMS is billed and length-limited by segment: 160 GSM-7 characters, or 153 each
once a message spans several and the concatenation header eats into them. A
single character outside GSM-7 — most emoji, a curly quote pasted from a word
processor — switches the *whole* message to UCS-2 at 70, then 67. So this stays
plain ASCII where it can, drops the blank lines the WhatsApp message uses for
breathing room, and says the shortest true thing rather than the friendliest
long one. Thirty septets spent on "Have a look and let me know if you can make
it" is a second segment on a list of a hundred.

It is addressed by name when we know it and always says who it is from: unlike
WhatsApp, an SMS arrives from a number the recipient has never seen, so an
unattributed link is indistinguishable from a phishing text.

Nothing here tracks anything. The link is the same per-recipient invitation URL
email and WhatsApp use, with no analytics parameter, no shortener and no
redirect — a shortener would also hide the destination, which is exactly the
property that makes a text look like a scam.

There is no card image: SMS is text, and MMS is a different product with a
different price. The invitation page carries the picture.
"""

from __future__ import annotations

import math

# Reused rather than reimplemented: a date has to read the same in a text as it
# does in the email and on the page.
from kith.core.wamessage import when_line

__all__ = ["invite_text", "reminder_text", "segments", "when_line"]

# GSM 03.38, the default 7-bit alphabet. Anything here costs one septet.
_GSM_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# The extension table. Each of these is an ESC plus the character: two septets.
_GSM_EXTENDED = "\f^{}\\[~]|€"

_GSM_ALL = frozenset(_GSM_BASIC) | frozenset(_GSM_EXTENDED)

# Single / concatenated capacity, per encoding. The smaller figures are what is
# left once the 6-byte concatenation header is taken out of the first 140 bytes.
_GSM_SINGLE, _GSM_MULTI = 160, 153
_UCS2_SINGLE, _UCS2_MULTI = 70, 67


def _greeting(recipient_name: str | None, host_name: str) -> str:
    """The WhatsApp greeting's wording, with punctuation SMS can afford.

    Deliberately not imported from ``core.wamessage``: that one joins with an em
    dash, and an em dash is not in the 7-bit alphabet. One of them in the first
    line would push the entire message into UCS-2 and drop what fits from 160
    characters to 70 — so a hyphen it is. The wording is otherwise identical,
    and the tests here pin the encoding down so it stays that way.
    """
    who = (recipient_name or "").strip()
    host = (host_name or "").strip()
    if who and host:
        return f"Hi {who} - it's {host}."
    if who:
        return f"Hi {who}!"
    if host:
        return f"Hi! It's {host}."
    return "Hi!"


def is_gsm7(text: str) -> bool:
    """True when every character survives the 7-bit alphabet.

    One that doesn't costs the whole message: there is no per-character escape
    to UCS-2, so a single emoji more than halves what fits.
    """
    return all(ch in _GSM_ALL for ch in text)


def _gsm7_septets(text: str) -> int:
    return sum(2 if ch in _GSM_EXTENDED else 1 for ch in text)


def _ucs2_units(text: str) -> int:
    """UTF-16 code units, not code points.

    An emoji outside the BMP is a surrogate pair and so costs two, which is what
    the carrier counts. len() would say one.
    """
    return len(text.encode("utf-16-le")) // 2


def segments(text: str) -> int:
    """How many SMS segments this text costs to send.

    Segments are the unit of both billing and truncation, so the compose preview
    shows this number: a host who can see "2 segments" can decide to shorten the
    note, which they cannot do after pressing send.

    An empty message is still one segment. Concatenated GSM-7 has one subtlety
    this ignores: an escape pair may not straddle a segment boundary, so a
    message dense in {}[]~ characters can need one more than this says. It is
    within a segment of the truth, which is what the preview is for.
    """
    if is_gsm7(text):
        n, single, multi = _gsm7_septets(text), _GSM_SINGLE, _GSM_MULTI
    else:
        n, single, multi = _ucs2_units(text), _UCS2_SINGLE, _UCS2_MULTI
    if n <= single:
        return 1
    return math.ceil(n / multi)


def invite_text(
    *,
    title: str,
    host_name: str,
    view_url: str,
    recipient_name: str | None = None,
    when: str | None = None,
    rsvp: bool = True,
    note: str | None = None,
    invitation: bool = True,
) -> str:
    """The first message: who it's from, what it is, when, and the link.

    Same information as the WhatsApp invitation and in the same order, minus the
    blank lines — a text is read as one block anyway, and the whitespace is two
    septets that would rather be part of the title.

    ``invitation`` decides the phrasing (see ``core.eventkind``) and ``rsvp``
    whether we ask for an answer; a dated card can be an invitation without
    collecting RSVPs.
    """
    what = (title or "").strip()
    lines = [_greeting(recipient_name, host_name)]
    if invitation:
        lines.append(f"You're invited to {what}." if what else "You're invited.")
    else:
        # A card, not an event. "You're invited to Love you." is how you sound
        # when you've mistaken a note for a party.
        lines.append(f"I've sent you a card: {what}." if what else "I've sent you a card.")
    if when:
        lines.append(when)
    if note:
        lines.append(note.strip())
    lines.append("Can you make it?" if rsvp else "Have a look:")
    lines.append(view_url)
    return "\n".join(lines)


def reminder_text(
    *,
    title: str,
    host_name: str,
    view_url: str,
    recipient_name: str | None = None,
    when: str | None = None,
    rsvp: bool = True,
    invitation: bool = True,
) -> str:
    """A follow-up. Softer, shorter, and never a second pitch.

    It repeats who it is from, which the WhatsApp nudge can leave out: that one
    lands in an existing chat with a name on it, this one arrives from the same
    bare number as before.
    """
    what = (title or "").strip() or ("my invitation" if invitation else "the card I sent")
    who = (recipient_name or "").strip()
    host = (host_name or "").strip()
    opener = f"Hi {who} - " if who else "Hi - "
    if host:
        opener += f"{host} again. "
    lines = [
        opener
        + (
            f"Just a nudge about {what}. Still hoping you can come!"
            if rsvp
            else f"Just a nudge about {what}, in case it got buried."
        )
    ]
    if when:
        lines.append(when)
    lines.append(view_url)
    return "\n".join(lines)
