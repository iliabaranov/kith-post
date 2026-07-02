"""Legal pages (privacy / terms) — public, and carry the Google Limited Use line."""


def test_privacy_page(client):
    r = client.get("/privacy")
    assert r.status_code == 200
    assert "Privacy Policy" in r.text
    assert "gmail.send" in r.text
    assert "Limited Use" in r.text                       # required Google disclosure
    assert "api-services-user-data-policy" in r.text     # linked policy
    assert "hello@example.com" in r.text                  # contact (KITH_CONTACT_EMAIL)


def test_terms_page(client):
    r = client.get("/terms")
    assert r.status_code == 200
    assert "Terms of Service" in r.text
    assert "California" in r.text                          # governing law
    assert 'AS IS' in r.text or '"AS IS"' in r.text


def test_footer_links_privacy_and_terms(client):
    # the public landing page must link the policy (OAuth verification requirement)
    page = client.get("/").text
    assert 'href="/privacy"' in page
    assert 'href="/terms"' in page
