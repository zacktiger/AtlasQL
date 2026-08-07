"""Database connection helpers and schema application.

Connections come from a pool. Opening one is not free — a TCP connect, a TLS
handshake and password authentication measured 21 ms against a database on
localhost, and a hosted Postgres across a network is several times that. Every
endpoint here does a few milliseconds of query work, so an unpooled connection
per request spends most of its time on the handshake rather than the answer.
The pool keeps that cost to the first request per worker.

Connections are checked before they are handed out. That costs one round trip,
which is a real fraction of the saving on a local database and a small one on a
hosted database — where it earns its keep, because managed Postgres and the
poolers in front of it drop idle connections silently. Without the check that
surfaces as an intermittent 500 on the first request after a quiet spell, which
is exactly the kind of failure that is miserable to diagnose in production.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from atlasql import config

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def pool() -> ConnectionPool:
    """The process-wide connection pool, opened on first use.

    Lazily rather than at import time so that importing `atlasql.api` — which
    the tests do — never depends on a reachable database.
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                config.DATABASE_URL,
                min_size=config.POOL_MIN_SIZE,
                max_size=config.POOL_MAX_SIZE,
                timeout=config.POOL_TIMEOUT_S,
                max_idle=config.POOL_MAX_IDLE_S,
                kwargs={"row_factory": dict_row},
                check=ConnectionPool.check_connection,
                name="atlasql",
                open=True,
            )
            atexit.register(close_pool)
    return _pool


def close_pool() -> None:
    """Close the pool. Idempotent; safe to call when it was never opened."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """A pooled connection that commits on success and rolls back on error.

    Same contract as the `psycopg.connect()` call this replaced, so every ETL
    job and endpoint using it is unchanged.
    """
    with pool().connection() as conn:
        yield conn


def apply_schema() -> None:
    """Apply every migration in sql/ in filename order.

    The migrations are written to be re-runnable (CREATE ... IF NOT EXISTS), so
    this doubles as the setup step and the idempotent no-op on an existing
    database.
    """
    files = sorted(config.SQL_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError(f"no migrations found in {config.SQL_DIR}")
    with connect() as conn:
        for path in files:
            log.info("applying %s", path.name)
            conn.execute(path.read_text(encoding="utf-8"))
    log.info("schema applied: %s", ", ".join(f.name for f in files))


def register_metric(
    conn: psycopg.Connection,
    metric_name: str,
    label: str,
    unit: str | None,
    description: str | None,
    source: str | None,
) -> None:
    """Upsert a metric into the live registry.

    Every ETL job calls this before writing values: metrics.metric_name has a
    foreign key onto the registry, so an unregistered metric cannot be stored
    and /metadata can never fall out of sync with what is queryable.
    """
    conn.execute(
        """
        INSERT INTO metric_definitions (metric_name, label, unit, description, source)
        VALUES (%(metric_name)s, %(label)s, %(unit)s, %(description)s, %(source)s)
        ON CONFLICT (metric_name) DO UPDATE SET
            label       = EXCLUDED.label,
            unit        = EXCLUDED.unit,
            description = EXCLUDED.description,
            source      = EXCLUDED.source
        """,
        {
            "metric_name": metric_name,
            "label": label,
            "unit": unit,
            "description": description,
            "source": source,
        },
    )
