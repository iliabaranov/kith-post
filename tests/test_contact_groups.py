"""G7-P3: address-book group tags (multiple per contact) + filtering."""

import json
import sqlite3

from kith.config import get_settings


def _db():
    return sqlite3.connect(get_settings().db_path)


def _groups_of_first():
    v = _db().execute("SELECT groups FROM contacts ORDER BY created_at LIMIT 1").fetchone()[0]
    return json.loads(v) if v else []


def _cid():
    return _db().execute("SELECT id FROM contacts ORDER BY created_at LIMIT 1").fetchone()[0]


def test_add_contact_with_groups(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"email": "a@example.com", "name": "Al",
                                        "groups": "Family, Local"})
    assert _groups_of_first() == ["Family", "Local"]


def test_groups_dedup_case_insensitive(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add",
                data={"email": "a@example.com", "groups": "Family, family, FAMILY"})
    assert _groups_of_first() == ["Family"]


def test_edit_replaces_groups(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"email": "a@example.com", "groups": "family"})
    client.post(f"/contacts/{_cid()}/edit",
                data={"email": "a@example.com", "name": "", "groups": "work"})
    assert _groups_of_first() == ["work"]


def test_group_filter_chips_render(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"email": "a@example.com", "groups": "family"})
    client.post("/contacts/add", data={"email": "b@example.com", "groups": "work"})
    page = client.get("/contacts").text
    assert 'class="group-filter"' in page
    assert 'data-group="family"' in page
    assert 'data-group="work"' in page


def test_compose_picker_carries_groups(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"email": "a@example.com", "groups": "family"})
    page = client.get("/events/new").text
    assert 'data-groups="family"' in page


def test_csv_import_with_groups(client):
    client.post("/auth/dev-login")
    csv_data = (
        "name,email,groups\n"
        'Alex,alex@example.com,"family, local"\n'   # quoted multi-group cell
        "Sam,sam@example.com,work\n"
    )
    r = client.post("/contacts/import-csv",
                    files={"file": ("contacts.csv", csv_data, "text/csv")},
                    follow_redirects=True)
    assert r.status_code == 200
    all_groups = set()
    for (g,) in _db().execute("SELECT groups FROM contacts").fetchall():
        all_groups |= set(json.loads(g or "[]"))
    assert all_groups == {"family", "local", "work"}
    assert _db().execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 2


def test_csv_template_download(client):
    r = client.get("/contacts/template.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert r.text.splitlines()[0] == "name,email,groups"


def test_csv_export_includes_groups(client):
    client.post("/auth/dev-login")
    client.post("/contacts/add", data={"email": "a@example.com", "groups": "family, local"})
    csv_text = client.get("/contacts/export").text
    assert "groups" in csv_text.splitlines()[0]
    assert "family, local" in csv_text
