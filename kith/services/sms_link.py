"""The kith side of a host's own SMS setup — the counterpart of ``wa_session``.

WhatsApp is linked per host because a WhatsApp account *is* a person. SMS was
first built the other way round, one provider for the whole site, on the theory
that a Twilio account or a gateway phone is the operator's. On a single-host box
the two are the same person and the split is invisible; the moment a second
host wants to text from their own number it isn't. So this module gives each
host their own row (:class:`~kith.db.models.SmsLink`), and resolves which
configuration a given host's texts use:

    the host's own row, if it is complete  →  else the site's KITH_SMS_* settings
    →  else nothing.

Everything downstream — the send path, the reminder sweep, the compose form,
the webhooks — asks :func:`config_for` and gets back a plain
:class:`~kith.services.sms.SmsConfig`, never a row, so none of it knows or
cares where the settings came from. The site-wide settings keep working exactly
as before; a host's row is a layer on top, not a replacement.

Secrets are the one thing this module is careful about. They are stored
encrypted, they are never rendered back into a form (a blank secret field on
save means "keep what you have"), and a test send is the only way a host learns
whether they work — which is the same way anyone learns a password is right.
"""

from __future__ import annotations

import logging
import secrets as _secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.config import Settings
from kith.core import phones
from kith.db.models import SmsLink, User
from kith.services import sms, sms_crypto
from kith.services.sms_gateway import LOCAL_SERVER_PATH, RELAY_PATH

log = logging.getLogger("kith")

# The gateway events the webhook handler understands, registered together so a
# phone that reports deliveries also reports STOP replies — receiving one
# without the other is how a channel ends up honouring receipts and not people.
GATEWAY_EVENTS = ("sms:received", "sms:delivered", "sms:failed", "sms:cancelled")

# What a test send says. Short, plain, and obviously not an invitation.
TEST_TEXT = "Kith Post: texting is set up. This is a test message to you alone."

PROVIDERS = ("gateway", "twilio")


class SmsLinkError(ValueError):
    """Something the host typed doesn't work. The message is for them."""


def available(settings: Settings) -> bool:
    """Does this site let hosts set up their own texting at all?"""
    return settings.sms_host_links_enabled


def get(db: Session, user: User) -> SmsLink | None:
    return db.get(SmsLink, user.id)


def webhook_url(settings: Settings, link: SmsLink) -> str:
    """Where this host's gateway should POST. Public, because the phone is on the
    host's network and the app is behind the tunnel — the container's own
    address is not reachable from there."""
    return f"{settings.base_url.rstrip('/')}/sms/webhook/gateway/{link.webhook_token}"


def config_from_link(link: SmsLink, settings: Settings) -> sms.SmsConfig:
    """A host's row as the plain configuration the rest of the app works from."""
    return sms.SmsConfig(
        provider=link.provider,
        source="host",
        gateway_url=link.gateway_url or "",
        gateway_user=link.gateway_user or "",
        gateway_pass=link.gateway_pass or "",
        gateway_path=link.gateway_path or LOCAL_SERVER_PATH,
        gateway_device_id=link.gateway_device_id or "",
        gateway_passphrase=link.gateway_passphrase or "",
        twilio_account_sid=link.twilio_account_sid or "",
        twilio_auth_token=link.twilio_auth_token or "",
        twilio_from=link.twilio_from or "",
        twilio_messaging_service_sid=link.twilio_messaging_service_sid or "",
        sender_number=link.sender_number or link.twilio_from or "",
        self_number=link.self_number or "",
        webhook_secret=link.webhook_secret or "",
        # Twilio receipts come back to the one Twilio endpoint; the host is
        # found from the AccountSid in the POST, so the URL carries no token.
        status_callback=(
            f"{settings.base_url.rstrip('/')}/sms/webhook/twilio"
            if link.provider == "twilio" and link.webhook_secret else ""
        ),
        timeout=settings.sms_timeout_seconds,
    )


def config_for(db: Session, user: User | None, settings: Settings) -> sms.SmsConfig | None:
    """The configuration this host's texts use, or None if there is none.

    A host's own row wins when it is complete. An incomplete row — a provider
    chosen, a field still blank — does not fall through to the site's settings:
    a host who has started setting up their own texting would be surprised to
    find their invitations leaving from the operator's number in the meantime.
    """
    if user is not None and available(settings):
        link = get(db, user)
        if link is not None:
            cfg = config_from_link(link, settings)
            return cfg if cfg.configured else None
    return sms.SmsConfig.from_settings(settings)


