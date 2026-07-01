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
# Don't run the background reminder sweep loop during tests (scheduling itself
# stays enabled); tests drive sweep_tick directly with an injected clock.
os.environ.setdefault("KITH_REMINDERS__SWEEP_SECONDS", "0")
# Hermetic tests: never inherit a developer's real .env Google credentials.
# os.environ takes precedence over the .env file, so "" forces the unconfigured
# (dev-login) baseline; the configured-OAuth test monkeypatches these back.
os.environ["KITH_GOOGLE_CLIENT_ID"] = ""
os.environ["KITH_GOOGLE_CLIENT_SECRET"] = ""

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
