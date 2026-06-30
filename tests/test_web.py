def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_index_renders_logged_out(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Kith Post" in r.text
    assert "Sign in with Google" in r.text
    assert "no ads" in r.text


def test_landing_has_privacy_faq(client):
    t = client.get("/").text
    assert "What you're signing up for" in t
    assert "send only" in t            # clarifies we can't read mail
    assert "permanent delete" in t     # data control is stated


def test_login_offers_dev_signin_when_google_unconfigured(client):
    r = client.get("/auth/login")
    assert r.status_code == 200
    assert "dev" in r.text.lower()


def test_css_is_served(client):
    r = client.get("/static/css/kith.css")
    assert r.status_code == 200
    assert "--paper" in r.text  # the design tokens are present


def test_favicon_is_served(client):
    r = client.get("/static/favicon.svg")
    assert r.status_code == 200
    assert "svg" in r.headers["content-type"]
