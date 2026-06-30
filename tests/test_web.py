from fastapi.testclient import TestClient

from kith.web.app import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_index_renders():
    r = client.get("/")
    assert r.status_code == 200
    assert "Kith Post" in r.text
    assert "Sign in with Google" in r.text


def test_login_stub():
    r = client.get("/auth/login")
    assert r.status_code == 200
    assert "next build" in r.text
