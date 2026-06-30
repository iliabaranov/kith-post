"""Field-level encryption for PII + OAuth refresh tokens (Fernet).

``Cipher`` is pure and key-injected (easy to unit-test). ``default_cipher()``
builds one from settings; if no key is configured it falls back to an ephemeral
key with a loud warning (fine for a throwaway dev run, useless across restarts).
"""

from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet

log = logging.getLogger("kith")


def generate_key() -> str:
    """A fresh urlsafe base64 Fernet key."""
    return Fernet.generate_key().decode()


class Cipher:
    def __init__(self, key: str) -> None:
        self._f = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._f.decrypt(token.encode()).decode()


@lru_cache
def default_cipher() -> Cipher:
    from kith.config import get_settings  # local import avoids an import cycle

    key = get_settings().fernet_key
    if not key:
        key = generate_key()
        log.warning(
            "KITH_FERNET_KEY not set — using an ephemeral key. Encrypted data "
            "will NOT survive a restart. Set KITH_FERNET_KEY for anything real."
        )
    return Cipher(key)
