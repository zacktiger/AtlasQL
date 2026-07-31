"""Shared region upsert used by every boundary import.

Boundary sources differ (Natural Earth, geoBoundaries, GeoNames) but they all
land in the same table through this one function, keyed on (source, source_id).
That is what keeps ids stable across re-imports so metrics never orphan.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, TypedDict

import psycopg

log = logging.getLogger(__name__)


class RegionRow(TypedDict, total=False):
    name: str
    level: str
    parent_id: int | None
    source: str
    source_id: str
    wkb: bytes | None  # polygon geometry; None for point-only tiers
    point_wkb: bytes | None  # point tiers (cities) supply their location here


# ST_MakeValid can hand back a GeometryCollection when it repairs a self
# intersection; CollectionExtract(..., 3) keeps only the polygonal parts so the
# GEOMETRY(MultiPolygon) column constraint always holds.
_GEOM_SQL = """
    CASE WHEN %(wkb)s::bytea IS NULL THEN NULL ELSE
        ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_GeomFromWKB(%(wkb)s::bytea, 4326)), 3))
    END
"""

# Polygon tiers derive their centroid from the polygon. Point tiers have no
# polygon to derive it from, so they supply the point directly.
_CENTROID_SQL = f"""
    CASE
        WHEN %(point_wkb)s::bytea IS NOT NULL THEN ST_GeomFromWKB(%(point_wkb)s::bytea, 4326)
        WHEN %(wkb)s::bytea IS NULL THEN NULL
        ELSE ST_Centroid({_GEOM_SQL})
    END
"""

_UPSERT_SQL = f"""
INSERT INTO regions (name, level, parent_id, source, source_id, geom, centroid)
VALUES (
    %(name)s, %(level)s, %(parent_id)s, %(source)s, %(source_id)s,
    {_GEOM_SQL},
    {_CENTROID_SQL}
)
ON CONFLICT (source, source_id) DO UPDATE SET
    name      = EXCLUDED.name,
    level     = EXCLUDED.level,
    parent_id = EXCLUDED.parent_id,
    geom      = EXCLUDED.geom,
    centroid  = EXCLUDED.centroid
RETURNING id
"""


def upsert_regions(conn: psycopg.Connection, rows: Sequence[RegionRow]) -> dict[str, int]:
    """Upsert regions, returning {source_id: region_id} for parent wiring."""
    ids: dict[str, int] = {}
    with conn.cursor() as cur:
        for row in rows:
            # Both geometry slots are always bound; a tier supplies one of them.
            params: dict[str, Any] = {"wkb": None, "point_wkb": None, **row}
            cur.execute(_UPSERT_SQL, params)
            result = cur.fetchone()
            assert result is not None  # RETURNING on an upsert always yields a row
            ids[row["source_id"]] = result["id"]
    log.info("upserted %d regions", len(ids))
    return ids


def existing_ids(conn: psycopg.Connection, source: str) -> dict[str, int]:
    """Map source_id -> region id for one source, for parent lookups."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_id, id FROM regions WHERE source = %s", (source,)
        )
        return {row["source_id"]: row["id"] for row in cur.fetchall()}
