"""Additive schema sync — the safety net that fixes 'no such column' after a
model gains a new nullable field (the event_end_time/signoff bug)."""

from sqlalchemy import create_engine, inspect, text

import kith.db.models  # noqa: F401 — register tables on Base.metadata
from kith.db.session import ensure_schema


def test_ensure_schema_adds_missing_nullable_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
    # Simulate a pre-existing events table from an earlier schema (the NOT NULL
    # columns are present; the new nullable ones are not).
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE events (id VARCHAR PRIMARY KEY, user_id VARCHAR, title VARCHAR, "
            "message TEXT, blocks JSON, status VARCHAR, created_at DATETIME)"
        ))

    ensure_schema(engine)

    cols = {col["name"] for col in inspect(engine).get_columns("events")}
    assert "event_end_time" in cols  # the column whose absence crashed the app
    assert "signoff" in cols
    assert "event_date" in cols      # other nullable columns are backfilled too
    assert "title" in cols           # existing columns are preserved
