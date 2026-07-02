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
    secret_key: str = "dev-insecure-change-me"

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

    reminders: ReminderSettings = ReminderSettings()

    # Per-client rate limiting on the public + auth endpoints. On by default;
    # tests turn it off so limits don't bleed across the in-process suite.
    rate_limit_enabled: bool = True

    # Heavy full-res card images are deleted this many days after the event (or,
    # for dateless/orphaned cards, after creation). The small inline copy is kept
    # so the card still renders. 0 disables auto-purge.
    asset_retention_days: int = 30

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