def configured_for(db: Session, user: User | None, settings: Settings) -> bool:
    cfg = config_for(db, user, settings)
    return cfg is not None and cfg.configured


def self_number_for(db: Session, user: User | None, settings: Settings) -> str | None:
    """Where a self-only send for this host goes, normalised, or None."""
    cfg = config_for(db, user, settings)
    if cfg is None:
        return None
    return phones.normalize(cfg.self_number or "")


# --- lookups the webhooks need -----------------------------------------------

def by_token(db: Session, token: str) -> SmsLink | None:
    if not token:
        return None
    return db.execute(
        select(SmsLink).where(SmsLink.webhook_token == token)
    ).scalars().first()


def by_twilio_account(db: Session, account_sid: str) -> list[SmsLink]:
    """Every host row on this Twilio account. Usually one; two hosts sharing an
    account is possible and harmless, since they share the token too."""
    if not account_sid:
        return []
    return list(db.execute(
        select(SmsLink).where(
            SmsLink.provider == "twilio", SmsLink.twilio_account_sid == account_sid,
        )
    ).scalars().all())


# --- the page's verbs ----------------------------------------------------------

def _clean_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise SmsLinkError("Enter the phone's address — the app shows it under Local Server.")
    if not url.startswith(("http://", "https://")):
        raise SmsLinkError(
            "The phone's address should start with http:// or https:// — for example "
            "http://192.168.1.50:8080, or the phone's tailnet name with :8080."
        )
    # The path is its own setting; a URL that already ends in /message would
    # send to /message/message.
    for p in (LOCAL_SERVER_PATH, RELAY_PATH):
        if url.endswith(p):
            raise SmsLinkError(
                f"Leave the path off the address — just the host and port, without {p}."
            )
    return url


def _clean_phone(value: str, *, label: str, required: bool) -> str:
    value = (value or "").strip()
    if not value:
        if required:
            raise SmsLinkError(f"Enter {label}.")
        return ""
    e164 = phones.normalize(value)
    if e164 is None:
        raise SmsLinkError(
            f"{label[0].upper()}{label[1:]} doesn't look like a phone number with its "
            "country code — try the form +1 555 123 4567."
        )
    return e164


def save(
    db: Session,
    user: User,
    *,
    provider: str,
    gateway_url: str = "",
    gateway_user: str = "",
    gateway_pass: str = "",
    gateway_relay: bool = False,
    gateway_device_id: str = "",
    gateway_encrypt: bool = False,
    gateway_passphrase: str = "",
    twilio_account_sid: str = "",
    twilio_auth_token: str = "",
    twilio_from: str = "",
    twilio_messaging_service_sid: str = "",
    sender_number: str = "",
    self_number: str = "",
) -> SmsLink:
    """Create or update this host's row from the form. Raises SmsLinkError.

    A blank secret keeps the stored one, so the form never has to echo a
    secret back to fill itself in. Switching provider clears the other
    provider's fields: one way of texting at a time.
    """
    if provider not in PROVIDERS:
        raise SmsLinkError("Choose how to send: your phone, or Twilio.")
    link = get(db, user)
    fresh = link is None
    if fresh:
        link = SmsLink(
            user_id=user.id, provider=provider,
            webhook_token=_secrets.token_urlsafe(24),
            webhook_secret=_secrets.token_urlsafe(32),
        )
    assert link is not None
    switched = link.provider != provider
    link.provider = provider

    if provider == "gateway":
        link.gateway_url = _clean_url(gateway_url)
        user_ = (gateway_user or "").strip()
        pass_ = (gateway_pass or "").strip()
        if not user_:
            raise SmsLinkError("Enter the username the app shows under Local Server.")
        if not pass_ and (fresh or switched or not link.gateway_pass):
            raise SmsLinkError("Enter the password the app shows under Local Server.")
        link.gateway_user = user_
        if pass_:
            link.gateway_pass = pass_
        link.gateway_path = RELAY_PATH if gateway_relay else LOCAL_SERVER_PATH
        link.gateway_device_id = (gateway_device_id or "").strip() or None
        if gateway_encrypt:
            pp = (gateway_passphrase or "").strip()
            if not pp and not link.gateway_passphrase:
                raise SmsLinkError(
                    "Enter the encryption passphrase you set in the app under "
                    "Settings → Encryption."
                )
            if pp:
                if len(pp) < sms_crypto.MIN_PASSPHRASE:
                    raise SmsLinkError(
                        f"The encryption passphrase should be at least {sms_crypto.MIN_PASSPHRASE} "
                        "characters — the same long one you set on the phone."
                    )
                link.gateway_passphrase = pp
        else:
            # Off means off: a stored passphrase would keep encrypting texts the
            # phone can no longer read if the host also cleared it there.
            link.gateway_passphrase = None
        link.sender_number = _clean_phone(
            sender_number, label="the phone's own number", required=False,
        ) or None
        if switched:
            link.twilio_account_sid = link.twilio_auth_token = None
            link.twilio_from = link.twilio_messaging_service_sid = None
    else:
        sid = (twilio_account_sid or "").strip()
        if not sid.startswith("AC") or len(sid) < 10:
            raise SmsLinkError(
                "The Account SID starts with AC — copy it from the Twilio "
                "console's home page."
            )
        token = (twilio_auth_token or "").strip()
        if not token and (fresh or switched or not link.twilio_auth_token):
            raise SmsLinkError("Enter the Auth Token from the Twilio console.")
        mss = (twilio_messaging_service_sid or "").strip()
        if mss and not mss.startswith("MG"):
            raise SmsLinkError(
                "A Messaging Service SID starts with MG. Leave it blank to send "
                "from a number."
            )
        from_ = _clean_phone(twilio_from, label="the Twilio number to send from", required=not mss)
        link.twilio_account_sid = sid
        if token:
            link.twilio_auth_token = token
        link.twilio_from = from_ or None
        link.twilio_messaging_service_sid = mss or None
        link.sender_number = from_ or None
        if switched:
            link.gateway_url = link.gateway_user = link.gateway_pass = None
            link.gateway_path = link.gateway_device_id = link.gateway_passphrase = None
            link.webhooks_registered_at = None

    link.self_number = _clean_phone(self_number, label="your own mobile", required=False) or None
    if not fresh:
        link.updated_at = datetime.now(UTC)
        if switched:
            link.last_test_at = link.last_ok_at = None
            link.last_error = None
    db.add(link)
    db.commit()
    return link


