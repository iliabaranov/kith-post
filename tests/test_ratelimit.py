"""Per-client rate limiting on the public/auth surface.

The suite runs with limiting disabled (see conftest) so counters don't bleed
between tests; here we flip the limiter on, hammer an endpoint past its ceiling,
and assert the surplus request is rejected with 429.
"""

import pytest


@pytest.fixture
def limited():
    from kith.web.ratelimit import limiter

    limiter.enabled = True
    try:
        yield limiter
    finally:
        limiter.enabled = False
        limiter.reset()  # clear counters so later tests start clean


def test_rsvp_is_rate_limited(client, limited):
    # 20/minute on the RSVP endpoint; the 21st from one client should 429.
    codes = [client.post("/i/nonexistent/rsvp", data={}).status_code for _ in range(25)]
    assert 429 in codes
    # the first handful go through to the normal (404/redirect) handling
    assert codes[0] != 429


def test_limit_disabled_lets_everything_through(client):
    # With limiting off (the suite default) the same burst never trips.
    codes = [client.post("/i/nonexistent/rsvp", data={}).status_code for _ in range(25)]
    assert 429 not in codes
