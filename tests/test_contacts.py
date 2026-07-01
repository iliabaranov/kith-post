"""Address-book service: encrypted-at-rest with blind-index dedup/lookup."""

import sqlite3

from sqlalchemy import text

from kith.config import get_settings
from kith.core.crypto import default_cipher
from kith.core.recipients import Parsed
from kith.db.models import User
from kith.db.session import init_db, make_engine, make_session_factory
from kith.services import contacts


def _count(table: str) -> int:
    return sqlite3.connect(get_settings().db_path).execute(
        f"SELECT COUNT(*) FROM {table}"
    ).fetchone()[0]


def _session(tmp_path):
    engine = make_engine(tmp_path / "s.sqlite3")
    init_db(engine)
    return make_session_factory(engine)()


def _user(db):
    u = User(google_sub="g", email="host@example.com", display_name="Mara")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_blind_index_is_deterministic_and_distinct():
    c = default_cipher()
    assert c.blind_index("a@x.com") == c.blind_index("a@x.com")
    assert c.blind_index("a@x.com") != c.blind_index("b@x.com")


def test_add_is_idempotent_and_case_insensitive(tmp_path):
    db = _session(tmp_path)
    u = _user(db)
    c1, created1 = contacts.add_contact(db, u.id, "Sam@Example.com", "Sam")
    assert created1 and c1.email == "sam@example.com"
    c2, created2 = contacts.add_contact(db, u.id, "  sam@example.com ")  # dup, different case/space
    assert created2 is False and c2.id == c1.id
    assert len(contacts.list_contacts(db, u.id)) == 1


def test_email_stored_encrypted_but_findable_by_blind_index(tmp_path):
    db = _session(tmp_path)
    u = _user(db)
    contacts.add_contact(db, u.id, "secret@example.com", "Secret")
    # raw SQL bypasses the decrypting column type -> we see what's on disk
    stored_email, stored_hash = db.execute(text("SELECT email, email_hash FROM contacts")).first()
    assert "@" not in stored_email  # ciphertext, not the address
    assert stored_hash and "@" not in stored_hash
    found = contacts.find_by_email(db, u.id, "SECRET@example.com")  # case-insensitive lookup
    assert found is not None and found.name == "Secret"


def test_import_text_counts_added_skipped_invalid(tmp_path):
    db = _session(tmp_path)
    u = _user(db)
    contacts.add_contact(db, u.id, "ana@x.com", "Ana")  # already in the book
    added, skipped, invalid = contacts.import_text(db, u.id, "ana@x.com\nben@x.com\nnope")
    assert added == 1 and skipped == 1 and invalid == ["nope"]  # ben added, ana skipped
    assert len(contacts.list_contacts(db, u.id)) == 2


def test_update_rejects_email_collision(tmp_path):
    db = _session(tmp_path)
    u = _user(db)
    a, _ = contacts.add_contact(db, u.id, "a@x.com", "A")
    contacts.add_contact(db, u.id, "b@x.com", "B")
    # renaming A's email onto B's should be refused
    assert contacts.update_contact(db, u.id, a.id, "b@x.com", "A") is None
    # a plain rename is fine
    ok = contacts.update_contact(db, u.id, a.id, "a2@x.com", "A2")
    assert ok is not None and ok.email == "a2@x.com"


def test_delete_is_owner_scoped(tmp_path):
    db = _session(tmp_path)
    u = _user(db)
    other = User(google_sub="g2", email="x@y.com", display_name="Other")
    db.add(other)
    db.commit()
    c, _ = contacts.add_contact(db, u.id, "a@x.com", "A")
    assert contacts.delete_contact(db, other.id, c.id) is False  # not their contact
    assert contacts.delete_contact(db, u.id, c.id) is True
    assert contacts.list_contacts(db, u.id) == []


def test_new_among_returns_only_unknown_people(tmp_path):
    db = _session(tmp_path)
    u = _user(db)
    contacts.add_contact(db, u.id, "known@x.com", "Known")
    parsed = [
        Parsed(name="Known", email="known@x.com"),
        Parsed(name="New1", email="new1@x.com"),
        Parsed(name="New1 dup", email="new1@x.com"),
    ]
    fresh = contacts.new_among(db, u.id, parsed)
    assert [p.email for p in fresh] == ["new1@x.com"]  # known dropped, dup collapsed


# ---- web routes (dev-login session) ----

def test_contacts_page_requires_login(client):
    assert client.get("/contacts", follow_redirects=False).status_code == 303


def test_add_and_list_via_page(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"name": "Ana", "email": "ana@x.com"}, follow_redirects=True)
    page = client.get("/contacts").text
    assert "ana@x.com" in page and "Ana" in page
    assert _count("contacts") == 1


def test_import_bulk_dedups(client):
    client.post("/auth/dev-login")
    client.post(
        "/contacts/import",
        data={"people": "ana@x.com\nBen <ben@x.com>\nana@x.com"},
        follow_redirects=True,
    )
    assert _count("contacts") == 2


def test_delete_via_page(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"name": "", "email": "gone@x.com"}, follow_redirects=True)
    cid = sqlite3.connect(get_settings().db_path).execute(
        "SELECT id FROM contacts LIMIT 1"
    ).fetchone()[0]
    client.post(f"/contacts/{cid}/delete", follow_redirects=True)
    assert _count("contacts") == 0


def test_export_csv(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"name": "Ana", "email": "ana@x.com"}, follow_redirects=True)
    r = client.get("/contacts/export")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    assert "name,email" in r.text and "ana@x.com" in r.text


def test_account_export_includes_contacts(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"name": "Ana", "email": "ana@x.com"}, follow_redirects=True)
    data = client.get("/account/export").json()
    assert any(c["email"] == "ana@x.com" for c in data["contacts"])


def test_account_delete_wipes_contacts(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"name": "Ana", "email": "ana@x.com"}, follow_redirects=True)
    client.post("/account/delete")
    assert _count("contacts") == 0


def test_compose_form_shows_importer_when_book_has_people(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"name": "Ana", "email": "ana@x.com"}, follow_redirects=True)
    f = client.get("/events/new").text
    assert "Import from your address book" in f
    assert 'data-email="ana@x.com"' in f
    assert 'id="bookAdd"' in f


def test_compose_form_hides_importer_when_book_empty(client):
    client.post("/auth/dev-login")
    assert "Import from your address book" not in client.get("/events/new").text
