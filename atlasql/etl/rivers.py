"""Major river length and count per region, from HydroRIVERS (HydroSHEDS).

HydroRIVERS is 8.5 million line segments globally. Only the major ones are
staged — Strahler stream order 5 and above — and that single decision is what
makes this job proportionate to what it produces.

The numbers behind it, measured on the full dataset:

    order 1   4,367,076 segments (51.5%)   18,786,783 km
    order 2   2,001,402 segments (23.6%)    8,968,266 km
    order 3   1,044,886 segments (12.3%)    4,272,499 km
    order 4     554,340 segments ( 6.5%)    2,053,274 km
    order 5+    510,179 segments ( 6.0%)    1,768,693 km

Orders 1 to 4 are 94% of the segments and 95% of the length, and they are not
rivers in any sense a person means the word. HydroRIVERS is derived from
modelled flow accumulation over a digital elevation model, so an order-1 reach
is a computed headwater trickle — including channels that carry water only after
rain. Summing them produced a "total river length" of 3.9 million km for the
largest country, a number dominated by modelled drainage nobody has ever seen,
and cost 1.27 GB of staging in Postgres plus 2.7 GB of cached shapefile on disk
to produce two columns of 4,854 rows each.

Staging order 5 and above keeps every river you would name and 6% of the rows.

Two decisions carried over from the original job, both still true:

  * A segment counts toward the region containing its midpoint. The obvious
    alternative, clipping every segment to every polygon with ST_Intersection,
    is exact but runs for hours against millions of complex-polygon pairs.
    HydroRIVERS segments are short, so the error is confined to segments
    straddling a border, and it never double counts a river across two
    countries.
  * Rivers are counted by distinct MAIN_RIV, the main-stem id, not by segment.
    A single river is stored as dozens of segments; counting rows would report
    the Nile as dozens of rivers.
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

# Strahler stream order at or above which a river is treated as major. This is
# now both the reporting threshold and the staging threshold: nothing below it
# is stored, because nothing below it is used.
MAJOR_ORDER = 5

CHUNK = 200_000

STAGING_TABLE = "hydrorivers_segments"
STATE_TABLE = "hydrorivers_staging_state"

_STAGING_DDL = f"""
CREATE TABLE IF NOT EXISTS {STAGING_TABLE} (
    hyriv_id  BIGINT PRIMARY KEY,
    main_riv  BIGINT NOT NULL,
    length_km DOUBLE PRECISION NOT NULL,
    ord_stra  INT,
    midpoint  GEOMETRY(Point, 4326) NOT NULL
);
"""

# Staging is resumable, and once it is filtered the row count no longer says how
# far through the source file we are — 510,179 rows is a complete load, not a
# load that stopped at 6%. So progress is recorded against features *scanned*.
# min_order is stored too: lowering it later has to invalidate what is staged,
# since the existing rows would be a subset of what was asked for.
_STATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
    id               INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    scanned_features BIGINT NOT NULL DEFAULT 0,
    total_features   BIGINT,
    min_order        INT NOT NULL
);
"""


def _find_shapefile(directory: Path) -> Path:
    candidates = sorted(directory.rglob("HydroRIVERS_v10.shp"))
    if not candidates:
        candidates = sorted(directory.rglob("*.shp"))
    if not candidates:
        raise RuntimeError(f"no shapefile found under {directory}")
    return candidates[0]


def _read_state(conn) -> dict | None:
    conn.execute(_STATE_DDL)
    return conn.execute(f"SELECT * FROM {STATE_TABLE} WHERE id = 1").fetchone()


def prune_staging(conn, min_order: int = MAJOR_ORDER) -> int:
    """Drop staged segments below `min_order`, reclaiming the space.

    Rewrites rather than deleting in place: this discards ~94% of the rows, and
    a DELETE of that size leaves the table the same size on disk until a VACUUM
    FULL rewrites it anyway, having written a great deal of WAL first.

    Returns the number of rows removed.
    """
    before = conn.execute(f"SELECT count(*) AS n FROM {STAGING_TABLE}").fetchone()["n"]
    below = conn.execute(
        f"SELECT count(*) AS n FROM {STAGING_TABLE} WHERE ord_stra < %s", (min_order,)
    ).fetchone()["n"]
    if not below:
        return 0

    log.info(
        "pruning %d of %d staged segments below Strahler order %d",
        below,
        before,
        min_order,
    )
    conn.execute(
        f"CREATE TABLE {STAGING_TABLE}_pruned AS "
        f"SELECT * FROM {STAGING_TABLE} WHERE ord_stra >= %s",
        (min_order,),
    )
    conn.execute(f"DROP TABLE {STAGING_TABLE}")
    conn.execute(f"ALTER TABLE {STAGING_TABLE}_pruned RENAME TO {STAGING_TABLE}")
    conn.execute(f"ALTER TABLE {STAGING_TABLE} ADD PRIMARY KEY (hyriv_id)")
    conn.execute(
        f"CREATE INDEX idx_{STAGING_TABLE}_midpoint "
        f"ON {STAGING_TABLE} USING GIST(midpoint)"
    )
    conn.execute(f"ANALYZE {STAGING_TABLE}")
    conn.commit()
    return below


