"""The SMS channel's transport seam: what a provider is, and which one to use.

SMS has no single self-hosted transport the way WhatsApp has WAHA. It has at
least two shapes worth supporting — a paid carrier API, and a phone of your own
running a gateway app — and they differ in cost, in registration burden and in
the terms you are agreeing to, not in anything the send path cares about. So the
send path talks to this interface and never to a provider directly.

The error hierarchy mirrors ``services.waha``: one base, and specific subclasses
for the cases a caller can act on differently. A provider maps its own failures
onto these, so ``services.send`` handles a Twilio 401 and a gateway 403 with the
same branch.

Nothing here touches the network, and in this state nothing behind it does
either — ``NullProvider`` is all there is. Concrete providers arrive later and
are reached through :func:`get_provider`, which is the only thing that needs to
know they exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger("kith")


class SmsError(Exception):
    """Base for every SMS transport failure."""


class SmsAuthError(SmsError):
    """The provider rejected our credentials. Retrying will not help."""


class SmsTimeout(SmsError):
    """The provider did not answer inside the configured timeout."""


class SmsNotConfigured(SmsError):
    """Asked to send with no provider configured.

    Raised rather than returned so a misconfigured live send fails loudly. The
    alternative — a no-op that reports success — flips recipients to 'sent' and
    tells the host their invitations went out when nothing left the building.
    """


class SmsMisconfigured(SmsError):
    """The provider rejected something about *our* setup, not this recipient.

    A sender number that isn't one, a path that isn't there, an account id the
    provider has never heard of: every remaining recipient would fail the same
    way, so the batch stops rather than spending a paced API call per person to
    learn the same thing thirty times over.
    """


class SmsRateLimited(SmsError):
    """The provider asked us to slow down. The batch stops; the rest stay queued."""


@dataclass(frozen=True)
class SmsResult:
    """What came back from a successful send.

    ``message_id`` is the provider's own handle for the message, kept so a later
    delivery receipt can be matched to the recipient it belongs to. Optional
    because not every provider returns one, and a send that worked without giving
    us an id is still a send that worked.
    """

    message_id: str | None


@dataclass(frozen=True)
class SmsCaps:
    """What a provider can do beyond sending, so the UI need not guess.

    Both default to False: a provider that has not said it posts receipts is
    treated as one that does not, which shows the host nothing rather than a
    delivery line that will never fill in.
    """

    can_receipt: bool = False   # posts delivery-status callbacks
    can_inbound: bool = False   # posts inbound messages (which is how STOP arrives)


@runtime_checkable
class SmsProvider(Protocol):
    """The whole interface. Two methods, one of which is a constant."""

    def send(self, to_e164: str, text: str) -> SmsResult:
        """Send ``text`` to one E.164 number, or raise an :class:`SmsError`."""
        ...

    def capabilities(self) -> SmsCaps:
        ...


class NullProvider:
    """The provider when SMS is unconfigured.

    Never reached on the dry-run path — dry-run writes the outbox and does not
    call a provider at all — so raising here costs nothing in testing and turns
    "live mode with no provider set" into a logged failure with the recipients
    left queued, rather than a silent drop.
    """

    def send(self, to_e164: str, text: str) -> SmsResult:
        raise SmsNotConfigured("no SMS provider configured")

    def capabilities(self) -> SmsCaps:
        return SmsCaps()


def get_provider(settings) -> SmsProvider:  # noqa: ANN001 — a Settings
    """The provider this instance is configured for.

    SMS is instance-level: one provider for the box, chosen by the operator,
    rather than something each host links for themselves the way WhatsApp is.
    Concrete providers are reached only once ``sms_configured`` says the
    channel can actually send.

    An unrecognised provider name falls through to NullProvider rather than
    raising here: a typo in the config should stop the send loudly at the point
    of sending, not take the whole app down at import.
    """
    if not settings.sms_configured:
        # Off, or on with no usable provider: fail loudly at the point of
        # sending rather than reach a half-configured transport.
        return NullProvider()
    if settings.sms_provider == "twilio":
        # Imported lazily so the interface module stays free of transport code
        # and of httpx.
        from kith.services.sms_twilio import TwilioProvider

        return TwilioProvider(
            settings.sms_twilio_account_sid,
            settings.sms_twilio_auth_token,
            from_number=settings.sms_twilio_from,
            messaging_service_sid=settings.sms_twilio_messaging_service_sid,
            timeout=settings.sms_timeout_seconds,
        )
    if settings.sms_provider != "none":
        log.warning("sms: unknown provider %r; nothing will send", settings.sms_provider)
    return NullProvider()
