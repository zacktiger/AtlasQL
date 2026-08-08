"""County boundaries from geoBoundaries CGAZ ADM2.

The fourth tier, and the first not to come from Natural Earth. The plan names
geoBoundaries for county and below for a licensing reason worth restating: GADM
is the obvious alternative and its redistribution terms forbid what this project
does with the data. geoBoundaries is open.

**"County" means second-level administrative division, and countries do not
agree on what that is.** Brazil contributes 5,570 municipalities, Romania 3,235
communes, the United States 3,231 counties, Japan 1,731 municipalities. These
are not comparable units of population or area, and nothing here pretends they
are — the tier is defined by its position in each country's own hierarchy, not
by size. That is a property of administrative geography rather than of this
import, and it applies to the state tier too, where Natural Earth ADM1 gives
Malta 68 local councils and the United States 51 states.

**Only genuine ADM2 units are imported.** CGAZ substitutes coarser geometry for
countries that have no second-level data: of 49,349 features, 49,015 are ADM2
and the rest are ADM1 (312, for Malta, Moldova, Montenegro, Greenland and a
dozen small island states), two whole countries at ADM0, and 20 disputed areas.
Loading those as counties would put provinces and entire countries in the county
tier, so that a query for counties would silently be answered partly by states —
the same reasoning that keeps `major_river_count` to Strahler order 5 and above
rather than quietly redefining what a river is. A country with no ADM2 gets no
counties, which is the honest answer and makes its `county_count` zero.

**Parenting uses the source's own country code, and geometry only below it.**
Each county declares its ISO3 country, so the country is read rather than
inferred; geometry then picks which state within *that* country it belongs to.
This is what makes cross-border misassignment impossible, which matters because
geoBoundaries ADM2 and Natural Earth ADM1 are different datasets whose borders
do not coincide — a county near an international border can easily have its
interior point land in the neighbouring country's province. Within a country the
worst case is the wrong province, which is a small error; across one it would be
a county filed under the wrong nation.

A county whose interior point lands in no state at all falls back to the nearest
state in its own country, and one whose country has no states at all stays
parented to the country. Both are the pattern the city import already uses.
"""

from __future__ import annotations

import logging

from pyogrio import read_dataframe, read_info

from atlasql import db
from atlasql.etl import availability, download
from atlasql.etl.natural_earth import COUNTRY_SOURCE
from atlasql.etl.regions import RegionRow, existing_ids, upsert_regions

log = logging.getLogger(__name__)

ARCHIVE_URL = (
    "https://github.com/wmgeolab/geoBoundaries/raw/main/"
    "releaseData/CGAZ/geoBoundariesCGAZ_ADM2.gpkg"
)
FILENAME = "geoBoundariesCGAZ_ADM2.gpkg"

COUNTY_SOURCE = "geoboundaries_adm2"

# The only shapeType that is actually a second-level division. See the module
# docstring for what the others are and why they are not counties.
ADM2 = "ADM2"

# Read in chunks: 49,015 polygons held as shapely objects at once is gigabytes
# of resident memory for no benefit, since each chunk is written and dropped.
CHUNK = 5_000

# geoBoundaries and Natural Earth disagree on a few ISO3 codes. Mapping them
# explicitly beats losing the countries silently — the same problem, and the
# same fix, as ISO3_ALIASES in world_bank.py, though the sets differ because
# each source has its own idea of which codes are current. ESH carries no ADM2
# today and is mapped anyway, so that a release which adds it does not have its
# counties quietly dropped.
ISO3_ALIASES = {
    "SSD": "SDS",  # South Sudan
    "XKX": "KOS",  # Kosovo
    "ESH": "SAH",  # Western Sahara
}


# Postgres evaluates UPDATE ... FROM against the pre-update snapshot, so
# `s.parent_id = ct.parent_id` still reads the country this county was inserted
# under while the statement rewrites that column to a state. That is what scopes
# the geometric search to one country.
#
# ST_PointOnSurface rather than the stored centroid: a centroid is not
# guaranteed to lie inside its own polygon, and a county shaped like a crescent
# would be parented by a point in the middle of its neighbour.
_ASSIGN_STATES_SQL = """
UPDATE regions ct
SET parent_id = s.id
FROM regions s
WHERE ct.level = 'county'
  AND ct.source = %(source)s
  AND ct.geom IS NOT NULL
  AND s.level = 'state'
  AND s.geom IS NOT NULL
  AND s.parent_id = ct.parent_id
  AND ST_Contains(s.geom, ST_PointOnSurface(ct.geom))
"""