def stage_segments(force: bool = False, min_order: int = MAJOR_ORDER) -> int:
    """Load major HydroRIVERS midpoints into PostGIS. Skips if already staged."""
    with db.connect() as conn:
        conn.execute(_STAGING_DDL)
        state = _read_state(conn)
        conn.commit()

        if force:
            conn.execute(f"TRUNCATE {STAGING_TABLE}")
            conn.execute(f"DELETE FROM {STATE_TABLE}")
            conn.commit()
            state = None

        # An existing table staged at a lower threshold is pruned down rather
        # than reloaded: the rows we want are already there.
        if state is not None and state["min_order"] < min_order:
            prune_staging(conn, min_order)
            conn.execute(
                f"UPDATE {STATE_TABLE} SET min_order = %s WHERE id = 1", (min_order,)
            )
            conn.commit()
            state = _read_state(conn)

        # A table staged before this bookkeeping existed has no state row. If it
        # already holds only major segments, adopt it rather than reloading.
        if state is None:
            staged = conn.execute(
                f"SELECT count(*) AS n FROM {STAGING_TABLE}"
            ).fetchone()["n"]
            if staged:
                pruned = prune_staging(conn, min_order)
                total = read_info(
                    _find_shapefile(
                        download.unzip(
                            download.fetch(
                                ARCHIVE_URL, filename="HydroRIVERS_v10_shp.zip"
                            ),
                            subdir="hydrorivers_v10",
                        )
                    )
                )["features"]
                # It was a complete load, whether or not it needed pruning.
                conn.execute(
                    f"INSERT INTO {STATE_TABLE} (id, scanned_features, total_features, "
                    "min_order) VALUES (1, %s, %s, %s)",
                    (total, total, min_order),
                )
                conn.commit()
                log.info("adopted an existing staging table (%d pruned)", pruned)
                return conn.execute(
                    f"SELECT count(*) AS n FROM {STAGING_TABLE}"
                ).fetchone()["n"]

    archive = download.fetch(ARCHIVE_URL, filename="HydroRIVERS_v10_shp.zip")
    directory = download.unzip(archive, subdir="hydrorivers_v10")
    shapefile = _find_shapefile(directory)
    total = read_info(shapefile)["features"]
    log.info("HydroRIVERS has %d segments; staging order >= %d", total, min_order)

    with db.connect() as conn:
        state = _read_state(conn)
        if state is None:
            conn.execute(
                f"INSERT INTO {STATE_TABLE} (id, scanned_features, total_features, "
                "min_order) VALUES (1, 0, %s, %s)",
                (total, min_order),
            )
            conn.commit()
            scanned = 0
        else:
            scanned = state["scanned_features"]
            if scanned >= total:
                staged = conn.execute(
                    f"SELECT count(*) AS n FROM {STAGING_TABLE}"
                ).fetchone()["n"]
                log.info("all %d segments already scanned, skipping load", total)
                return staged
            log.info("resuming staging at source feature %d of %d", scanned, total)

        # Chunks are read in file order, so the count of features *scanned* is
        # also the offset to resume from. Filtering happens after the read, so
        # that offset stays a position in the source file rather than in the
        # subset — which is what makes an interrupted load resumable.
        staged = conn.execute(f"SELECT count(*) AS n FROM {STAGING_TABLE}").fetchone()["n"]
        for offset in range(scanned, total, CHUNK):
            gdf = read_dataframe(
                shapefile,
                columns=["HYRIV_ID", "MAIN_RIV", "LENGTH_KM", "ORD_STRA"],
                skip_features=offset,
                max_features=CHUNK,
            )
            read = len(gdf)
            gdf = gdf[gdf["ORD_STRA"] >= min_order]

            if len(gdf):
                # The midpoint along the line, not the centroid of its bounding
                # box: for a curved river those differ and only the former is
                # guaranteed to lie on the segment.
                midpoints = shapely.line_interpolate_point(
                    gdf.geometry.values, 0.5, normalized=True
                )
                with conn.cursor().copy(
                    f"COPY {STAGING_TABLE} (hyriv_id, main_riv, length_km, ord_stra, "
                    "midpoint) FROM STDIN"
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
                                    shapely.set_srid(point, 4326),
                                    hex=True,
                                    include_srid=True,
                                ),
                            )
                        )
                staged += len(gdf)

            conn.execute(
                f"UPDATE {STATE_TABLE} SET scanned_features = %s WHERE id = 1",
                (offset + read,),
            )
            # Commit per chunk so an interrupted load resumes instead of
            # rolling back hours of work.
            conn.commit()
            log.info("scanned %d/%d segments, staged %d", offset + read, total, staged)

        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{STAGING_TABLE}_midpoint "
            f"ON {STAGING_TABLE} USING GIST(midpoint)"
        )
        conn.execute(f"ANALYZE {STAGING_TABLE}")
    return staged


