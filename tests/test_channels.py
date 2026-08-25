"""Mixed email + WhatsApp recipient lists: parsing, identity, reconciliation,
and the address book.

The load-bearing idea is *identity*: the email when there is one, else
"tel:<e164>". It's what keeps a WhatsApp-only person distinct (they all share the
empty-string email that the NOT NULL column forces) and what lets an event edit
preserve tokens and RSVPs instead of recreating rows.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kith.config import get_settings
from kith.core.recipients import (
    CHANNEL_EMAIL,
    CHANNEL_WHATSAPP,
    Parsed,
    identity_of,
    parse_phones,
)
from kith.db.models import Contact, Recipient, User
from kith.services import contacts as book


@pytest.fixture
def wa_client(monkeypatch):
    """A signed-in client with the WhatsApp channel on and a linked session.

    The channel is off by default (and settings are cached), so enabling it means
    a fresh app — same shape as the configured-OAuth test in test_auth.
    """
    monkeypatch.setenv("KITH_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("KITH_WAHA_API_KEY", "test-key")
    get_settings.cache_clear()
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            db, user = _db_and_user()
            user.wa_session = "utest"
            user.wa_status = "WORKING"
            user.wa_number = "+15550009999"
            db.commit()
            yield c
    finally:
        get_settings.cache_clear()


def _db_and_user():
    from kith.db.session import make_engine, make_session_factory

    db = make_session_factory(make_engine(get_settings().db_path))()
    return db, db.execute(select(User)).scalars().first()


# --- parsing + identity -------------------------------------------------------

def test_phone_entries_are_whatsapp_and_email_entries_are_not():
    assert Parsed(name=None, email="", phone="+15551234567").channel == CHANNEL_WHATSAPP
    assert Parsed(name=None, email="a@x.com").channel == CHANNEL_EMAIL


def test_identity_namespaces_phones_away_from_emails():
    # A number must never be able to collide with an address.
    assert Parsed(name=None, email="", phone="+15551234567").identity == "tel:+15551234567"
    assert Parsed(name=None, email="a@x.com").identity == "a@x.com"
    assert identity_of("A@X.com", None) == "a@x.com"
    assert identity_of("", "+15551234567") == "tel:+15551234567"


def test_two_whatsapp_people_have_different_identities():
    """The regression this whole design exists for: both carry email == "", so
    hashing the email alone would make them the same person."""
    a = Parsed(name="A", email="", phone="+15551110000")
    b = Parsed(name="B", email="", phone="+15552220000")
    assert a.identity != b.identity


def test_parse_phones_dedupes_across_formats():
    valid, invalid = parse_phones("Mara <+1 555 123 4567>\n+15551234567\n(555) 000-1111")
    assert [p.phone for p in valid] == ["+15551234567"]
    assert valid[0].name == "Mara"          # the named form wins, being first
    assert invalid == ["(555) 000-1111"]    # no country code: asked about, not guessed


# --- the address book ---------------------------------------------------------

def _user(client):
    """Sign in via dev-login and hand back the User row."""
    client.post("/auth/dev-login")
    return _db_and_user()


def test_whatsapp_only_contacts_stay_distinct(client):
    db, user = _user(client)
    a, created_a = book.add_contact(db, user.id, "", "Mara", phone="+15551110000")
    b, created_b = book.add_contact(db, user.id, "", "Sam", phone="+15552220000")
    assert created_a and created_b
    assert a.id != b.id
    assert a.email == b.email == ""          # the NOT NULL sentinel
    assert a.email_hash != b.email_hash      # ...but distinct identities
    assert db.execute(select(Contact)).scalars().all().__len__() == 2


def test_adding_the_same_number_twice_is_not_a_duplicate(client):
    db, user = _user(client)
    first, created = book.add_contact(db, user.id, "", "Mara", phone="+1 555 111 0000")
    again, created_again = book.add_contact(db, user.id, "", None, phone="+15551110000")
    assert created and not created_again
    assert first.id == again.id


def test_a_phone_can_be_added_to_an_existing_email_contact(client):
    db, user = _user(client)
    c, created = book.add_contact(db, user.id, "mara@example.com", "Mara")
    assert created and c.phone is None
    same, created_again = book.add_contact(
        db, user.id, "mara@example.com", "Mara", phone="+15551110000"
    )
    assert not created_again and same.id == c.id
    assert same.phone == "+15551110000"
    assert same.phone_hash == book.phone_hash("+15551110000")
    # Identity stays the email, so the contact isn't forked in two.
    assert same.email_hash == book.identity_hash("mara@example.com", None)


def test_find_by_phone(client):
    db, user = _user(client)
    book.add_contact(db, user.id, "mara@example.com", "Mara", phone="+15551110000")
    assert book.find_by_phone(db, user.id, "+15551110000").name == "Mara"
    assert book.find_by_phone(db, user.id, "+15557770000") is None


def test_a_contact_with_neither_address_is_refused(client):
    db, user = _user(client)
    assert book.add_contact(db, user.id, "", "Nobody") == (None, False)
    assert book.add_contact(db, user.id, "  ", None, phone="  ") == (None, False)


def test_an_unusable_number_is_refused_rather_than_dropped(client):
    # Offering a number we can't parse is an error, not a silently email-only contact.
    db, user = _user(client)
    assert book.add_contact(db, user.id, "a@x.com", "A", phone="555 123 4567") == (None, False)


def test_update_contact_can_set_and_clear_a_number(client):
    db, user = _user(client)
    c, _ = book.add_contact(db, user.id, "mara@example.com", "Mara")
    book.update_contact(db, user.id, c.id, "mara@example.com", "Mara", phone="+15551110000")
    assert db.get(Contact, c.id).phone == "+15551110000"
    book.update_contact(db, user.id, c.id, "mara@example.com", "Mara", phone="")
    assert db.get(Contact, c.id).phone is None
    assert db.get(Contact, c.id).phone_hash is None


def test_new_among_counts_a_whatsapp_person_as_new(client):
    db, user = _user(client)
    book.add_contact(db, user.id, "", "Mara", phone="+15551110000")
    fresh = book.new_among(db, user.id, [
        Parsed(name="Mara", email="", phone="+15551110000"),  # known
        Parsed(name="Sam", email="", phone="+15552220000"),   # new
        Parsed(name="Ali", email="ali@x.com"),                # new
    ])
    assert sorted(p.name for p in fresh) == ["Ali", "Sam"]


# --- event recipients ---------------------------------------------------------

def _event_with(client, *, emails="", phones=""):
    client.post("/auth/dev-login")
    r = client.post(
        "/events",
        data={"title": "Party", "recipients": emails, "wa_recipients": phones,
              "block_rsvp": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return r.headers["location"].split("/events/")[1].split("?")[0]


def _recipients(event_id):
    from kith.db.session import make_engine, make_session_factory

    db = make_session_factory(make_engine(get_settings().db_path))()
    return db, db.execute(
        select(Recipient).where(Recipient.event_id == event_id)
    ).scalars().all()


def test_compose_accepts_both_channels_at_once(client):
    ev = _event_with(client, emails="ali@example.com", phones="Mara <+15551110000>")
    _, rows = _recipients(ev)
    by_channel = {r.channel: r for r in rows}
    assert set(by_channel) == {CHANNEL_EMAIL, CHANNEL_WHATSAPP}
    assert by_channel[CHANNEL_EMAIL].email == "ali@example.com"
    assert by_channel[CHANNEL_EMAIL].phone is None
    wa = by_channel[CHANNEL_WHATSAPP]
    assert (wa.phone, wa.email, wa.name) == ("+15551110000", "", "Mara")
    assert len({r.token for r in rows}) == 2  # one opaque token each


def test_editing_preserves_a_whatsapp_recipients_token_and_rsvp(client):
    ev = _event_with(client, phones="+15551110000")
    db, rows = _recipients(ev)
    token, rid = rows[0].token, rows[0].id
    rows[0].status, rows[0].party_size = "coming", 2
    db.commit()

    # Re-save the same number, formatted differently, plus a new person.
    client.post(
        f"/events/{ev}",
        data={"title": "Party", "recipients": "", "block_rsvp": "on",
              "wa_recipients": "+1 (555) 111-0000\n+15552220000"},
        follow_redirects=False,
    )
    _, rows2 = _recipients(ev)
    kept = [r for r in rows2 if r.id == rid]
    assert len(kept) == 1, "the existing recipient must survive the edit"
    assert kept[0].token == token and kept[0].status == "coming" and kept[0].party_size == 2
    assert len(rows2) == 2


def test_removing_a_number_from_the_box_removes_the_recipient(client):
    ev = _event_with(client, phones="+15551110000\n+15552220000")
    client.post(
        f"/events/{ev}",
        data={"title": "Party", "recipients": "", "wa_recipients": "+15551110000",
              "block_rsvp": "on"},
        follow_redirects=False,
    )
    _, rows = _recipients(ev)
    assert [r.phone for r in rows] == ["+15551110000"]


def test_moving_someone_between_channels_is_a_new_invitation(client):
    """Email and WhatsApp are different conversations. Carrying an RSVP across
    would misreport which invite they actually answered."""
    ev = _event_with(client, emails="mara@example.com")
    db, rows = _recipients(ev)
    old_id, old_token = rows[0].id, rows[0].token
    client.post(
        f"/events/{ev}",
        data={"title": "Party", "recipients": "", "wa_recipients": "+15551110000",
              "block_rsvp": "on"},
        follow_redirects=False,
    )
    _, rows2 = _recipients(ev)
    assert len(rows2) == 1
    assert rows2[0].id != old_id and rows2[0].token != old_token
    assert rows2[0].channel == CHANNEL_WHATSAPP


def test_a_bad_number_does_not_create_a_recipient(client):
    ev = _event_with(client, phones="555 123 4567")
    _, rows = _recipients(ev)
    assert rows == []


def test_the_edit_page_splits_the_two_boxes_back_apart(wa_client):
    ev = _event_with(wa_client, emails="Ali <ali@example.com>", phones="Mara <+15551110000>")
    body = wa_client.get(f"/events/{ev}/edit").text
    assert "Ali &lt;ali@example.com&gt;" in body
    assert "Mara &lt;+15551110000&gt;" in body


def test_the_whatsapp_box_is_hidden_unless_the_channel_is_on(client):
    client.post("/auth/dev-login")
    body = client.get("/events/new").text
    assert 'name="wa_recipients"' not in body
    assert "Link your WhatsApp account" not in body  # not even offered when off


def test_an_unlinked_host_is_offered_the_link_instead_of_the_box(monkeypatch):
    monkeypatch.setenv("KITH_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("KITH_WAHA_API_KEY", "test-key")
    get_settings.cache_clear()
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            body = c.get("/events/new").text
            assert 'name="wa_recipients"' not in body
            assert "Link your WhatsApp account" in body
    finally:
        get_settings.cache_clear()


def test_the_box_appears_for_a_linked_host(wa_client):
    body = wa_client.get("/events/new").text
    assert 'name="wa_recipients"' in body
    assert "Include the country code" in body


def test_legacy_rows_with_no_channel_are_treated_as_email(client):
    """Every recipient written before this feature has channel NULL."""
    ev = _event_with(client, emails="ali@example.com")
    db, rows = _recipients(ev)
    rows[0].channel = None  # as an older row would be
    db.commit()
    # Re-saving the same list must recognise them, not duplicate them.
    client.post(
        f"/events/{ev}",
        data={"title": "Party", "recipients": "ali@example.com", "block_rsvp": "on"},
        follow_redirects=False,
    )
    _, rows2 = _recipients(ev)
    assert len(rows2) == 1 and rows2[0].id == rows[0].id


# --- the address book UI ------------------------------------------------------

def test_a_number_can_be_added_to_a_new_contact_from_the_page(wa_client):
    """The gap this closes: the service layer took a phone from the start, but
    the /contacts page had no field for one."""
    body = wa_client.get("/contacts").text
    assert 'name="phone"' in body
    wa_client.post("/contacts/add", data={
        "name": "Mara", "email": "mara@example.com",
        "phone": "+1 555 111 0000", "groups": "family",
    })
    db, user = _db_and_user()
    c = book.find_by_phone(db, user.id, "+15551110000")
    assert c is not None and c.name == "Mara" and c.email == "mara@example.com"


def test_a_number_can_be_added_to_an_existing_contact_from_the_page(wa_client):
    db, user = _db_and_user()
    c, _ = book.add_contact(db, user.id, "ali@example.com", "Ali")
    assert c.phone is None
    # The row's edit form carries the number field, pre-filled.
    assert 'class="contact-phone"' in wa_client.get("/contacts").text
    wa_client.post(f"/contacts/{c.id}/edit", data={
        "name": "Ali", "email": "ali@example.com", "phone": "+15554440000", "groups": "",
    })
    db2, _ = _db_and_user()
    assert db2.get(Contact, c.id).phone == "+15554440000"


def test_a_whatsapp_only_contact_can_be_added_without_an_email(wa_client):
    wa_client.post("/contacts/add", data={
        "name": "Gillian", "email": "", "phone": "+15554440000", "groups": "family",
    })
    db, user = _db_and_user()
    c = book.find_by_phone(db, user.id, "+15554440000")
    assert c is not None and c.name == "Gillian" and c.email == ""


def test_a_number_can_be_cleared_from_the_page(wa_client):
    db, user = _db_and_user()
    c, _ = book.add_contact(db, user.id, "ali@example.com", "Ali", phone="+15554440000")
    wa_client.post(f"/contacts/{c.id}/edit", data={
        "name": "Ali", "email": "ali@example.com", "phone": "", "groups": "",
    })
    db2, _ = _db_and_user()
    assert db2.get(Contact, c.id).phone is None


def test_an_unreachable_contact_is_reported_rather_than_silently_dropped(wa_client):
    r = wa_client.post("/contacts/add", data={"name": "Nobody", "email": "", "phone": ""},
                       follow_redirects=True)
    assert "invalid=1" in str(r.url) or "country code" in r.text
    assert "country code" in r.text


def test_a_number_without_a_country_code_is_reported(wa_client):
    r = wa_client.post("/contacts/add", data={
        "name": "Mara", "email": "mara@example.com", "phone": "555 111 0000",
    }, follow_redirects=True)
    assert "country code" in r.text
    db, user = _db_and_user()
    assert book.find_by_email(db, user.id, "mara@example.com") is None


def test_the_contacts_page_has_no_number_field_when_the_channel_is_off(client):
    client.post("/auth/dev-login")
    body = client.get("/contacts").text
    assert 'name="phone"' not in body
    assert "WhatsApp number" not in body


def test_using_a_contact_by_number_marks_them_even_if_they_have_an_email(wa_client):
    """mark_used matched only the identity hash, which for a contact holding both
    is their email — so inviting them over WhatsApp never bumped last_used_at.
    The same email-vs-phone key mismatch that broke the 'not in your book' prompt."""
    db, user = _db_and_user()
    c, _ = book.add_contact(db, user.id, "both@example.com", "Both", phone="+15551110000")
    assert c.last_used_at is None
    _event_with(wa_client, phones="+15551110000")     # invited by number only
    db2, _ = _db_and_user()
    assert db2.get(Contact, c.id).last_used_at is not None


def test_editing_a_contact_cannot_steal_a_number_another_one_holds(wa_client):
    """add_contact treats a number as a join key; editing has to as well, or two
    rows end up holding one number and find_by_phone picks arbitrarily."""
    db, user = _db_and_user()
    a, _ = book.add_contact(db, user.id, "", "PhoneOnly", phone="+15551110000")
    b, _ = book.add_contact(db, user.id, "b@example.com", "Other")
    assert book.update_contact(
        db, user.id, b.id, "b@example.com", "Other", phone="+15551110000"
    ) is None, "the edit should be refused, not duplicate the number"
    db2, _ = _db_and_user()
    assert db2.get(Contact, b.id).phone is None
    # ...and the original still owns it.
    assert book.find_by_phone(db2, user.id, "+15551110000").id == a.id
