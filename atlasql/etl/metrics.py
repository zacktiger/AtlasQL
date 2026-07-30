"""Shared metric upsert used by every metric-producing ETL job."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TypedDict

import psycopg

log = logging.getLogger(__name__)


class MetricRow(TypedDict):
    region_id: int
    metric_name: str
    value: float | None
    year: int


_UPSERT_SQL = """
INSERT INTO metrics (region_id, metric_name, value, year)
VALUES (%(region_id)s, %(metric_name)s, %(value)s, %(year)s)
ON CONFLICT (region_id, metric_name, year) DO UPDATE SET value = EXCLUDED.value
"""


def upsert_metrics(conn: psycopg.Connection, rows: Sequence[MetricRow]) -> int:
    """Upsert metric values on (region_id, metric_name, year). Idempotent."""
    if not rows:
        log.warning("no metric rows to write")
        return 0
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, rows)
    log.info("upserted %d metric rows (%s)", len(rows), rows[0]["metric_name"])
    return len(rows)
