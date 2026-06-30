"""Auth + account flows, exercised via the dev-login fallback (no Google needed)."""

import sqlite3

from kith.config import get_settings


def test_dev_login_then_signed_in_home(client):
    r = client.post("/auth/dev-login")  # 303 -> follows to /
    assert r.status_code == 200
    assert "Hello" in r.text
    assert "Manage account" in r.text


def test_account_page_shows_decrypted_email(client):
    client.post("/auth/dev-login")
    r = client.get("/account")
    assert r.status_code == 200
    assert "dev@example.com" in r.text


def test_export_returns_json(client):
    client.post("/auth/dev-login")
    r = client.get("/account/export")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "dev@example.com"
    assert body["google_sub"] == "dev-user"


def test_email_is_encrypted_at_rest(client):
    client.post("/auth/dev-login")
    db_path = get_settings().db_path
    raw = sqlite3.connect(db_path).execute("SELECT email FROM users LIMIT 1").fetchone()[0]
    assert raw != "dev@example.com"
    assert raw.startswith("gAAAA")  # stored as a Fernet token, not plaintext


def test_logout_returns_to_landing(client):
    client.post("/auth/dev-login")
    client.get("/auth/logout")
    r = client.get("/")
    assert "Sign in with Google" in r.text


def test_delete_account_removes_user_and_session(client):
    client.post("/auth/dev-login")
    r = client.post("/account/delete")  # 303 -> /
    assert "Sign in with Google" in r.text
    # protected pages now redirect to the landing page
    r = client.get("/account/export")
    assert "Sign in with Google" in r.text
