"""Database engine, session factory and schema bootstrap (PostgreSQL in production, SQLite for tests)."""
from __future__ import annotations

import os
import time
from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return eng
    return create_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def init_db(retries: int = 30, delay: float = 1.0) -> None:
    """Create tables and indexes. Safe to call from several processes at once (advisory lock)."""
    from . import models  # noqa: F401  (register tables)

    last: Exception | None = None
    for _ in range(retries):
        try:
            with engine.begin() as conn:
                if is_postgres():
                    conn.execute(text("SELECT pg_advisory_xact_lock(727001)"))
                    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                Base.metadata.create_all(conn)
                if is_postgres():
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_pages_fts ON document_pages "
                        "USING GIN (to_tsvector('simple'::regconfig, text))"))
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding ON chunks "
                        "USING hnsw (embedding vector_cosine_ops)"))
            return
        except OperationalError as exc:  # database not ready yet
            last = exc
            time.sleep(delay)
    raise RuntimeError(f"Database not reachable: {last}")
