"""hub.db 엔진/세션 헬퍼 — WAL 모드 + busy timeout."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DB_PATH = Path("workspace/hub.db")


def _db_url(path: Path | None = None) -> str:
    target = path or Path(os.environ.get("HUB_DB_PATH", DEFAULT_DB_PATH))
    target.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{target}"


def get_engine(path: Path | None = None) -> Engine:
    """WAL 모드 + 30초 busy timeout 강제 (핸드오버 §10-2)."""
    engine = create_engine(_db_url(path), future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


_SessionFactory: sessionmaker[Session] | None = None


def _factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory


@contextmanager
def get_session() -> Iterator[Session]:
    session = _factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
