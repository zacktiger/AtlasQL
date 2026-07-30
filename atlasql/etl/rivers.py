"""River length and river count per region, from HydroRIVERS (HydroSHEDS).

HydroRIVERS is 8.5 million line segments globally, far too much to hold in
memory alongside 258 country polygons, so the segments are staged into PostGIS
once and every region tier is aggregated against them in SQL. Loading it is the
slow part; adding the state or county tier afterwards is one query.

Two decisions worth stating:

  * A segment counts toward the region containing its midpoint. The obvious
    alternative, clipping every segment to every polygon with ST_Intersection,
    is exact but runs for hours against millions of complex-polygon pairs.
    HydroRIVERS segments are short, so the error is confined to segments
    straddling a border, and it never double counts a river across two
    countries.
  * Rivers are counted by distinct MAIN_RIV, the main-stem id, not by segment.
    A single river is stored as dozens of segments; counting rows would report
    the Nile as dozens of rivers. "Major" means Strahler order 5 or above,
    which is what keeps the count to recognisable rivers instead of every
    seasonal creek.
"""

from __future__ import annotations

import logging
from pathlib import Path

import shapely
from pyogrio import read_dataframe, read_info

from atlasql import db
from atlasql.etl import availability, download

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://data.hydrosheds.org/file/HydroRIVERS/HydroRIVERS_v10_shp.zip"

# HydroRIVERS v1.0 was released in 2019; the year records the vintage, not a
# time series.
VINTAGE_YEAR = 2019

# Strahler stream order at or above which a river is treated as "major".
MAJOR_ORDER = 5

CHUNK = 200_000

STAGING_TABLE = "hydrorivers_segments"

_STAGING_DDL = f"""
CREATE TABLE IF NOT EXISTS {STAGING_TABLE} (
    hyriv_id  BIGINT PRIMARY KEY,
    main_riv  BIGINT NOT NULL,
    length_km DOUBLE PRECISION NOT NULL,
    ord_stra  INT,
    midpoint  GEOMETRY(Point, 4326) NOT NULL
);
"""


def _find_shapefile(directory: Path) -> Path:
    candidates = sorted(directory.rglob("HydroRIVERS_v10.shp"))
    if not candidates:
        candidates = sorted(directory.rglob("*.shp"))
    if not candidates:
        raise RuntimeError(f"no shapefile found under {directory}")
    return candidates[0]


def stage_segments(force: bool = False) -> int:
    """Load HydroRIVERS midpoints into PostGIS. Skips if already staged."""
    archive = download.fetch(ARCHIVE_URL, filename="HydroRIVERS_v10_shp.zip")
    directory = download.unzip(archive, subdir="hydrorivers_v10")
    shapefile = _find_shapefile(directory)

    total = read_info(shapefile)["features"]
    log.info("HydroRIVERS has %d segments", total)

    with db.connect() as conn:
        conn.execute(_STAGING_DDL)
        conn.commit()
        if force:
            conn.execute(f"TRUNCATE {STAGING_TABLE}")
            conn.commit()

        staged = conn.execute(f"SELECT count(*) AS n FROM {STAGING_TABLE}").fetchone()["n"]
        if staged >= total:
            log.info("all %d segments already staged, skipping load", staged)
            return staged
        if staged:
            # Chunks are read and committed in file order, so the row count is
            # also the offset to resume from. Loading 8.5 million segments takes
            # long enough that starting over after an interruption is not an
            # acceptable answer.
            log.info("resuming staging at segment %d of %d", staged, total)

        loaded = staged
        for offset in range(staged, total, CHUNK):
            gdf = read_dataframe(
                shapefile,
                columns=["HYRIV_ID", "MAIN_RIV", "LENGTH_KM", "ORD_STRA"],
                skip_features=offset,
                max_features=CHUNK,
            )
            # The midpoint along the line, not the centroid of its bounding
            # box: for a curved river those differ and only the former is
            # guaranteed to lie on the segment.
            midpoints = shapely.line_interpolate_point(
                gdf.geometry.values, 0.5, normalized=True
            )
            with conn.cursor().copy(
                f"COPY {STAGING_TABLE} (hyriv_id, main_riv, length_km, ord_stra, midpoint) "
                "FROM STDIN"
            ) as copy:
                for hyriv_id, main_riv, length_km, ord_stra, point in zip(
                    gdf["HYRIV_ID"],
                    gdf["MAIN_RIV"],
                    gdf["LENGTH_KM"],
                    gdf["ORD_STRA"],
                    midpoints,
                    strict=True,
                ):
                    copy.write_row(
                        (
                            int(hyriv_id),
                            int(main_riv),
                            float(length_km),
                            int(ord_stra),
                            shapely.to_wkb(
                                shapely.set_srid(point, 4326), hex=True, include_srid=True
                            ),
                        )
                    )
            # Commit per chunk so an interrupted load resumes instead of
            # rolling back hours of work.
            conn.commit()
            loaded += len(gdf)
            log.info("staged %d/%d segments", loaded, total)

        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{STAGING_TABLE}_midpoint "
            f"ON {STAGING_TABLE} USING GIST(midpoint)"
        )
        conn.execute(f"ANALYZE {STAGING_TABLE}")
    return loaded


