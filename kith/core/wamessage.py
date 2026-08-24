"""Compose the WhatsApp text for an invitation or a nudge — pure, testable.

A WhatsApp message is not a small email. It lands in a personal chat, so it stays
short, is addressed to the person by name where we know it, and says who it's
from — a bare link from a number reads like spam even when the number is a friend.

Nothing here tracks anything. The link is the same per-recipient invitation URL
the email uses, and every signal (opened, RSVP, headcount, allergies) is collected
on that page. There is no analytics parameter, no redirect, no shortener: a
recipient can read the whole message and see exactly where it goes.

Kept deliberately plain text. WhatsApp's own markup (*bold*, _italic_) would show
up literally in any client that doesn't render it, and the words don't need it.
"""

from __future__ import annotations

from datetime import date

from kith.core.calendar import pretty_time


def when_line(event_date: date | None, event_time: str | None) -> str | None:
    """"Sat, Jun 14 at 3:00 pm" — matching how dates read everywhere else.

    None when the card has no date, in which case the message just doesn't
    mention one (a dateless card is a card, not a broken invitation).
    """
    if event_date is None:
        return None
    day = event_date.strftime("%a, %b %d").replace(" 0", " ")
    if not event_time:
        return day
    return f"{day} at {pretty_time(event_time)}"


def _greeting(recipient_name: str | None, host_name: str) -> str:
    who = (recipient_name or "").strip()
    host = (host_name or "").strip()
    if who and host:
        return f"Hi {who} — it's {host}."
    if who:
        return f"Hi {who}!"
    if host:
        return f"Hi! It's {host}."
    return "Hi!"


def invite_text(
    *,
    title: str,
    host_name: str,
    view_url: str,
    recipient_name: str | None = None,
    when: str | None = None,
    rsvp: bool = True,
    note: str | None = None,
) -> str:
    """The first message: who it's from, what it is, when, and the link."""
    what = (title or "").strip()
    lines = [_greeting(recipient_name, host_name)]
    lines.append(f"You're invited to {what}." if what else "I've sent you a card.")
    if when:
        lines.append(when)
    if note:
        lines += ["", note.strip()]
    lines += [
        "",
        ("Have a look and let me know if you can make it:" if rsvp else "Have a look:"),
        view_url,
    ]
    return "\n".join(lines)


def reminder_text(
    *,
    title: str,
    host_name: str,
    view_url: str,
    recipient_name: str | None = None,
    when: str | None = None,
    rsvp: bool = True,
) -> str:
    """A follow-up in the same chat. Softer, shorter, and never a second pitch."""
    what = (title or "").strip() or "my invitation"
    who = (recipient_name or "").strip()
    opener = f"Hi {who} — " if who else "Hi — "
    lines = [
        opener
        + (
            f"just a gentle nudge about {what}. Still hoping you can come!"
            if rsvp
            else f"just a gentle nudge about {what}, in case it got buried."
        )
    ]
    if when:
        lines.append(when)
    lines += ["", view_url]
    return "\n".join(lines)
