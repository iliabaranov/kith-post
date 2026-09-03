"""Test fixtures: a throwaway data dir + fixed Fernet key (so encrypted columns
round-trip), per-test DB isolation, and a lifespan-managed TestClient."""

import os
import tempfile

from cryptography.fernet import Fernet

os.environ.setdefault("KITH_DATA_DIR", tempfile.mkdtemp(prefix="kith-test-"))
os.environ.setdefault("KITH_FERNET_KEY", Fernet.generate_key().decode())
# Stay hermetic if a real .env is present (e.g. running on the deploy box): a
# production https base_url would make session cookies Secure-only, which the
# http TestClient drops (breaking every logged-in test); live send-mode would
# try real Gmail. Pin safe test defaults unless the caller overrides them.
os.environ.setdefault("KITH_BASE_URL", "http://localhost:8000")
os.environ.setdefault("KITH_SEND_MODE", "dry-run")
os.environ.setdefault("KITH_CONTACT_EMAIL", "hello@example.com")
# Don't run the background reminder sweep loop during tests (scheduling itself
# stays enabled); tests drive sweep_tick directly with an injected clock.
os.environ.setdefault("KITH_REMINDERS__SWEEP_SECONDS", "0")
# Hermetic tests: never inherit a developer's real .env Google credentials.
# os.environ takes precedence over the .env file, so "" forces the unconfigured
# (dev-login) baseline; the configured-OAuth test monkeypatches these back.
os.environ["KITH_GOOGLE_CLIENT_ID"] = ""
os.environ["KITH_GOOGLE_CLIENT_SECRET"] = ""
# Same for the WhatsApp channel: on the deploy box .env has it switched on, and
# tests that assert the default-off behaviour would read that as the default.
# Forced off here; the tests that need it monkeypatch it back on.
os.environ["KITH_WHATSAPP_ENABLED"] = "false"
os.environ["KITH_WAHA_API_KEY"] = ""
# ...and the receipt secret, which the deploy box's .env now has: without this a
# test asserting receipts are off by default reads the box's real configuration.
os.environ["KITH_WAHA_WEBHOOK_SECRET"] = ""
# Same treatment for the SMS channel, for the same reason: it is instance-level,
# so the moment the deploy box configures a provider its .env would become the
# default every test sees. Forced off here; the tests that need it set it back.
os.environ["KITH_SMS_ENABLED"] = "false"
os.environ["KITH_SMS_PROVIDER"] = "none"
os.environ["KITH_SMS_WEBHOOK_SECRET"] = ""
# Never pause between sends in tests: the pacing is proved by its own unit test,
# and a real gap would add minutes to the suite.
os.environ.setdefault("KITH_SMS_SEND_GAP_MIN_SECONDS", "0")
os.environ.setdefault("KITH_SMS_SEND_GAP_MAX_SECONDS", "0")
# Rate limiting is per-client and in-memory, so its counters would bleed across
# the in-process suite and make unrelated tests flaky. Off by default here; the
# dedicated test flips the limiter back on to prove it works.
os.environ.setdefault("KITH_RATE_LIMIT_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    """Start each test with an empty DB; the app's lifespan recreates the tables."""
    from kith.config import get_settings

    base = get_settings().db_path
    for f in (base, base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm")):
        if f.exists():
            f.unlink()
    yield


@pytest.fixture
def client():
    from kith.web.app import app  # imported after env is set

    with TestClient(app) as c:  # context manager runs lifespan (creates tables)
        yield c
