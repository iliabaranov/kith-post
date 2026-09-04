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

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger("kith")

# How the capcom6 gateway signs its webhook POSTs (docs.sms-gate.app): a hex
# HMAC-SHA256 over the raw body concatenated with the timestamp header, keyed
# with the signing key from the app's Settings -> Webhooks. Twilio signs
# completely differently — see services.sms_twilio.verify_twilio_signature —
# and the two must never be checked with each other's scheme.
GATEWAY_SIGNATURE_HEADER = "x-signature"
GATEWAY_TIMESTAMP_HEADER = "x-timestamp"

# How far out of date a signed webhook may be. The signature alone stops
# forgery but not replay: without this, one captured "delivered" POST could be
# re-sent forever. Five minutes is the gateway docs' own suggestion, and is
# generous enough for a phone with a lazy clock.
WEBHOOK_MAX_AGE_SECONDS = 300


def verify_gateway_webhook(
    secret: str,
    raw_body: bytes,
    signature: str | None,
    timestamp: str | None,
    *,
    now: float | None = None,
    max_age: int = WEBHOOK_MAX_AGE_SECONDS,
) -> bool:
    """Constant-time check that this POST really came from our gateway.

    Verifies HMAC-SHA256(secret, body + timestamp) against the hex signature,
    then that the timestamp is recent. Both halves matter: the HMAC proves the
    sender holds the key, the freshness check stops a captured POST being
    replayed.
    """
    if not secret or not signature or not timestamp:
        return False
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    # Absolute difference, so a clock that is ahead is as suspect as one behind.
    if abs((now if now is not None else time.time()) - sent_at) > max_age:
        return False
    expected = hmac.new(
        secret.encode(), raw_body + timestamp.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


# The opt-out keywords a carrier expects to be honoured, plus the ones people
# actually type. Matched on the whole trimmed, case-folded message: someone
# writing "stop by any time!" is making conversation, not unsubscribing.
STOP_KEYWORDS = frozenset({
    "stop", "stopall", "unsubscribe", "cancel", "end", "quit", "stop all",
})
# The documented way back in. Honoured because a number that opted out by
# accident otherwise has no route back at all — the host cannot clear it either.
# Deliberately not "yes": this app texts RSVP invitations, and a guest who once
# said STOP answering a later card with a bare "Yes" is replying, not
# re-consenting. Re-subscribing takes the word for it.
START_KEYWORDS = frozenset({"start", "unstop"})


def opt_out_intent(body: str) -> str | None:
    """"stop", "start", or None for an ordinary reply.

    Punctuation is stripped so "STOP." and "Stop!" count; anything with other
    words in it does not, because a message is only an opt-out if that is all
    it says.
    """
    word = (body or "").strip().strip(".!?,;:").casefold()
    word = " ".join(word.split())
    if word in STOP_KEYWORDS:
        return "stop"
    if word in START_KEYWORDS:
        return "start"
    return None


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


PROVIDER_NAMES = frozenset({"twilio", "gateway"})


@dataclass(frozen=True)
class SmsConfig:
    """Everything a provider needs, from wherever it was configured.

    Two sources produce one of these: the operator's ``KITH_SMS_*`` settings
    (the site-wide default) and a host's own row on ``/account/sms``
    (``services.sms_link``). The send path, the scheduler and the webhooks work
    from this and never ask which; ``source`` is kept only so a page can say
    where the settings came from. ``webhook_secret`` doubles as the switch for
    receipts, exactly as ``KITH_SMS_WEBHOOK_SECRET`` does for the site.
    """

    provider: str
    source: str = "site"                       # "site" | "host"
    gateway_url: str = ""
    gateway_user: str = ""
    gateway_pass: str = ""
    gateway_path: str = ""
    gateway_device_id: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""
    twilio_messaging_service_sid: str = ""
    sender_number: str = ""                    # the number guests see, if known
    self_number: str = ""                      # where a self-only send goes
    webhook_secret: str = ""
    status_callback: str = ""                  # Twilio receipts, "" for none
    timeout: float = 20.0

    @property
    def configured(self) -> bool:
        """Can this actually send: a known provider with its required fields."""
        if self.provider == "twilio":
            return bool(
                self.twilio_account_sid
                and self.twilio_auth_token
                and (self.twilio_from or self.twilio_messaging_service_sid)
            )
        if self.provider == "gateway":
            return bool(self.gateway_url and self.gateway_user and self.gateway_pass)
        return False

    @property
    def webhooks_configured(self) -> bool:
        return bool(self.configured and self.webhook_secret)

    @classmethod
    def from_settings(cls, settings) -> SmsConfig | None:  # noqa: ANN001 — a Settings
        """The site-wide default, or None when the operator left it off."""
        if not settings.sms_enabled:
            return None
        return cls(
            provider=settings.sms_provider,
            source="site",
            gateway_url=settings.sms_gateway_url,
            gateway_user=settings.sms_gateway_user,
            gateway_pass=settings.sms_gateway_pass,
            gateway_path=settings.sms_gateway_path,
            gateway_device_id=settings.sms_gateway_device_id,
            twilio_account_sid=settings.sms_twilio_account_sid,
            twilio_auth_token=settings.sms_twilio_auth_token,
            twilio_from=settings.sms_twilio_from,
            twilio_messaging_service_sid=settings.sms_twilio_messaging_service_sid,
            sender_number=settings.sms_twilio_from,
            self_number=settings.sms_self_number,
            webhook_secret=settings.sms_webhook_secret,
            status_callback=settings.sms_status_callback_url,
            timeout=settings.sms_timeout_seconds,
        )


def provider_from(config: SmsConfig | None) -> SmsProvider:
    """The provider for a resolved configuration.

    Concrete providers are reached only once ``configured`` says the channel
    can actually send; anything else is a NullProvider, which fails loudly at
    the point of sending rather than reaching a half-configured transport. An
    unrecognised provider name falls through the same way: a typo should stop
    the send with a logged reason, not take the app down at import.
    """
    if config is None or not config.configured:
        if config is not None and config.provider not in PROVIDER_NAMES | {"none", ""}:
            log.warning("sms: unknown provider %r; nothing will send", config.provider)
        return NullProvider()
    if config.provider == "twilio":
        # Imported lazily so the interface module stays free of transport code
        # and of httpx.
        from kith.services.sms_twilio import TwilioProvider

        return TwilioProvider(
            config.twilio_account_sid,
            config.twilio_auth_token,
            from_number=config.twilio_from,
            messaging_service_sid=config.twilio_messaging_service_sid,
            status_callback=config.status_callback,
            timeout=config.timeout,
        )
    from kith.services.sms_gateway import AndroidGatewayProvider

    return AndroidGatewayProvider(
        config.gateway_url,
        config.gateway_user,
        config.gateway_pass,
        device_id=config.gateway_device_id,
        path=config.gateway_path,
        timeout=config.timeout,
    )


def get_provider(settings) -> SmsProvider:  # noqa: ANN001 — a Settings
    """The provider for the site-wide settings alone.

    Kept for callers (and tests) that reason about the operator's configuration
    by itself. Anything acting for a host goes through
    ``services.sms_link.config_for`` and :func:`provider_from`, so a host's own
    settings are honoured.
    """
    return provider_from(SmsConfig.from_settings(settings))