_AGGREGATE_SQL = f"""
INSERT INTO metrics (region_id, metric_name, value, year)
SELECT r.id, metric_name, value, %(year)s
FROM regions r
JOIN LATERAL (
    SELECT
        -- Zero, not NULL, for a region with no segments: HydroRIVERS covers
        -- the whole globe, so "no mapped rivers" is a measurement rather than
        -- a gap. It also keeps the two river metrics at the same coverage,
        -- which they must have since they come from one pass over one source.
        COALESCE(sum(s.length_km), 0)                               AS length_km,
        count(DISTINCT s.main_riv) FILTER (WHERE s.ord_stra >= %(major_order)s)
                                                                    AS major_rivers
    FROM {STAGING_TABLE} s
    WHERE ST_Intersects(r.geom, s.midpoint)
) agg ON TRUE
CROSS JOIN LATERAL (
    VALUES ('river_length_km', agg.length_km),
           ('major_river_count', agg.major_rivers::double precision)
) AS v(metric_name, value)
WHERE r.level = %(level)s AND r.geom IS NOT NULL
ON CONFLICT (region_id, metric_name, year) DO UPDATE SET value = EXCLUDED.value
"""


def import_rivers(level: str = "country") -> None:
    """Aggregate staged segments onto every region at `level`. Safe to re-run."""
    stage_segments()

    with db.connect() as conn:
        db.register_metric(
            conn,
            metric_name="river_length_km",
            label="Total river length",
            unit="km",
            description=(
                "Combined length of the mapped drainage network: every "
                "HydroRIVERS segment whose midpoint falls inside the region. "
                "HydroRIVERS is derived from modelled flow accumulation and "
                "includes ephemeral channels, so arid countries carry far more "
                "length than their perennial rivers alone would suggest."
            ),
            source="HydroRIVERS v1.0 (HydroSHEDS)",
        )
        db.register_metric(
            conn,
            metric_name="major_river_count",
            label="Major rivers",
            unit="rivers",
            description=(
                f"Distinct main stems of Strahler order {MAJOR_ORDER} or above "
                "with a segment midpoint inside the region. Counted by main-stem "
                "id, so one river counts once however many segments it has. "
                "This counts separate river systems, which favours countries "
                "with fragmented coastal drainage over those drained by a few "
                "very large basins."
            ),
            source="HydroRIVERS v1.0 (HydroSHEDS)",
        )
        log.info("aggregating river segments onto %s regions", level)
        conn.execute(
            _AGGREGATE_SQL,
            {"year": VINTAGE_YEAR, "major_order": MAJOR_ORDER, "level": level},
        )
        availability.refresh(conn)

        summary = conn.execute(
            """
            SELECT r.name, m.value
            FROM metrics m JOIN regions r ON r.id = m.region_id
            WHERE m.metric_name = 'river_length_km' AND r.level = %s
            ORDER BY m.value DESC NULLS LAST LIMIT 5
            """,
            (level,),
        ).fetchall()
    log.info(
        "longest total river networks: %s",
        ", ".join(f"{r['name']} {r['value']:,.0f} km" for r in summary),
    )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_rivers()
