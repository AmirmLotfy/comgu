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
    Base.metadata.create_all(engine())


def get_session() -> Iterator[Session]:
    s = SessionLocal()()
    try:
        yield s
    finally:
        s.close()


def session() -> Session:
    return SessionLocal()()
