"""Database connection helpers for CedarDB (Postgres-wire-compatible).

CedarDB speaks the standard Postgres wire protocol, so any
psycopg/SQLAlchemy client works against it unchanged. We default to
the values in `docker-compose.yml`; any of them can be overridden
via the standard `PG*` environment variables or `WINETONE_DB_URL`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text

DEFAULT_DB_URL = (
    "postgresql+psycopg://winetone:winetone@localhost:5432/winetone"
)


def db_url() -> str:
    """Resolve the database URL.

    Order of precedence:
      1. WINETONE_DB_URL (full SQLAlchemy URL)
      2. DATABASE_URL (standard convention used by hosting platforms)
      3. PG* env vars (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE)
      4. The docker-compose default (winetone/winetone @ localhost:5432).

    Bare `postgresql://` URLs (the form managed providers like Neon /
    Supabase / Render hand you) are normalized to `postgresql+psycopg://`
    so SQLAlchemy picks the psycopg3 driver.
    """
    url = os.environ.get("WINETONE_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pg_host = os.environ.get("PGHOST")
        if pg_host:
            pg_port = os.environ.get("PGPORT", "5432")
            pg_user = os.environ.get("PGUSER", "winetone")
            pg_pass = os.environ.get("PGPASSWORD", "winetone")
            pg_db = os.environ.get("PGDATABASE", "winetone")
            url = (
                f"postgresql+psycopg://{pg_user}:{pg_pass}"
                f"@{pg_host}:{pg_port}/{pg_db}"
            )
        else:
            url = DEFAULT_DB_URL
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    elif url.startswith("postgres://"):  # heroku-style
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def engine() -> Engine:
    """Return a SQLAlchemy engine for CedarDB.

    Memoized per process — `create_engine` is cheap but pooled
    connections are not.
    """
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = create_engine(db_url(), pool_pre_ping=True)
    return _engine_singleton


_engine_singleton: Engine | None = None


@contextmanager
def connect() -> Iterator:
    """Yield a transactional connection."""
    with engine().begin() as conn:
        yield conn


def ping() -> bool:
    """Return True iff CedarDB responds to `SELECT 1`."""
    try:
        with engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — surface the broad exception to caller
        return False


def init_schema() -> None:
    """Drop and recreate the canonical schema.

    Called by `winetone build canonical`. We do an unconditional
    drop/create rather than IF NOT EXISTS so the PoC build is
    reproducible from cold state — no partial-state confusion.
    """
    with connect() as conn:
        for stmt in (
            "DROP TABLE IF EXISTS wine_features CASCADE",
            "DROP TABLE IF EXISTS source_records CASCADE",
            "DROP TABLE IF EXISTS wines CASCADE",
            "DROP TABLE IF EXISTS wine_embeddings CASCADE",
        ):
            conn.execute(text(stmt))
