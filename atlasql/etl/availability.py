"""Recompute `metric_availability`, the table level auto-detection runs on.

Coverage is the share of regions at a level that have a non-null value for a
metric. A row is written for every registered metric at every level that has
regions, including the zeroes: "gdp_per_capita has 0% coverage at city level"
is the fact that lets a city query mentioning GDP be rejected by name instead
of returning an empty table.

Every ETL job calls `refresh()` when it finishes. Stale coverage does not fail
loudly, it silently changes which level a query runs at, so it is never left to
a separate manual step.
"""

from __future__ import annotations

import logging

import psycopg

from atlasql import db

log = logging.getLogger(__name__)

_REFRESH_SQL = """
WITH level_totals AS (
    SELECT level, count(*)::double precision AS total
    FROM regions
    GROUP BY level
),
covered AS (
    SELECT r.level, m.metric_name, count(DISTINCT m.region_id)::double precision AS n
    FROM metrics m
    JOIN regions r ON r.id = m.region_id
    WHERE m.value IS NOT NULL
    GROUP BY r.level, m.metric_name
)
INSERT INTO metric_availability (metric_name, level, coverage_pct, computed_at)
SELECT d.metric_name,
       t.level,
       100.0 * COALESCE(c.n, 0) / t.total,
       now()
FROM metric_definitions d
CROSS JOIN level_totals t
LEFT JOIN covered c ON c.level = t.level AND c.metric_name = d.metric_name
ON CONFLICT (metric_name, level) DO UPDATE SET
    coverage_pct = EXCLUDED.coverage_pct,
    computed_at  = EXCLUDED.computed_at
"""

# Rows for metrics that have been unregistered, or for levels that no longer
# have any regions, would otherwise linger and keep answering queries.
_PRUNE_SQL = """
DELETE FROM metric_availability a
WHERE NOT EXISTS (
        SELECT 1 FROM metric_definitions d WHERE d.metric_name = a.metric_name
      )
   OR NOT EXISTS (SELECT 1 FROM regions r WHERE r.level = a.level)
"""


def refresh(conn: psycopg.Connection | None = None) -> None:
    """Recompute coverage for every registered metric at every populated level.

    Accepts an open connection so an ETL job can refresh inside its own
    transaction; opens one itself otherwise.
    """
    if conn is None:
        with db.connect() as owned:
            refresh(owned)
        return

    pruned = conn.execute(_PRUNE_SQL).rowcount
    conn.execute(_REFRESH_SQL)
    rows = conn.execute(
        """
        SELECT metric_name, level, round(coverage_pct::numeric, 1) AS pct
        FROM metric_availability
        WHERE coverage_pct > 0
        ORDER BY metric_name, coverage_pct DESC
        """
    ).fetchall()
    log.info(
        "metric_availability refreshed (%d stale rows pruned): %s",
        pruned,
        ", ".join(f"{r['metric_name']}@{r['level']}={r['pct']}%" for r in rows) or "no coverage yet",
    )
