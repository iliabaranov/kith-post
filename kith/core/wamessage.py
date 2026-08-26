"""Compose the WhatsApp text for an invitation or a nudge — pure, testable.

A WhatsApp message is not a small email. It lands in a personal chat, next to
messages from the host's actual friends, so it reads like one of those: first name
only, one sentence, no announcement voice. "You're invited to Joe's 3rd Birthday"
is a printed card; "Hi Mara, it's Ilia, I've sent you this invite to Joe's 3rd
Birthday" is a person with a phone.

The host's **first name** is deliberate. WhatsApp already shows who a message is
from — it arrives from their own number — so spelling out the full name Google
happens to hold is both redundant and the stiffest thing in the message.

No emoji. The same template carries a birthday invitation and a condolence card,
and there is no emoji that is right for both.

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


def first_name(name: str | None) -> str:
    """"Ilia Baranov" -> "Ilia". Empty for anything unusable as a name.

    A chat message wants what friends call the host, not the name on their Google
    account. A mononym comes back as itself; the stand-ins other channels use for
    a nameless account would be mangled into "A", so they're rejected instead.
    """
    n = (name or "").strip()
    if not n or n.lower() in {"a friend", "friend", "someone"}:
        return ""
    return n.split()[0]


def _opening(recipient_name: str | None, host_name: str | None) -> str:
    """"Hi Mara, it's Ilia," — as much of that as we actually know."""
    who = (recipient_name or "").strip()
    host = first_name(host_name)
    if who and host:
        return f"Hi {who}, it's {host},"
    if who:
        return f"Hi {who},"
    if host:
        return f"Hi, it's {host},"
    return "Hi,"


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

    ``invitation`` decides whether this is an invite or a card (see
    ``core.eventkind``); ``rsvp`` decides whether we ask for an answer, since a
    dated card can be an invitation without collecting RSVPs.

    A card doesn't name itself. The picture *is* the message and carries the title
    already, and restating it reads badly when the title is a sentiment — "Thinking
    of you — for you."
    """
    what = (title or "").strip()
    opening = _opening(recipient_name, host_name)
    if invitation:
        body = (
            f"I've sent you this invite to {what}." if what else "I've sent you this invite."
        )
    else:
        body = "I've sent you this card."

    lines = [f"{opening} {body}"]
    if when:
        lines.append(when)
    if note:
        lines += ["", note.strip()]
    lines += [
        "",
        ("Have a look and let me know if you can come:" if rsvp else "Have a look:"),
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
    invitation: bool = True,
) -> str:
    """A follow-up in the same chat. Softer, shorter, and never a second pitch."""
    what = (title or "").strip()
    opening = _opening(recipient_name, host_name)
    thing = what or ("the invite" if invitation else "the card")
    body = (
        f"just a nudge about {thing} — still hoping you can come!"
        if rsvp
        else f"just a nudge about {thing}, in case it got buried."
    )
    lines = [f"{opening} {body}"]
    if when:
        lines.append(when)
    lines += ["", view_url]
    return "\n".join(lines)
