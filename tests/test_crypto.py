import pytest
from cryptography.fernet import InvalidToken

from kith.core.crypto import Cipher, generate_key


def test_roundtrip():
    c = Cipher(generate_key())
    assert c.decrypt(c.encrypt("dev@example.com")) == "dev@example.com"


def test_ciphertext_is_not_plaintext():
    c = Cipher(generate_key())
    token = c.encrypt("secret")
    assert token != "secret"
    assert token.startswith("gAAAA")  # Fernet token prefix


def test_wrong_key_cannot_decrypt():
    token = Cipher(generate_key()).encrypt("secret")
    with pytest.raises(InvalidToken):
        Cipher(generate_key()).decrypt(token)
