"""Application settings.

Precedence (highest first): explicit init args > env (``KITH_*``) > .env >
config.toml > built-in defaults. Secrets come from env/.env; non-secret tunables
may live in config.toml.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

# The built-in session key. Fine for a laptop; on a public URL it means anyone
# holding this repo can forge a signed-in cookie, so `check_production_ready`
# refuses to start with it.
DEV_SECRET_KEY = "dev-insecure-change-me"


class SendMode(StrEnum):
    dry_run = "dry-run"      # compose real MIME -> data/outbox/*.eml, no Gmail call
    self_only = "self-only"  # actually send, but only to the logged-in user
    live = "live"            # normal sending


class ReminderSettings(BaseModel):
    """Automated-reminder defaults (§8). Per-event overrides live in Event.reminder_cfg.
    Override via env, e.g. KITH_REMINDERS__ENABLED=false, KITH_REMINDERS__TARGET=not-clicked."""

    enabled: bool = True
    target: str = "no-rsvp"                       # "no-rsvp" | "not-clicked"
    offsets: list[str] = ["halfway", "7d", "3d"]  # noqa: RUF012 — pydantic copies defaults
    send_hour_local: int = 9                       # ~9am sender-local, not the exact instant
    min_gap_hours: int = 24                        # merge reminders closer than this
    max_per_recipient: int = 3
    # background sweep interval in seconds (0 disables the loop)
    sweep_seconds: int = 300


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KITH_",
        env_file=".env",
        toml_file="config.toml",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "Kith Post"
    base_url: str = "http://localhost:8000"
    send_mode: SendMode = SendMode.dry_run
    data_dir: Path = Path("data")
    # Deliberately a recognisable placeholder, so the startup check below can
    # tell "never configured" from "configured to something".
    secret_key: str = DEV_SECRET_KEY

    # Encryption of PII + OAuth refresh tokens at rest (Fernet). Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    fernet_key: str = ""

    # Google OAuth (G1). When unset, /auth/login offers a local dev sign-in.
    google_client_id: str = ""
    google_client_secret: str = ""

    # Access control. When ``allowed_emails`` is non-empty, only those addresses
    # may sign in; everyone else gets the "request access" page. Comma/space
    # separated. Empty = rely on Google's OAuth test-user list (the default).
    # If you enable it, include your OWN Google address or you'll lock yourself out.
    allowed_emails: str = ""
    # Shown to people who can't get in, so they know who to ask for access.
    contact_email: str = ""

    # --- WhatsApp channel (via a self-hosted WAHA container) ---
    # Off by default: WAHA is an unofficial WhatsApp client, so the channel only
    # appears once the operator has read the warning and opted in on the box.
    whatsapp_enabled: bool = False
    waha_url: str = "http://waha:3000"
    waha_api_key: str = ""
    # Every WAHA call is bounded. Calls against a session that isn't WORKING do
    # not fail fast — sendText and contacts/check-exists were observed hanging
    # indefinitely, and sessions/{s}/timelock blocked for minutes before a 500 —
    # so a timeout is the only thing standing between a stuck session and a
    # wedged request handler (or a wedged reminder sweep).
    waha_timeout_seconds: float = 20.0
    # Where WAHA should POST delivery/read receipts and session-status changes.
    # The compose-network address, not base_url: a public URL would leave the box
    # and come back through the tunnel for no reason. Empty secret = no webhooks
    # configured at all, and the endpoint refuses everything.
    waha_webhook_url: str = "http://kith:8000/wa/webhook"
    waha_webhook_secret: str = ""

    # Pause between consecutive WhatsApp sends, drawn fresh at random from this
    # range each time. Messaging many contacts in a burst is what triggers
    # WhatsApp's reachout timelock (error 463) — and a metronome-steady gap is
    # its own tell that a machine is typing, since a person working through a
    # list takes a breath of a different length between each one. Both 0 sends
    # flat out (tests do that).
    waha_send_gap_min_seconds: float = 5.0
    waha_send_gap_max_seconds: float = 20.0

    # --- SMS channel ---
    # Off by default, and unlike WhatsApp this one is instance-level: the
    # operator configures a provider once for the box, rather than each host
    # linking their own. So there is nothing per-host to set up and no linking
    # page — the channel is simply there or it isn't.
    sms_enabled: bool = False
    # "none" | "twilio" | "gateway". Concrete providers land in later changes.
    sms_provider: str = "none"
    sms_timeout_seconds: float = 20.0
    # Pause between consecutive SMS sends, drawn fresh at random from this range.
    # Lower than WhatsApp's: a carrier throttles or filters a burst rather than
    # banning the number, so the stakes are smaller — but a hundred texts fired
    # flat out still get spam-filtered, and an even cadence is its own tell.
    sms_send_gap_min_seconds: float = 1.0
    sms_send_gap_max_seconds: float = 4.0
    # Shared secret for delivery-receipt and STOP callbacks. Empty = no webhooks
    # configured at all, and the endpoint refuses everything.
    sms_webhook_secret: str = ""
    # Where self-only mode sends a text: the operator's own number, in any
    # readable form — it is normalised to E.164 before use. WhatsApp's self-only
    # borrows the host's linked number; SMS has no per-host identity, so this is
    # instance-level like the rest of the channel. Unset, self-only holds every
    # text rather than guessing at a destination.
    sms_self_number: str = ""

    # Twilio SMS provider (KITH_SMS_PROVIDER=twilio)
    sms_twilio_account_sid: str = ""
    sms_twilio_auth_token: str = ""
    # The sender: a Twilio number in E.164 (e.g. +15551234567) OR a Messaging
    # Service SID (starts with "MG"). If both are set the Messaging Service
    # wins — it is the more specific instruction, and it picks the number itself.
    sms_twilio_from: str = ""
    sms_twilio_messaging_service_sid: str = ""

    # Android SMS gateway provider (KITH_SMS_PROVIDER=gateway)
    # Where the gateway is: the phone's own Local Server
    # (http://<phone-ip>:8080) or a self-hosted relay. Reached over the LAN or
    # the compose network, never the public tunnel — not being reachable is the
    # gateway's whole security model.
    sms_gateway_url: str = ""
    sms_gateway_user: str = ""            # Basic auth username (shown in the app)
    sms_gateway_pass: str = ""            # Basic auth password
    # The send path, which differs by deployment shape: "/message" for the
    # app's on-device Local Server, "/3rdparty/v1/messages" for the relay or
    # cloud server. See kith.services.sms_gateway for both constants.
    sms_gateway_path: str = "/message"
    # Only needed when a relay fronts more than one phone.
    sms_gateway_device_id: str = ""

    reminders: ReminderSettings = ReminderSettings()

    # Per-client rate limiting on the public + auth endpoints. On by default;
    # tests turn it off so limits don't bleed across the in-process suite.
    rate_limit_enabled: bool = True

    # Heavy full-res card images are deleted this many days after the event (or,
    # for dateless/orphaned cards, after creation). The small inline copy is kept
    # so the card still renders. 0 disables auto-purge.
    asset_retention_days: int = 30

    @property
    def waha_webhooks_configured(self) -> bool:
        """Receipts are opt-in: they need a shared secret to be trustworthy."""
        return bool(self.whatsapp_enabled and self.waha_webhook_url
                    and self.waha_webhook_secret)

    @property
    def sms_webhooks_configured(self) -> bool:
        """Receipts and STOP handling are opt-in, and need a secret to be on.

        The secret turns the channel's webhooks on; each endpoint then exists
        only for the provider that is configured. The gateway path uses the
        secret as its signing key; the Twilio path verifies Twilio's own
        signature with the account auth token instead — so on a Twilio box the
        secret is a switch, and on a gateway box it is the key, and neither
        provider's endpoint is left answering for the other.
        """
        return bool(self.sms_enabled and self.sms_webhook_secret)

    @property
    def sms_status_callback_url(self) -> str:
        """Where Twilio should POST delivery receipts, or "" for none.

        The public base_url, unlike the WhatsApp webhook's compose-internal
        address: this callback arrives from Twilio's servers over the internet,
        so it has to be reachable from outside the box. Empty when receipts are
        off, which is what stops a callback URL being registered at all.
        """
        if not self.sms_webhooks_configured:
            return ""
        return f"{self.base_url.rstrip('/')}/sms/webhook/twilio"

    @property
    def sms_configured(self) -> bool:
        """The channel is usable: enabled, with a provider that can actually send.

        A provider that is named but not credentialed is not configured. The
        alternative is a compose box that appears, accepts numbers, and then
        fails on the first live send — the failure belongs at startup, in the
        operator's config, not in front of a host mid-party-planning.
        """
        if not self.sms_enabled:
            return False
        if self.sms_provider == "twilio":
            return bool(
                self.sms_twilio_account_sid
                and self.sms_twilio_auth_token
                and (self.sms_twilio_from or self.sms_twilio_messaging_service_sid)
            )
        if self.sms_provider == "gateway":
            # The credentials matter as much as the URL: the app's Local Server
            # only supports Basic auth, so an unauthenticated call is a 401
            # rather than a send.
            return bool(
                self.sms_gateway_url
                and self.sms_gateway_user
                and self.sms_gateway_pass
            )
        return False

    @property
    def whatsapp_configured(self) -> bool:
        """The channel is usable: enabled, and we know where WAHA is + how to auth."""
        return bool(self.whatsapp_enabled and self.waha_url and self.waha_api_key)

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def allowed_email_set(self) -> frozenset[str]:
        return frozenset(
            e.strip().lower()
            for e in self.allowed_emails.replace(",", " ").split()
            if e.strip()
        )

    def email_allowed(self, email: str) -> bool:
        """True if this email may sign in. An empty allowlist permits anyone who
        made it through Google (i.e. Google's test-user list is the gate)."""
        allow = self.allowed_email_set
        return not allow or (email or "").strip().lower() in allow

    def check_production_ready(self) -> list[str]:
        """Configuration mistakes that must not run on a public URL.

        Only enforced when base_url is https, i.e. when the deployment is
        actually reachable: a local http run is allowed to be insecure, which is
        the point of the defaults.
        """
        if not self.https_only:
            return []
        problems = []
        if self.secret_key == DEV_SECRET_KEY or not self.secret_key:
            problems.append(
                "KITH_SECRET_KEY is still the built-in default — session cookies "
                "would be forgeable by anyone with a copy of this repo. Generate "
                'one: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if not self.fernet_key:
            problems.append(
                "KITH_FERNET_KEY is unset — PII would be encrypted with a "
                "throwaway key written under the data dir, and a lost key means "
                "unreadable rows. Generate one and back it up."
            )
        return problems

    @property
    def https_only(self) -> bool:
        return self.base_url.startswith("https")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "kith.sqlite3"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # order = priority; env beats the TOML file
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
