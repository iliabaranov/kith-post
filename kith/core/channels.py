"""The single source of truth for which channel a recipient is reached on.

``channel`` is authoritative. NULL predates the column and means email (the rule
the ``Recipient`` docstring already documents). The legacy phone==whatsapp
inference is kept ONLY for NULL rows that carry a phone, so nothing written
before the channel column existed is misrouted.

This lives apart from ``core.recipients`` because the send paths need to ask
"which channel is this row on?" without pulling in the compose-form parsers, and
because a phone number stops being a reliable answer the moment a second
phone-based channel exists.
"""

from __future__ import annotations

CHANNEL_EMAIL = "email"
CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_SMS = "sms"

ALL_CHANNELS = (CHANNEL_EMAIL, CHANNEL_WHATSAPP, CHANNEL_SMS)


def channel_of(recipient: object) -> str:
    """The channel a stored recipient is reached on.

    Takes ``object`` rather than a ``Recipient`` on purpose: this module stays
    free of the DB layer so it can be unit-tested against a plain stand-in, and
    the two attributes it reads are the same on any row-like object.
    """
    ch = getattr(recipient, "channel", None)
    if ch:
        return str(ch)
    return CHANNEL_WHATSAPP if getattr(recipient, "phone", None) else CHANNEL_EMAIL
