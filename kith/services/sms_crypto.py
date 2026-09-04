"""Message encryption for the Android SMS gateway.

The app (capcom6/android-sms-gateway) can decrypt message text and phone
numbers that a client encrypted with a passphrase set once on the phone
(Settings -> Encryption). This is that scheme, as its own source and the
official Python client implement it -- not a scheme of our choosing:

    key   = PBKDF2-HMAC-SHA1(passphrase, salt, iterations, 32 bytes)
    salt  = 16 random bytes, and it is ALSO the AES-CBC IV
    body  = AES-256-CBC(PKCS7(plaintext))
    wire  = "$aes-256-cbc/pbkdf2-sha1$i=<iterations>$<b64 salt>$<b64 body>"

Reusing the salt as the IV is the app's quirk. The salt is fresh per call, so
the IV is fresh per call, which is what CBC needs; it is simply not derived
separately. The KDF is deliberately cheap (the phone pays it per message), so
the passphrase carries the strength -- callers should insist on a long one.

Why it exists: the phone, not the gateway between us and it, should be the
only thing that can read a guest's number and the text. Over a tailnet that
is belt and braces; through a relay it is the whole point.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PREFIX = "$aes-256-cbc/pbkdf2-sha1$"
# The app's default. Kept as a constant, not a setting: the phone reads the
# count from the string, and a higher one only makes each text slower there.
ITERATIONS = 75_000
_SALT_LEN = 16
_KEY_LEN = 32
MIN_PASSPHRASE = 8


class SmsCryptoError(ValueError):
    """The token is not one of ours, or the passphrase is wrong."""


def _key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=_KEY_LEN, salt=salt, iterations=iterations)  # noqa: S303 — the app's scheme
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt(passphrase: str, text: str, *, iterations: int = ITERATIONS) -> str:
    """One field, encrypted for the app. Fresh salt (and so IV) every call."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise SmsCryptoError("passphrase too short")
    salt = os.urandom(_SALT_LEN)
    key = _key(passphrase, salt, iterations)
    padder = padding.PKCS7(128).padder()
    data = padder.update(text.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(salt)).encryptor()
    body = enc.update(data) + enc.finalize()
    return (
        f"{PREFIX}i={iterations}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(body).decode('ascii')}"
    )


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def decrypt(passphrase: str, token: str) -> str:
    """The inverse, for tests and for anything the phone might one day send back
    encrypted. Not on any request path today: the app posts webhooks in the clear."""
    if not is_encrypted(token):
        raise SmsCryptoError("not an encrypted token")
    parts = token.split("$")
    # ["", "aes-256-cbc/pbkdf2-sha1", "i=75000", salt, body]
    if len(parts) != 5 or not parts[2].startswith("i="):
        raise SmsCryptoError("malformed token")
    try:
        iterations = int(parts[2][2:])
        salt = base64.b64decode(parts[3], validate=True)
        body = base64.b64decode(parts[4], validate=True)
    except (ValueError, TypeError) as e:
        raise SmsCryptoError("malformed token") from e
    if len(salt) != _SALT_LEN or not body or len(body) % 16:
        raise SmsCryptoError("malformed token")
    key = _key(passphrase, salt, iterations)
    dec = Cipher(algorithms.AES(key), modes.CBC(salt)).decryptor()
    padded = dec.update(body) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        data = unpadder.update(padded) + unpadder.finalize()
    except ValueError as e:
        raise SmsCryptoError("wrong passphrase") from e
    return data.decode("utf-8")
