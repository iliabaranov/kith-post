"""Application settings.

Precedence (highest first): explicit init args > env (``KITH_*``) > .env >
config.toml > built-in defaults. Secrets come from env/.env; non-secret tunables
may live in config.toml.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KITH_",
        env_file=".env",
        toml_file="config.toml",
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
