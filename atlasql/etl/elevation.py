"""Elevation statistics per region, from the GMTED2010 30 arc-second DEM.

Three metrics are stored rather than one: mean, min and max. Picking a single
canonical "elevation" would throw away the distinction between a high plateau
and a country with one tall mountain, which is exactly what elevation queries
are usually about.

GMTED2010 ships as 96 tiles of 30 by 20 degrees. Rather than mosaicking them
into one raster, each tile is processed on its own and the partial results are
combined: means are recovered from summed cell values and cell counts, minima
and maxima by taking the extreme across tiles. A country straddling a tile
boundary therefore gets the same answer as if the DEM were a single file, at a
fraction of the memory.

Known limitation: the mean is a plain cell mean, not area weighted. Cells
narrow toward the poles, so high-latitude countries are weighted slightly
toward their northern parts. The effect is a fraction of a percent at country
scale; correcting it needs a cosine-weighted mean, which rasterstats does not
do natively.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

import shapely
from rasterstats import zonal_stats

from atlasql import db
from atlasql.etl import availability, download
from atlasql.etl.metrics import MetricRow, upsert_metrics

log = logging.getLogger(__name__)

BASE_URL = (
    "https://edcintl.cr.usgs.gov/downloads/sciweb1/shared/topo/downloads/"
    "GMTED/Global_tiles_GMTED/300darcsec/mea"
)

# GMTED2010 vintage. Not a time series, but the year records which release the
# numbers came from.
VINTAGE_YEAR = 2010

METRICS = {
    "elevation_mean": ("Mean elevation", "Mean ground elevation above sea level."),
    "elevation_min": (
        "Minimum elevation",
        "Lowest ground elevation in the region. Depressions below sea level "
        "read negative, as they should: Egypt bottoms out near -129 m.",
    ),
    "elevation_max": (
        "Maximum elevation",
        "Highest ground elevation in the region. This is the highest cell of a "
        "1 km DEM whose cells are themselves averages, so summits are smoothed "
        "and read low: Everest comes out around 8,600 m rather than 8,848 m. "
        "It ranks regions correctly but is not a summit height.",
    ),
}

# Tile origins: latitude bands run from 70S to 70N in 20 degree steps (the
# northernmost covers 70N-90N), longitude from 180W to 150E in 30 degree steps.
TILE_LAT_ORIGINS = [-70, -50, -30, -10, 10, 30, 50, 70]
TILE_LON_ORIGINS = list(range(-180, 180, 30))


@dataclass
class Tile:
    lat: int  # south edge
    lon: int  # west edge

    @property
    def name(self) -> str:
        ns = "S" if self.lat < 0 else "N"
        ew = "W" if self.lon < 0 else "E"
        return f"{abs(self.lat):02d}{ns}{abs(self.lon):03d}{ew}"

    @property
    def filename(self) -> str:
        return f"{self.name}_20101117_gmted_mea300.tif"

    @property
    def url(self) -> str:
        ew = "W" if self.lon < 0 else "E"
        return f"{BASE_URL}/{ew}{abs(self.lon):03d}/{self.filename}"

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        # The 70N tile runs to the pole, 20 degrees tall like the rest.
        return (self.lon, self.lat, self.lon + 30, self.lat + 20)


def register_metrics(conn) -> None:
    """Put the three elevation metrics in the live registry."""
    for metric_name, (label, description) in METRICS.items():
        db.register_metric(
            conn,
            metric_name=metric_name,
            label=label,
            unit="m",
            description=f"{description} GMTED2010 30 arc-second DEM, zonal statistics.",
            source="USGS/NGA GMTED2010",
        )


def _regions_for(level: str) -> list[dict]:
    """Region geometries at one level, as GeoJSON-ish dicts for rasterstats."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, ST_AsBinary(geom) AS wkb, ST_XMin(geom) AS xmin,
                   ST_YMin(geom) AS ymin, ST_XMax(geom) AS xmax, ST_YMax(geom) AS ymax
            FROM regions
            WHERE level = %s AND geom IS NOT NULL
            ORDER BY id
            """,
            (level,),
        ).fetchall()
    return [dict(row, geometry=shapely.from_wkb(bytes(row["wkb"]))) for row in rows]


def _overlaps(region: dict, tile: Tile) -> bool:
    west, south, east, north = tile.bounds
    return not (
        region["xmax"] < west
        or region["xmin"] > east
        or region["ymax"] < south
        or region["ymin"] > north
    )


def import_elevation(level: str = "country") -> None:
    """Compute elevation statistics for every region at `level`. Safe to re-run."""
    regions = _regions_for(level)
    if not regions:
        raise RuntimeError(f"no regions with geometry at level {level!r}")
    log.info("computing elevation for %d %s regions", len(regions), level)

    all_tiles = [Tile(lat, lon) for lat in TILE_LAT_ORIGINS for lon in TILE_LON_ORIGINS]
    # Open ocean tiles touch no land region and are never downloaded.
    tiles = [t for t in all_tiles if any(_overlaps(r, t) for r in regions)]
    log.info(
        "%d of %d GMTED tiles intersect a region; fetching them",
        len(tiles),
        len(all_tiles),
    )
    paths = download.fetch_many([(t.url, t.filename) for t in tiles])

    # Summed values and cell counts, accumulated across tiles per region.
    totals: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    minima: dict[int, float] = {}
    maxima: dict[int, float] = {}

    for index, tile in enumerate(tiles, start=1):
        covered = [r for r in regions if _overlaps(r, tile)]
        path = paths.get(tile.filename)
        if path is None:
            # GMTED omits a few tiles that are entirely ocean.
            log.warning("tile %s unavailable, skipping", tile.name)
            continue

        stats = zonal_stats(
            [r["geometry"] for r in covered],
            str(path),
            stats=["sum", "count", "min", "max"],
            all_touched=False,
        )
        touched = 0
        for region, stat in zip(covered, stats, strict=True):
            if not stat["count"]:
                continue  # region overlaps the tile's box but has no land cells in it
            touched += 1
            rid = region["id"]
            totals[rid] += float(stat["sum"])
            counts[rid] += int(stat["count"])
            minima[rid] = min(minima.get(rid, float("inf")), float(stat["min"]))
            maxima[rid] = max(maxima.get(rid, float("-inf")), float(stat["max"]))
        log.info(
            "tile %s (%d/%d): %d of %d overlapping regions had data",
            tile.name,
            index,
            len(tiles),
            touched,
            len(covered),
        )

    rows: list[MetricRow] = []
    for region in regions:
        rid = region["id"]
        if not counts.get(rid):
            # No DEM cells at all. Left absent rather than written as zero: a
            # missing value is a coverage fact, a zero is a claim about the
            # terrain.
            continue
        rows.append(
            {
                "region_id": rid,
                "metric_name": "elevation_mean",
                "value": totals[rid] / counts[rid],
                "year": VINTAGE_YEAR,
            }
        )
        rows.append(
            {
                "region_id": rid,
                "metric_name": "elevation_min",
                "value": minima[rid],
                "year": VINTAGE_YEAR,
            }
        )
        rows.append(
            {
                "region_id": rid,
                "metric_name": "elevation_max",
                "value": maxima[rid],
                "year": VINTAGE_YEAR,
            }
        )

    with db.connect() as conn:
        register_metrics(conn)
        upsert_metrics(conn, rows)
        availability.refresh(conn)

    covered_regions = len(rows) // len(METRICS)
    log.info(
        "elevation: %d of %d %s regions covered (%.0f%%)",
        covered_regions,
        len(regions),
        level,
        100 * covered_regions / len(regions),
    )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_elevation()
