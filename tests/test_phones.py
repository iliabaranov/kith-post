"""Phone parsing for the WhatsApp channel. A wrong number here messages a
stranger, so the rules are deliberately strict about country codes."""

import pytest

from kith.core import phones


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+15551234567", "+15551234567"),
        ("+1 (555) 123-4567", "+15551234567"),
        ("  +1.555.123.4567  ", "+15551234567"),
        ("00 1 555 123 4567", "+15551234567"),          # ITU trunk prefix
        ("+44 20 7946 0958", "+442079460958"),
        ("+1 555‐123‐4567", "+15551234567"),  # nbsp + unicode dashes
    ],
)
def test_normalize_accepts_formatting_noise(raw, expected):
    assert phones.normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "555 123 4567",     # no country code — we refuse to guess the country
        "(555) 123-4567",
        "+1 555",           # too short
        "+1555123456789012345",  # over E.164's 15 digits
        "+0555123456",      # country codes don't start at 0
        "friend@example.com",
        "",
        None,
    ],
)
def test_normalize_rejects_ambiguous_or_bad(raw):
    assert phones.normalize(raw) is None


def test_is_phone_like_separates_a_bad_number_from_a_bad_email():
    # Both fail to normalize; the UI needs to complain about the right thing.
    assert phones.is_phone_like("+1 555") is True
    assert phones.is_phone_like("not-an-email") is False
    assert phones.is_phone_like("555 123 4567") is False  # no "+" = not clearly a phone


def test_chat_id_round_trip():
    assert phones.chat_id("+15551234567") == "15551234567@c.us"
    assert phones.from_chat_id("15551234567@c.us") == "+15551234567"
    # WAHA also reports a device-suffixed jid; the device part isn't the number.
    assert phones.from_chat_id("15551234567:12@s.whatsapp.net") == "+15551234567"


def test_session_name_is_short_and_alphanumeric():
    name = phones.session_name("a1b2-C3D4-e5")
    assert name == "ua1b2c3d4e5"
    assert name.isalnum() and name[0].isalpha()


def test_session_names_are_distinct_per_user():
    import uuid

    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    assert phones.session_name(a) != phones.session_name(b)
