"""Import cities from GeoNames, with their population.

Cities are the first point tier. They carry a centroid and no polygon, which
means the two things every polygon tier gets for free are unavailable here:
zonal statistics need an area to average over, and a river intersection needs
an area to intersect. Combined with GDP, which has no source below the country
level, that leaves cities genuinely unable to answer most of the metric
registry - and saying so plainly is the point of the coverage table.

Cities are assigned to their parent by geometry rather than by code. GeoNames
admin-1 codes do not line up with Natural Earth's adm1_code, and a translation
table between them would be a source of quiet errors; the polygons are already
loaded, so asking which state contains the point is both simpler and correct by
construction.

Source: GeoNames (CC BY 4.0), the cities15000 dump - every settlement with a
population of 15,000 or more.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import shapely

from atlasql import db
from atlasql.etl import availability, download
from atlasql.etl.metrics import MetricRow, upsert_metrics
from atlasql.etl.regions import RegionRow, upsert_regions

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://download.geonames.org/export/dump/cities15000.zip"
POPULATION_FLOOR = 15_000

CITY_SOURCE = "geonames"

METRIC_NAME = "population"

# The dump is a rolling snapshot rather than a dated release, so the year
# records when it was taken. metrics.year is part of the primary key, so a
# later re-import under a new vintage adds rows instead of overwriting a
# differently dated measurement.
VINTAGE_YEAR = 2026

# Column order of the GeoNames dump, which ships without a header row.
COLUMNS = [
    "geonameid",
    "name",
    "asciiname",
    "alternatenames",
    "latitude",
    "longitude",
    "feature_class",
    "feature_code",
    "country_code",
    "cc2",
    "admin1_code",
    "admin2_code",
    "admin3_code",
    "admin4_code",
    "population",
    "elevation",
    "dem",
    "timezone",
    "modification_date",
]


def _read_cities(path: Path) -> list[dict]:
    """Parse the tab separated dump.

    QUOTE_NONE matters: place names legitimately contain quote characters and
    the file does not quote its fields, so the default dialect would mangle
    them.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(
            handle, fieldnames=COLUMNS, delimiter="\t", quoting=csv.QUOTE_NONE
        )
        return list(reader)


def load_cities() -> list[dict]:
    archive = download.fetch(ARCHIVE_URL, filename="cities15000.zip")
    directory = download.unzip(archive, subdir="geonames_cities15000")
    files = sorted(directory.glob("cities15000.txt"))
    if not files:
        raise RuntimeError(f"cities15000.txt not found under {directory}")
    rows = _read_cities(files[0])
    log.info("read %d cities from GeoNames", len(rows))
    return rows


# Parents are resolved in one pass per level, most specific first: a city
# inside a state polygon belongs to that state, and one that lands outside
# every state (a country with no admin-1 coverage, or a coastal point just
# beyond a boundary) falls back to the country containing it.
_ASSIGN_PARENTS_SQL = """
UPDATE regions c
SET parent_id = p.id
FROM regions p
WHERE c.level = 'city'
  AND c.source = %(source)s
  AND p.level = %(parent_level)s
  AND p.geom IS NOT NULL
  AND (c.parent_id IS NULL OR %(overwrite)s)
  AND ST_Contains(p.geom, c.centroid)
"""


# Coastal and island cities routinely land just outside every polygon: at 10m
# resolution a generalised coastline cuts inside the true shore, so a harbour
# town's coordinates fall in the sea. Snapping those to the nearest region
# within a few kilometres recovers them without inventing anything - the
# alternative is losing Ajaccio and Reykjavik's neighbours from the hierarchy.
_SNAP_TO_NEAREST_SQL = """
UPDATE regions c
SET parent_id = (
    SELECT p.id
    FROM regions p
    WHERE p.level = %(parent_level)s
      AND p.geom IS NOT NULL
      AND ST_DWithin(p.geom::geography, c.centroid::geography, %(tolerance_m)s)
    ORDER BY p.geom <-> c.centroid
    LIMIT 1
)
WHERE c.level = 'city'
  AND c.source = %(source)s
  AND c.parent_id IS NULL
"""

