"""Postgres access: a connection pool plus a tiny, ordered migration runner."""
from __future__ import annotations

import logging
import pathlib
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)
MIGRATIONS = pathlib.Path(__file__).parent / "migrations"
_MIGRATION_LOCK = 774_001


class Database:
    def __init__(self, url: str, min_size: int = 2, max_size: int = 30, lock_pool_size: int = 24) -> None:
        self.url = url
        # Fail fast (-> 503, vendor retries) instead of queueing forever when the pool is exhausted.
        self.pool = ConnectionPool(
            url, min_size=min_size, max_size=max_size, open=False, timeout=5.0,
            kwargs={"row_factory": dict_row, "autocommit": False},
        )
        # Session-level advisory locks are held for a whole agent turn. They live in their own pool so
        # holding a lock can never starve the connections the turn itself needs (deadlock found under load).
        self.lock_pool = ConnectionPool(
            url, min_size=1, max_size=lock_pool_size, open=False, timeout=5.0,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )

    def open(self, wait_seconds: float = 60.0) -> None:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                with psycopg.connect(self.url, connect_timeout=3):
                    break
            except psycopg.OperationalError as exc:
                if time.monotonic() > deadline:
                    raise
                log.info("waiting for postgres: %s", exc)
                time.sleep(1)
        self.pool.open(wait=True)
        self.lock_pool.open(wait=True)

    def close(self) -> None:
        self.lock_pool.close()
        self.pool.close()

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        """A transaction: commits on success, rolls back on any exception."""
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    @contextmanager
    def lock_conn(self) -> Iterator[psycopg.Connection]:
        """An autocommit connection from the lock pool, for session advisory locks."""
        with self.lock_pool.connection() as conn:
            yield conn

    def migrate(self) -> None:
        with self.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name in done:
                    continue
                log.info("applying migration %s", path.name)
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
