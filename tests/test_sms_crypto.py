"""The gateway's message encryption, pinned to the app's own scheme.

The reference vector was produced on 4 Sep 2026 by the official Python client
(android-sms-gateway 
 Encryptor(passphrase, iterations=75_000).encrypt(...)); if our decrypt reads
it, the phone will read ours.
"""

import re

import pytest

from kith.services import sms_crypto

PASSPHRASE = "correct horse battery staple"
REFERENCE = (
    "$aes-256-cbc/pbkdf2-sha1$i=75000$T/UIJjlaK3U337b2fWF02Q==$"
    "Y7HPBuj7Nso9YpRoFJivbfM+KzGratxM1ovnFErAidA="
)
WIRE = re.compile(r"^\$aes-256-cbc/pbkdf2-sha1\$i=(\d+)\$([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+)$")


def test_it_reads_the_official_clients_output():
    assert sms_crypto.decrypt(PASSPHRASE, REFERENCE) == "hello +15551234567"


def test_round_trip_and_wire_format():
    token = sms_crypto.encrypt(PASSPHRASE, "Come to the thing! https://x.test/i/abc")
    m = WIRE.match(token)
    assert m and int(m.group(1)) == sms_crypto.ITERATIONS
    assert sms_crypto.is_encrypted(token)
    assert sms_crypto.decrypt(PASSPHRASE, token) == "Come to the thing! https://x.test/i/abc"


def test_a_fresh_salt_every_call():
    a = sms_crypto.encrypt(PASSPHRASE, "same")
    b = sms_crypto.encrypt(PASSPHRASE, "same")
    assert a != b
    assert a.split("$")[3] != b.split("$")[3]


def test_the_wrong_passphrase_is_an_error_not_gibberish():
    token = sms_crypto.encrypt(PASSPHRASE, "+15551234567")
    with pytest.raises(sms_crypto.SmsCryptoError):
        sms_crypto.decrypt("not the passphrase at all", token)


@pytest.mark.parametrize("bad", [
    "hello",
    "$aes-256-cbc/pbkdf2-sha1$i=75000$short$AAAA",
    "$aes-256-cbc/pbkdf2-sha1$x=75000$T/UIJjlaK3U337b2fWF02Q==$AAAAAAAAAAAAAAAAAAAAAA==",
    "$aes-256-cbc/pbkdf2-sha1$i=75000$T/UIJjlaK3U337b2fWF02Q==$not*base64",
])
def test_malformed_tokens_are_refused(bad):
    with pytest.raises(sms_crypto.SmsCryptoError):
        sms_crypto.decrypt(PASSPHRASE, bad)


def test_a_short_passphrase_is_refused_up_front():
    with pytest.raises(sms_crypto.SmsCryptoError):
        sms_crypto.encrypt("short", "x")


def test_unicode_survives():
    text = "Café — 7pm ✨"
    assert sms_crypto.decrypt(PASSPHRASE, sms_crypto.encrypt(PASSPHRASE, text)) == text