def remove(db: Session, user: User) -> bool:
    link = get(db, user)
    if link is None:
        return False
    db.delete(link)
    db.commit()
    return True


def test_send(db: Session, link: SmsLink, settings: Settings) -> str | None:
    """Text the host's own number once. Returns the error to show, or None.

    Goes to ``self_number`` and nowhere else, whatever the send mode: this is a
    host checking their own credentials, and the only number it is safe to
    prove them on is their own. Recorded either way so the page can say when
    it last worked.
    """
    cfg = config_from_link(link, settings)
    link.last_test_at = datetime.now(UTC)
    to = phones.normalize(link.self_number or "")
    if not to:
        link.last_error = "Add your own mobile number above first — the test text goes to you."
        db.commit()
        return link.last_error
    if not cfg.configured:
        link.last_error = "Some settings are still missing."
        db.commit()
        return link.last_error
    try:
        sms.provider_from(cfg).send(to, TEST_TEXT)
    except sms.SmsError as e:
        link.last_error = _plain(e)
        db.commit()
        return link.last_error
    link.last_ok_at = datetime.now(UTC)
    link.last_error = None
    db.commit()
    return None


def register_gateway_webhooks(db: Session, link: SmsLink, settings: Settings) -> str | None:
    """Tell the phone to POST receipts and replies to this host's URL."""
    if link.provider != "gateway":
        return "Webhooks are only registered this way for the phone route."
    cfg = config_from_link(link, settings)
    if not cfg.configured:
        return "Some settings are still missing."
    from kith.services.sms_gateway import AndroidGatewayProvider

    provider = sms.provider_from(cfg)
    assert isinstance(provider, AndroidGatewayProvider)
    try:
        provider.register_webhooks(webhook_url(settings, link), GATEWAY_EVENTS)
    except sms.SmsError as e:
        link.last_error = _plain(e)
        db.commit()
        return link.last_error
    link.webhooks_registered_at = datetime.now(UTC)
    link.last_error = None
    db.commit()
    return None


def _plain(e: Exception) -> str:
    """A provider error in the host's terms, capped so a stack trace of an
    upstream HTML page doesn't end up on the settings page."""
    if isinstance(e, sms.SmsTimeout):
        return (
            "No answer in time. Is the phone awake, its Local Server on, and reachable from "
            "this site — on the same network, or on the tailnet?"
        )
    if isinstance(e, sms.SmsAuthError):
        return "The username or password was refused. Check them against what the app shows."
    return str(e)[:300]
