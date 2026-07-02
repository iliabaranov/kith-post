"""Security hardening: response headers + POST-only logout."""


def test_security_headers_present(client):
    h = client.get("/").headers
    assert h.get("x-frame-options") == "DENY"
    assert h.get("x-content-type-options") == "nosniff"
    assert "strict-origin-when-cross-origin" in h.get("referrer-policy", "")
    csp = h.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp


def test_static_versioned_is_immutable(client):
    # a ?v=<mtime> cache-buster → safe to cache forever
    cc = client.get("/static/css/kith.css?v=123").headers.get("cache-control", "")
    assert "max-age=31536000" in cc
    assert "immutable" in cc


def test_static_unversioned_is_short_lived(client):
    # no cache-buster (e.g. favicon) → modest TTL, revalidates
    cc = client.get("/static/favicon.svg").headers.get("cache-control", "")
    assert "max-age=3600" in cc
    assert "immutable" not in cc


def test_logout_is_post_only(client):
    client.post("/auth/dev-login")
    assert client.get("/auth/logout", follow_redirects=False).status_code == 405


def test_logout_post_clears_session(client):
    client.post("/auth/dev-login")
    assert "Create a card" in client.get("/").text          # logged in
    client.post("/auth/logout")
    assert "Sign in with Google" in client.get("/").text    # logged out
