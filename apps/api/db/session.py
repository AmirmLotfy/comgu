"""Database session handling.

SQLite by default so a judge can clone and run with nothing installed;
`DATABASE_URL` switches it to Postgres for anything shared.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from apps.api.db.models import Base

DEFAULT_SQLITE = f"sqlite:///{Path.cwd() / 'comgu.db'}"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_SQLITE)


def make_engine(url: str | None = None):
    url = url or database_url()
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        # WAL keeps the API readable while the worker writes; foreign keys are
        # off by default in SQLite and we rely on them.
        @event.listens_for(engine, "connect")
        def _pragmas(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


_engine = None
_Session: sessionmaker | None = None


def engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def SessionLocal() -> sessionmaker:
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _Session


def init_db() -> None:
    """Create any missing tables.

    `create_all` is additive and never drops or alters, so it is safe to run
    alongside Alembic — it fills in tables a migration has not yet been written
    for, and does nothing when the schema is current. Alembic remains the
    source of truth for changes to existing tables, which `create_all` cannot
    perform.

    A database created this way has no version stamped, so `alembic upgrade
    head` would try to recreate its tables. `stamp_if_unversioned` records the
    current head instead; see infra/README.md.
    """
    Base.metadata.create_all(engine())


def schema_drift() -> list[str]:
    """Differences between the models and the live database.

    `create_all` adds missing *tables* but never adds a column to an existing
    one, so a schema built that way silently lags the models as soon as a column
    is added. Stamping such a database at head then tells Alembic there is
    nothing to do, and the mismatch only surfaces at query time as
    `no such column`.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    with engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    return [str(d) for d in diff]


def stamp_if_unversioned(alembic_ini: str = "alembic.ini") -> str | None:
    """Mark an already-created schema as being at the latest revision.

    Refuses when the schema does not actually match the models — stamping a
    lagging database is worse than leaving it unversioned, because it hides the
    problem from Alembic too.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text

    eng = engine()
    if inspect(eng).has_table("alembic_version"):
        with eng.connect() as conn:
            if list(conn.execute(text("SELECT version_num FROM alembic_version"))):
                return None

    drift = schema_drift()
    if drift:
        raise RuntimeError(
            "refusing to stamp: the database does not match the models.\n  "
            + "\n  ".join(drift[:6])
            + "\nRebuild it with `alembic upgrade head` against an empty database."
        )

    cfg = Config(alembic_ini)
    cfg.set_main_option("sqlalchemy.url", database_url())
    command.stamp(cfg, "head")
    return "head"


def get_session() -> Iterator[Session]:
    s = SessionLocal()()
    try:
        yield s
    finally:
        s.close()


def session() -> Session:
    return SessionLocal()()
