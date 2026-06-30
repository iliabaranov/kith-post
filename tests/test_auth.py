"""Auth + account flows.

The dev-login fallback exercises the signed-in app without Google. The real
OAuth wiring is covered by (a) building the authorization URL and (b) the
callback with the network exchange mocked.
"""

import sqlite3

from fastapi.testclient import TestClient

from kith.config import Settings, get_settings

# ---------- dev-login path ----------

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
    body = client.get("/account/export").json()
    assert body["email"] == "dev@example.com"
    assert body["google_sub"] == "dev-user"


def test_email_is_encrypted_at_rest(client):
    client.post("/auth/dev-login")
    raw = sqlite3.connect(get_settings().db_path).execute(
        "SELECT email FROM users LIMIT 1"
    ).fetchone()[0]
    assert raw != "dev@example.com"
    assert raw.startswith("gAAAA")  # Fernet token, not plaintext


def test_dev_login_is_idempotent(client):
    client.post("/auth/dev-login")
    client.post("/auth/dev-login")
    n = sqlite3.connect(get_settings().db_path).execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]
    assert n == 1


def test_logout_returns_to_landing(client):
    client.post("/auth/dev-login")
    client.get("/auth/logout")
    assert "Sign in with Google" in client.get("/").text


def test_delete_removes_user_and_session(client):
    client.post("/auth/dev-login")
    assert "Sign in with Google" in client.post("/account/delete").text
    n = sqlite3.connect(get_settings().db_path).execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]
    assert n == 0


# ---------- logged-out access is redirected, never crashes ----------

def test_account_requires_login(client):
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_export_requires_login(client):
    r = client.get("/account/export", follow_redirects=False)
    assert r.status_code == 303


def test_delete_when_logged_out_is_safe(client):
    r = client.post("/account/delete", follow_redirects=False)
    assert r.status_code == 303  # no user, no crash


# ---------- callback (CSRF + the real login path, exchange mocked) ----------

def test_callback_without_code_redirects_to_error(client):
    r = client.get("/auth/callback", follow_redirects=False)
    assert r.status_code == 303
    assert "error=auth" in r.headers["location"]


def test_callback_creates_user_and_stores_refresh_token(client, monkeypatch):
    from kith.services import google_auth
    from kith.services.google_auth import GoogleIdentity

    monkeypatch.setattr(
        google_auth,
        "exchange_code",
        lambda s, code, state: GoogleIdentity(
            sub="g-123", email="alice@example.com", name="Alice", refresh_token="rt-xyz"
        ),
    )
    r = client.get("/auth/callback?code=fakecode", follow_redirects=True)
    assert r.status_code == 200
    assert "Hello" in r.text and "Alice" in r.text
    body = client.get("/account/export").json()
    assert body["email"] == "alice@example.com"
    assert body["google_sub"] == "g-123"
    # refresh token must be stored encrypted, never plaintext
    raw = sqlite3.connect(get_settings().db_path).execute(
        "SELECT refresh_token FROM users WHERE google_sub='g-123'"
    ).fetchone()[0]
    assert raw != "rt-xyz"
    assert raw.startswith("gAAAA")


# ---------- real OAuth wiring (no network) ----------

def test_authorization_url_has_scopes_and_callback():
    from kith.services import google_auth

    s = Settings(
        google_client_id="cid.apps.googleusercontent.com",
        google_client_secret="secret",
        base_url="https://party.example.ts.net",
    )
    url, state = google_auth.authorization_url(s)
    assert url.startswith("https://accounts.google.com/o/oauth2/")
    assert "gmail.send" in url
    assert "auth%2Fcallback" in url  # redirect_uri ends with /auth/callback
    assert state


def test_login_redirects_to_google_and_disables_dev_login_when_configured(monkeypatch):
    monkeypatch.setenv("KITH_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("KITH_GOOGLE_CLIENT_SECRET", "secret")
    get_settings.cache_clear()
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            r = c.get("/auth/login", follow_redirects=False)
            assert r.status_code in (302, 303, 307)
            assert "accounts.google.com" in r.headers["location"]
            # dev login is off once real OAuth is configured
            r2 = c.post("/auth/dev-login", follow_redirects=False)
            assert r2.headers["location"].endswith("/auth/login")
    finally:
        get_settings.cache_clear()


# ---------- full lifecycle, the way a user actually moves through it ----------

def test_e2e_signed_in_lifecycle(client):
    assert "Sign in with Google" in client.get("/").text          # logged out
    assert "Hello" in client.post("/auth/dev-login").text          # sign in
    assert "dev@example.com" in client.get("/account").text        # manage account
    assert client.get("/account/export").json()["email"] == "dev@example.com"
    assert "Sign in with Google" in client.post("/account/delete").text  # delete
    assert "Sign in with Google" in client.get("/account/export").text   # gone -> landing
