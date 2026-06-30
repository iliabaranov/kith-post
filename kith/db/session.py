"""SQLite engine + session factory. WAL mode + foreign keys on.

`init_db` creates tables and runs a small additive schema sync so that adding a
new *nullable* column to a model doesn't require a manual migration (pre-launch
we only add columns). It is NOT a full migration tool — drops/renames/type
changes need real handling.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

log = logging.getLogger("kith")


class Base(DeclarativeBase):
    pass


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def ensure_schema(engine: Engine) -> None:
    """ADD COLUMN for any nullable model column missing from an existing table."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                if not col.nullable:
                    log.warning(
                        "schema: %s.%s is missing and NOT NULL — add it manually",
                        table.name, col.name,
                    )
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'))
                log.info("schema: added column %s.%s (%s)", table.name, col.name, ddl_type)


def init_db(engine: Engine) -> None:
    from kith.db import models  # noqa: F401 — register tables on Base.metadata

    Base.metadata.create_all(engine)
    ensure_schema(engine)
