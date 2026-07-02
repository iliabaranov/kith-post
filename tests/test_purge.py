"""G6: auto-purge of heavy full-res card images past their retention window."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from kith.config import Settings
from kith.db.models import Asset, Event, User
from kith.db.session import init_db, make_engine, make_session_factory
from kith.services import scheduler

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _session(tmp_path):
    engine = make_engine(tmp_path / "s.sqlite3")
    init_db(engine)
    return make_session_factory(engine)()


def _asset(db, tmp_path, name):
    full = tmp_path / f"{name}-full.jpg"
    inline = tmp_path / f"{name}-inline.jpg"
    full.write_bytes(b"BIGDATA")
    inline.write_bytes(b"small")
    u = db.execute(select(User)).scalars().first()
    if u is None:
        u = User(google_sub="g", email="h@x", display_name="H")
        db.add(u)
        db.commit()
        db.refresh(u)
    a = Asset(user_id=u.id, sha256="x", mime="image/jpeg", full_path=str(full),
              inline_path=str(inline), width=10, height=10, bytes=7)
    db.add(a)
    db.commit()
    db.refresh(a)
    return a, full, inline, u


def _settings(tmp_path, days=30):
    return Settings(data_dir=tmp_path / "d", asset_retention_days=days)


def test_purges_full_after_event_plus_window(tmp_path):
    db = _session(tmp_path)
    a, full, inline, u = _asset(db, tmp_path, "old")
    ev = Event(user_id=u.id, title="Past", event_date=date(2026, 4, 1), asset_id=a.id)  # 60d prior
    db.add(ev)
    db.commit()

    assert scheduler.purge_expired_assets(db, _settings(tmp_path), now=NOW) == 1
    assert not full.exists()      # heavy file gone
    assert inline.exists()        # small copy kept
    db.expire_all()
    assert db.get(Asset, a.id).purged_at is not None


def test_keeps_recent_event(tmp_path):
    db = _session(tmp_path)
    a, full, inline, u = _asset(db, tmp_path, "recent")
    ev = Event(user_id=u.id, title="Soon", event_date=date(2026, 5, 25), asset_id=a.id)  # 7d prior
    db.add(ev)
    db.commit()

    assert scheduler.purge_expired_assets(db, _settings(tmp_path), now=NOW) == 0
    assert full.exists()


def test_orphaned_asset_purged_by_age(tmp_path):
    db = _session(tmp_path)
    a, full, inline, u = _asset(db, tmp_path, "orphan")
    # no event references it; created_at defaults to now() (recent) so it should NOT purge yet
    assert scheduler.purge_expired_assets(db, _settings(tmp_path), now=NOW + timedelta(days=1)) == 0
    assert full.exists()


def test_retention_zero_disables(tmp_path):
    db = _session(tmp_path)
    a, full, inline, u = _asset(db, tmp_path, "keep")
    ev = Event(user_id=u.id, title="Past", event_date=date(2020, 1, 1), asset_id=a.id)
    db.add(ev)
    db.commit()
    assert scheduler.purge_expired_assets(db, _settings(tmp_path, days=0), now=NOW) == 0
    assert full.exists()


def test_idempotent(tmp_path):
    db = _session(tmp_path)
    a, full, inline, u = _asset(db, tmp_path, "old")
    ev = Event(user_id=u.id, title="Past", event_date=date(2026, 4, 1), asset_id=a.id)
    db.add(ev)
    db.commit()
    assert scheduler.purge_expired_assets(db, _settings(tmp_path), now=NOW) == 1
    assert scheduler.purge_expired_assets(db, _settings(tmp_path), now=NOW) == 0  # already purged