_AGGREGATE_SQL = f"""
INSERT INTO metrics (region_id, metric_name, value, year)
SELECT r.id, metric_name, value, %(year)s
FROM regions r
JOIN LATERAL (
    SELECT
        -- Zero, not NULL, for a region with no segments: HydroRIVERS covers
        -- the whole globe, so "no major rivers" is a measurement rather than
        -- a gap. It also keeps the two river metrics at the same coverage,
        -- which they must have since they come from one pass over one source.
        COALESCE(sum(s.length_km), 0)      AS length_km,
        count(DISTINCT s.main_riv)         AS major_rivers
    FROM {STAGING_TABLE} s
    WHERE ST_Intersects(r.geom, s.midpoint)
) agg ON TRUE
CROSS JOIN LATERAL (
    VALUES ('major_river_length_km', agg.length_km),
           ('major_river_count', agg.major_rivers::double precision)
) AS v(metric_name, value)
WHERE r.level = %(level)s AND r.geom IS NOT NULL
ON CONFLICT (region_id, metric_name, year) DO UPDATE SET value = EXCLUDED.value
"""

# The old name measured something else: every modelled channel including
# ephemeral order-1 reaches. Leaving the name in place with major-river values
# would silently redefine what "> 1000" means to anyone who had used it, so the
# metric is retired rather than quietly reinterpreted.
RETIRED_METRICS = ("river_length_km",)


def _retire_old_metrics(conn) -> None:
    for name in RETIRED_METRICS:
        removed = conn.execute(
            "DELETE FROM metrics WHERE metric_name = %s", (name,)
        ).rowcount
        conn.execute("DELETE FROM metric_availability WHERE metric_name = %s", (name,))
        conn.execute("DELETE FROM metric_definitions WHERE metric_name = %s", (name,))
        if removed:
            log.info("retired %s (%d values removed)", name, removed)


def import_rivers(level: str = "country") -> None:
    """Aggregate staged major segments onto every region at `level`. Re-runnable."""
    stage_segments()

    with db.connect() as conn:
        _retire_old_metrics(conn)
        db.register_metric(
            conn,
            metric_name="major_river_length_km",
            label="Major river length",
            unit="km",
            description=(
                f"Combined length of river reaches of Strahler order {MAJOR_ORDER} "
                "or above whose midpoint falls inside the region. Lower orders — "
                "modelled headwater and ephemeral channels, which are 95% of "
                "HydroRIVERS by length — are excluded, so this measures the "
                "recognisable river network rather than total modelled drainage."
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
        log.info("aggregating major river segments onto %s regions", level)
        conn.execute(
            _AGGREGATE_SQL,
            {"year": VINTAGE_YEAR, "level": level},
        )
        availability.refresh(conn)

        summary = conn.execute(
            """
            SELECT r.name, m.value
            FROM metrics m JOIN regions r ON r.id = m.region_id
            WHERE m.metric_name = 'major_river_length_km' AND r.level = %s
            ORDER BY m.value DESC NULLS LAST LIMIT 5
            """,
            (level,),
        ).fetchall()
    log.info(
        "longest major river networks: %s",
        ", ".join(f"{r['name']} {r['value']:,.0f} km" for r in summary),
    )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_rivers()
