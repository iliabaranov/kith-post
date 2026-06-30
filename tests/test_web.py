def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_index_renders_logged_out(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Kith Post" in r.text
    assert "Sign in with Google" in r.text


def test_login_offers_dev_signin_when_google_unconfigured(client):
    r = client.get("/auth/login")
    assert r.status_code == 200
    assert "dev" in r.text.lower()
