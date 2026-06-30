"""Test fixtures: a throwaway data dir + fixed Fernet key (so encrypted columns
round-trip), per-test DB isolation, and a lifespan-managed TestClient."""

import os
import tempfile

from cryptography.fernet import Fernet

os.environ.setdefault("KITH_DATA_DIR", tempfile.mkdtemp(prefix="kith-test-"))
os.environ.setdefault("KITH_FERNET_KEY", Fernet.generate_key().decode())

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