# Kept tight on purpose: wide enough for coastline generalisation, narrow
# enough that a city is never snapped across a strait into another country.
SNAP_TOLERANCE_M = 10_000


def _assign_parents(conn) -> None:
    """Attach every city to the smallest region that contains it."""
    for parent_level, overwrite in (("state", True), ("country", False)):
        # States first and allowed to overwrite, so a re-run after new state
        # boundaries land moves cities onto their state rather than leaving
        # them on the country they were parked on.
        updated = conn.execute(
            _ASSIGN_PARENTS_SQL,
            {"source": CITY_SOURCE, "parent_level": parent_level, "overwrite": overwrite},
        ).rowcount
        log.info("assigned %d cities to a %s by containment", updated, parent_level)

    for parent_level in ("state", "country"):
        snapped = conn.execute(
            _SNAP_TO_NEAREST_SQL,
            {
                "source": CITY_SOURCE,
                "parent_level": parent_level,
                "tolerance_m": SNAP_TOLERANCE_M,
            },
        ).rowcount
        if snapped:
            log.info(
                "snapped %d cities to the nearest %s within %d km",
                snapped,
                parent_level,
                SNAP_TOLERANCE_M // 1000,
            )

    orphans = conn.execute(
        """
        SELECT name, source_id FROM regions
        WHERE level = 'city' AND source = %s AND parent_id IS NULL
        ORDER BY name
        """,
        (CITY_SOURCE,),
    ).fetchall()
    if orphans:
        # Points that fall inside no polygon at all: islands generalised away
        # at 10m resolution, or coordinates just offshore.
        log.warning(
            "%d cities are inside no state or country polygon, e.g. %s",
            len(orphans),
            ", ".join(f"{o['name']} [{o['source_id']}]" for o in orphans[:8]),
        )


def import_cities() -> None:
    """Import cities and their population. Safe to re-run."""
    rows = load_cities()

    region_rows: list[RegionRow] = []
    populations: dict[str, int] = {}
    skipped_small = 0
    for row in rows:
        try:
            population = int(row["population"] or 0)
            longitude = float(row["longitude"])
            latitude = float(row["latitude"])
        except (TypeError, ValueError):
            log.warning("unparseable city record %s, skipping", row.get("geonameid"))
            continue
        if population < POPULATION_FLOOR:
            # The dump is meant to be pre-filtered; anything below the floor
            # would make the tier's definition a lie.
            skipped_small += 1
            continue

        source_id = str(row["geonameid"]).strip()
        point = shapely.Point(longitude, latitude)
        region_rows.append(
            {
                "name": (row["name"] or row["asciiname"] or source_id).strip(),
                "level": "city",
                "parent_id": None,  # assigned spatially once the rows exist
                "source": CITY_SOURCE,
                "source_id": source_id,
                "wkb": None,
                "point_wkb": point.wkb,
            }
        )
        populations[source_id] = population

    with db.connect() as conn:
        ids = upsert_regions(conn, region_rows)
        _assign_parents(conn)

        db.register_metric(
            conn,
            metric_name=METRIC_NAME,
            label="Population",
            unit="people",
            description=(
                "Number of inhabitants. At city level this is the GeoNames "
                "figure for the settlement itself, not its metropolitan area, "
                "so a city can read smaller than the conurbation around it."
            ),
            source="GeoNames (CC BY 4.0)",
        )
        metric_rows: list[MetricRow] = [
            {
                "region_id": ids[source_id],
                "metric_name": METRIC_NAME,
                "value": float(population),
                "year": VINTAGE_YEAR,
            }
            for source_id, population in populations.items()
        ]
        upsert_metrics(conn, metric_rows)
        availability.refresh(conn)

    if skipped_small:
        log.warning(
            "%d records were below the %d population floor and were skipped",
            skipped_small,
            POPULATION_FLOOR,
        )
    log.info("imported %d cities", len(region_rows))


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_cities()
