"""Field-level encryption for PII + OAuth refresh tokens (Fernet).

``Cipher`` is pure and key-injected (easy to unit-test). ``default_cipher()``
builds one from settings; if no key is configured it falls back to an ephemeral
key with a loud warning (fine for a throwaway dev run, useless across restarts).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet

log = logging.getLogger("kith")


def generate_key() -> str:
    """A fresh urlsafe base64 Fernet key."""
    return Fernet.generate_key().decode()


class Cipher:
    def __init__(self, key: str) -> None:
        self._key = key.encode()
        self._f = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._f.decrypt(token.encode()).decode()

    def blind_index(self, value: str) -> str:
        """Deterministic keyed hash for equality lookups on encrypted PII.

        Fernet ciphertext is randomized, so two rows with the same email don't
        match and can't be UNIQUE/queried. This HMAC lets us dedupe and look up
        ("already in your book?") without storing plaintext. Caller normalizes
        (e.g. lower-case the email) before hashing.
        """
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()


def _load_or_create_dev_key(data_dir: Path) -> str:
    """Persist a dev key under the data dir so it survives restarts.

    Without this, an ephemeral key would change every boot and make previously
    encrypted rows undecryptable (a hard crash on the next read). Production sets
    KITH_FERNET_KEY explicitly and never reaches here.
    """
    key_file = data_dir / ".fernet.dev.key"
    if key_file.exists():
        return key_file.read_text().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = generate_key()
    key_file.write_text(key)
    with contextlib.suppress(OSError):
        key_file.chmod(0o600)
    log.warning(
        "KITH_FERNET_KEY not set — generated a persistent DEV key at %s. "
        "Set KITH_FERNET_KEY (and back it up) for anything real.",
        key_file,
    )
    return key


@lru_cache
def default_cipher() -> Cipher:
    from kith.config import get_settings  # local import avoids an import cycle

    settings = get_settings()
    key = settings.fernet_key or _load_or_create_dev_key(settings.data_dir)
    return Cipher(key)
