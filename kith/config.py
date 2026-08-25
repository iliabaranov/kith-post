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
