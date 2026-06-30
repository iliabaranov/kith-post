"""Test fixtures. Use a throwaway data dir + a fixed Fernet key so encrypted
columns round-trip, and a lifespan-managed TestClient so the DB is wired up."""

import os
import tempfile

from cryptography.fernet import Fernet

os.environ.setdefault("KITH_DATA_DIR", tempfile.mkdtemp(prefix="kith-test-"))
os.environ.setdefault("KITH_FERNET_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client():
    from kith.web.app import app  # imported after env is set

    with TestClient(app) as c:  # context manager runs lifespan (creates tables)
        yield c
