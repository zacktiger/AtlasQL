"""Region geometry as GeoJSON, for drawing results on a map.

This is deliberately a separate endpoint from /query rather than extra fields on
QueryResult. Geometry is large — the country tier alone is 11 MB of raw GeoJSON
— and it varies with how far the user has zoomed in, while a result row does
not. Keeping it out of QueryResult means `GeoFilter` in, `QueryResult` out stays
the one contract the query engine speaks, and the map is just another consumer
of ids the engine already returned.

Two things here are less obvious than they look:

  * Simplification is mandatory, not an optimization. At full resolution a
    ten-country result is megabytes over the wire to draw shapes a few hundred
    pixels wide. The client asks for the detail its current zoom can show.
  * Ring winding order is what makes the globe work, and the convention is the
    opposite of the one you would guess. On a plane a ring has an inside; on a
    sphere it merely separates two regions, and the winding order is the only
    thing that says which of them is the country. d3-geo — like TopoJSON, and
    unlike RFC 7946 — takes the *clockwise* side of an exterior ring as the
    interior for any polygon smaller than a hemisphere. Force the RFC's
    counterclockwise instead and every country renders as "the whole planet
    except this country", which looks like a projection bug and is not one.
    No administrative region is larger than a hemisphere, so the rule needs no
    special case here.
"""

from __future__ import annotations

import hashlib
import json

import psycopg

# Cities have no polygon, only a centroid, so a map of city results is points.
# Falling back to the centroid keeps that a property of the feature the client
# can read rather than a silently missing shape.
_SELECT = """
    SELECT
      r.id,
      r.name,
      r.level,
      p.name AS parent_name,
      ST_AsGeoJSON(
        ST_ForcePolygonCW(ST_SimplifyPreserveTopology(r.geom, %(tolerance)s)),
        %(digits)s
      ) AS polygon,
      ST_AsGeoJSON(r.centroid, 5) AS point
    FROM regions r
    LEFT JOIN regions p ON p.id = r.parent_id
    WHERE {predicate}
    ORDER BY r.id
"""

# Beyond this a request is asking for a bulk export, not a map. top_n caps a
# query at 100 results, so this is never hit by the UI.
MAX_IDS = 100

# Degrees. 0 disables simplification; the ceiling is roughly "a continent is a
# blob", past which nothing recognisable survives.
MIN_TOLERANCE = 0.0
MAX_TOLERANCE = 2.0


def clamp_tolerance(tolerance: float) -> float:
    return max(MIN_TOLERANCE, min(MAX_TOLERANCE, tolerance))


def _digits_for(tolerance: float) -> int:
    """Coordinate precision matched to the simplification.

    Writing seven decimals (centimetre precision) for vertices that have just
    been moved several kilometres by the simplifier is pure payload. Five
    decimals is about a metre, three about 100 m.
    """
    if tolerance >= 0.1:
        return 3
    if tolerance >= 0.01:
        return 4
    return 5


def _feature(row: dict) -> dict:
    """One GeoJSON feature, polygon if the tier has one and point otherwise."""
    geometry = row["polygon"] or row["point"]
    return {
        "type": "Feature",
        "id": row["id"],
        "geometry": json.loads(geometry) if geometry else None,
        "properties": {
            "region_id": row["id"],
            "name": row["name"],
            "level": row["level"],
            "parent_name": row["parent_name"],
            # The client draws points and polygons differently, and should not
            # have to infer which it got by inspecting the geometry type.
            "kind": "polygon" if row["polygon"] else "point",
        },
    }


def _collection(conn: psycopg.Connection, predicate: str, params: dict) -> dict:
    rows = conn.execute(_SELECT.format(predicate=predicate), params).fetchall()
    return {
        "type": "FeatureCollection",
        "features": [_feature(row) for row in rows],
    }


def for_regions(
    conn: psycopg.Connection, region_ids: list[int], tolerance: float
) -> dict:
    """Geometry for specific regions, ordered by region id rather than rank.

    The caller already has the ranked ordering from /query; sorting by rank
    here would only invite the client to depend on it. The id order is just
    what a deterministic query happens to produce, needed so /geometry/basemap
    gets a stable ETag -- not a second ordering contract to rely on.
    """
    if not region_ids:
        return {"type": "FeatureCollection", "features": []}
    tolerance = clamp_tolerance(tolerance)
    return _collection(
        conn,
        "r.id = ANY(%(ids)s)",
        {
            "ids": region_ids,
            "tolerance": tolerance,
            "digits": _digits_for(tolerance),
        },
    )


def basemap_digest(conn: psycopg.Connection, level: str, tolerance: float) -> str:
    """A stable identifier for what /geometry/basemap currently returns.

    Hashes the raw, unsimplified geometry rather than the simplified GeoJSON
    the endpoint sends: ST_SimplifyPreserveTopology's output is not byte-stable
    across repeated calls on identical input (confirmed directly -- the same
    row's simplified polygon comes back with a different vertex count run to
    run, on a single connection with parallel workers disabled, so it is GEOS's
    algorithm and not query planning). Hashing that output would mint a new
    ETag on almost every request even though nothing changed, defeating the
    revalidation this endpoint exists to provide. Raw geometry only changes on
    an ETL reimport, which is exactly the staleness this ETag needs to track.
    """
    tolerance = clamp_tolerance(tolerance)
    digits = _digits_for(tolerance)
    row = conn.execute(
        """
        SELECT md5(string_agg(md5(ST_AsEWKB(r.geom)), '' ORDER BY r.id)) AS digest
        FROM regions r
        WHERE r.level = %(level)s AND r.geom IS NOT NULL
        """,
        {"level": level},
    ).fetchone()
    content = row["digest"] or "empty"
    return hashlib.sha256(f"{content}:{tolerance}:{digits}".encode()).hexdigest()[:32]


def basemap(conn: psycopg.Connection, level: str, tolerance: float) -> dict:
    """Every region at one level, coarse: the context the results sit on.

    This is our own data rather than a tile service, which is the honest choice
    here — the coastlines a user sees are the same boundaries the query ran
    against, so a shape on screen never disagrees with a row in the table.
    """
    tolerance = clamp_tolerance(tolerance)
    return _collection(
        conn,
        "r.level = %(level)s AND r.geom IS NOT NULL",
        {
            "level": level,
            "tolerance": tolerance,
            "digits": _digits_for(tolerance),
        },
    )
