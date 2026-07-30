"""Database connection helpers and schema application."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from atlasql import config

log = logging.getLogger(__name__)


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Open a connection that commits on success and rolls back on error."""
    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row) as conn:
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
