"""Coastline length per region, derived from the boundaries already loaded.

No download and no new source. The definition is the whole job:

    coast = the region's boundary, minus every piece of it that another
            landmass is on the other side of

What is left is the part of the outline facing open water. Everything needed to
compute that is already in `regions`, because the boundaries this draws on are
the same ones the globe draws.

**Two things get subtracted, and the second one is not redundant.** Neighbours
at the same level cover the ordinary case — a country's borders with other
countries, a state's borders with other states. But the tiers do not tile the
world equally: seven countries have no ADM1 subdivisions in Natural Earth, so a
state beside one of them has a land border that no state is on the other side
of. Subtracting neighbouring *countries* as well (excluding the region's own
parent, whose polygon is what the region sits inside) closes that gap. It is a
small correction and a real one: without it Artigas and Corrientes, both inland
departments, come back with 3 and 4 km of "coast" borrowed from Brazilian
Island, and Santa Cruz gains 71 km of Southern Patagonian Ice Field. Being
wrongly coastal at all is a worse error than the size of the number suggests.

**The tolerance is zero, and that is a measurement not an assumption.** Natural
Earth's polygons share exact boundary geometry between neighbours, so the
difference needs no epsilon to absorb slivers. Buffering neighbours by 0.001°
before subtracting changes the result by under 0.01% and leaves every landlocked
country at exactly zero either way — so the fudge factor was measured to be
unnecessary rather than assumed to be. Every landlocked country coming out at
exactly 0.0 is the check that the subtraction is finding real shared borders and
not approximately-shared ones.

**Length is geodesic.** The geometry is in degrees, where a degree of longitude
is 111 km at the equator and nothing at the pole, so `ST_Length` on the raw
geometry would be a number in no unit at all. Casting to `geography` measures on
the spheroid and returns metres.

**Zero is a measurement, not a gap.** A landlocked country has no coast; that is
a fact about Switzerland, not missing data. It also makes `coastline_km == 0`
the query that finds the landlocked countries, which is the sort of thing this
metric is for.

The honesty caveat, which is in the metric description because users need it:
coastline length is famously scale-dependent — measure with a finer ruler and
you get a longer answer, without limit. Published national figures disagree with
each other by factors of two to five for exactly this reason, and ours will not
match any particular one of them. What it does do is measure every region on the
same linework at the same generalisation, which is what makes comparing and
ranking them meaningful. That is the question this engine asks.
"""

from __future__ import annotations

import logging

from atlasql import config, db
from atlasql.etl import availability

log = logging.getLogger(__name__)

METRIC = "coastline_km"

# Natural Earth 1:50m is what the boundaries were imported from, and a
# coastline length means nothing without the scale it was measured at.
SCALE = "1:50m"
VINTAGE_YEAR = 2024

# Cities are points and have no outline to measure.
UNSUPPORTED_LEVELS = ("city",)

DESCRIPTION = (
    "Length of the region's boundary that faces open water, measured on the "
    f"spheroid from Natural Earth {SCALE} boundaries — the same outlines this "
    "engine draws — by subtracting every border it shares with a neighbouring "
    "region or country. Landlocked regions are 0, which is a measurement rather "
    "than a gap, so `coastline_km == 0` is how you find them. Treat the number "
    "as comparable rather than authoritative: coastline length grows without "
    "limit as the ruler gets finer, which is why published national figures "
    f"disagree with each other by factors of two to five. Measuring every region "
    "on the same linework at the same generalisation is what makes ranking them "
    "mean something; matching any particular published figure is not something "
    "any single number can do."
)

SOURCE = f"Derived from Natural Earth {SCALE} boundaries"

# The country term needs two guards, and both were bugs before they were guards.
#
#   * It applies only *below* the country tier. Countries are what a continent
#     is made of, not what it borders, so applying it there subtracts every
#     country inside the continent and reports all seven as landlocked — which
#     is exactly what the first run did.
#   * It excludes the region's ancestor country, found by walking up rather than
#     by reading parent_id. For a state those are the same region and parent_id
#     would do; for a county they are not, and subtracting the country a county
#     sits inside would erase its whole boundary the same way. Walking up costs
#     nothing and stays right when the county tier lands.
_COMPUTE_SQL = """
WITH RECURSIVE up AS (
    SELECT id AS region_id, id AS node_id, parent_id, level
    FROM regions WHERE level = %(level)s
    UNION ALL
    SELECT u.region_id, r.id, r.parent_id, r.level
    FROM regions r JOIN up u ON r.id = u.parent_id
),
ancestor_country AS (
    SELECT region_id, node_id AS country_id FROM up WHERE level = 'country'
)
INSERT INTO metrics (region_id, metric_name, value, year)
SELECT c.id,
       %(metric)s,
       ST_Length(
           ST_Difference(
               ST_Boundary(c.geom),
               COALESCE(
                   (SELECT ST_Union(n.geom)
                    FROM regions n
                    WHERE n.geom IS NOT NULL
                      AND n.id <> c.id
                      AND (
                          n.level = c.level
                          OR (
                              %(subtract_countries)s
                              AND n.level = 'country'
                              AND n.id IS DISTINCT FROM (
                                  SELECT a.country_id FROM ancestor_country a
                                  WHERE a.region_id = c.id
                              )
                          )
                      )
                      AND ST_Intersects(n.geom, c.geom)),
                   ST_SetSRID('GEOMETRYCOLLECTION EMPTY'::geometry, 4326)
               )
           )::geography
       ) / 1000.0,
       %(year)s
FROM regions c
WHERE c.level = %(level)s AND c.geom IS NOT NULL
ON CONFLICT (region_id, metric_name, year) DO UPDATE SET value = EXCLUDED.value
"""


def _is_below_country(level: str) -> bool:
    """Whether the country term applies. See the comment above `_COMPUTE_SQL`."""
    if level not in config.LEVELS:
        raise ValueError(f"unknown level {level!r}; expected one of {config.LEVELS}")
    return config.LEVELS.index(level) > config.LEVELS.index("country")


def import_coastline(level: str = "country") -> None:
    """Compute coastline length for every region at `level`. Safe to re-run."""
    if level in UNSUPPORTED_LEVELS:
        raise RuntimeError(
            f"{METRIC} is not computed at {level} level: cities are point data "
            "and have no outline to measure"
        )

    with db.connect() as conn:
        db.register_metric(
            conn,
            metric_name=METRIC,
            label="Coastline length",
            unit="km",
            description=DESCRIPTION,
            source=SOURCE,
        )

        log.info("measuring coastline for every %s (this walks every shared border)", level)
        written = conn.execute(
            _COMPUTE_SQL,
            {
                "metric": METRIC,
                "year": VINTAGE_YEAR,
                "level": level,
                "subtract_countries": _is_below_country(level),
            },
        ).rowcount
        availability.refresh(conn)

        landlocked = conn.execute(
            """
            SELECT count(*) AS n FROM metrics m
            JOIN regions r ON r.id = m.region_id
            WHERE m.metric_name = %s AND r.level = %s AND m.value = 0
            """,
            (METRIC, level),
        ).fetchone()["n"]
        longest = conn.execute(
            """
            SELECT r.name, m.value FROM metrics m
            JOIN regions r ON r.id = m.region_id
            WHERE m.metric_name = %s AND r.level = %s
            ORDER BY m.value DESC LIMIT 5
            """,
            (METRIC, level),
        ).fetchall()

    log.info(
        "%s: %d %s regions measured, %d landlocked; longest: %s",
        METRIC,
        written,
        level,
        landlocked,
        ", ".join(f"{r['name']} {r['value']:,.0f} km" for r in longest),
    )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_coastline()
