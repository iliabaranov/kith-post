"""The reconnect-Google banner shows when a user's token needs re-consent."""

import sqlite3

from kith.config import get_settings


def test_reconnect_banner_on_dashboard(client):
    client.post("/auth/dev-login")
    db = sqlite3.connect(get_settings().db_path)
    db.execute("UPDATE users SET reconnect_needed = 1")
    db.commit()
    db.close()
    page = client.get("/").text
    assert "Reconnect Google" in page


def test_no_banner_when_ok(client):
    client.post("/auth/dev-login")
    assert "Reconnect Google" not in client.get("/").text