# Whatever the containment pass missed, where the country does have states:
# boundaries from two datasets never line up exactly, so a county along a
# provincial border can land in the gap between them.
_SNAP_TO_NEAREST_SQL = """
UPDATE regions ct
SET parent_id = (
    SELECT s.id FROM regions s
    WHERE s.level = 'state' AND s.geom IS NOT NULL AND s.parent_id = ct.parent_id
    ORDER BY s.geom <-> ct.centroid
    LIMIT 1
)
WHERE ct.level = 'county'
  AND ct.source = %(source)s
  AND EXISTS (
        SELECT 1 FROM regions p WHERE p.id = ct.parent_id AND p.level = 'country'
      )
  AND EXISTS (
        SELECT 1 FROM regions s
        WHERE s.level = 'state' AND s.geom IS NOT NULL AND s.parent_id = ct.parent_id
      )
"""


def _feature_name(name, shape_id: str) -> str:
    """A usable name. 37 features ship without one; the id beats an empty cell."""
    text = "" if name is None else str(name).strip()
    return text or f"[{shape_id}]"


def import_counties() -> None:
    """Import geoBoundaries ADM2 as the county tier. Safe to re-run."""
    path = download.fetch(ARCHIVE_URL, FILENAME)
    total = read_info(path)["features"]
    log.info("geoBoundaries CGAZ holds %d features; importing the ADM2 ones", total)

    with db.connect() as conn:
        country_ids = existing_ids(conn, COUNTRY_SOURCE)
        if not country_ids:
            raise RuntimeError("no country regions found; run import-natural-earth first")

        imported = 0
        skipped_type: dict[str, int] = {}
        skipped_empty = 0
        unmatched: dict[str, int] = {}

        for offset in range(0, total, CHUNK):
            gdf = read_dataframe(
                path,
                columns=["shapeName", "shapeID", "shapeGroup", "shapeType"],
                skip_features=offset,
                max_features=CHUNK,
            )
            rows: list[RegionRow] = []
            for _, feature in gdf.iterrows():
                shape_type = str(feature["shapeType"])
                if shape_type != ADM2:
                    skipped_type[shape_type] = skipped_type.get(shape_type, 0) + 1
                    continue
                geometry = feature.geometry
                if geometry is None or geometry.is_empty:
                    skipped_empty += 1
                    continue

                raw_code = str(feature["shapeGroup"]).strip().upper()
                code = ISO3_ALIASES.get(raw_code, raw_code)
                parent_id = country_ids.get(code)
                if parent_id is None:
                    unmatched[raw_code] = unmatched.get(raw_code, 0) + 1
                    continue

                shape_id = str(feature["shapeID"])
                rows.append(
                    {
                        "name": _feature_name(feature["shapeName"], shape_id),
                        "level": "county",
                        # The country to begin with; the passes below refine it
                        # to a state, and leave it here when there is none.
                        "parent_id": parent_id,
                        "source": COUNTY_SOURCE,
                        "source_id": shape_id,
                        "wkb": geometry.wkb,
                    }
                )

            if rows:
                upsert_regions(conn, rows)
                imported += len(rows)
            # Per chunk, so an interrupted import keeps what it has already
            # written and re-running resumes rather than restarting.
            conn.commit()
            log.info("read %d/%d features, imported %d counties", min(offset + CHUNK, total), total, imported)

        log.info("assigning counties to the state inside their own country")
        contained = conn.execute(_ASSIGN_STATES_SQL, {"source": COUNTY_SOURCE}).rowcount
        snapped = conn.execute(_SNAP_TO_NEAREST_SQL, {"source": COUNTY_SOURCE}).rowcount
        conn.commit()

        still_country = conn.execute(
            """
            SELECT count(*) AS n FROM regions ct
            JOIN regions p ON p.id = ct.parent_id
            WHERE ct.level = 'county' AND ct.source = %s AND p.level = 'country'
            """,
            (COUNTY_SOURCE,),
        ).fetchone()["n"]

        availability.refresh(conn)

    log.info(
        "counties: %d imported (%d contained by a state, %d snapped to the nearest, "
        "%d left under their country for want of one)",
        imported,
        contained,
        snapped,
        still_country,
    )
    if skipped_type:
        log.info(
            "skipped %d non-ADM2 features, which are not counties: %s",
            sum(skipped_type.values()),
            ", ".join(f"{k}={v}" for k, v in sorted(skipped_type.items())),
        )
    if skipped_empty:
        log.warning("skipped %d features with no geometry", skipped_empty)
    if unmatched:
        log.warning(
            "%d counties had no matching country region, add the code to "
            "ISO3_ALIASES if it should have one: %s",
            sum(unmatched.values()),
            ", ".join(f"{k}={v}" for k, v in sorted(unmatched.items())),
        )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_counties()
