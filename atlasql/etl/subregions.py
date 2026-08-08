"""How many regions of each lower tier lie inside a region.

The cheapest metric in the registry: no download, no raster, no staging table,
no geospatial dependency at all. Everything it needs is already in `regions`,
because the hierarchy the rest of the product is built on *is* the containment
relation. It runs in milliseconds.

Three decisions are worth stating, because each had a plausible alternative.

  * **Containment is the hierarchy, not geometry.** A city counts toward a
    country because its parent's parent is that country, not because
    ST_Contains says so. The two would mostly agree, and where they disagreed
    the map would show a result whose stated parent contradicts the count
    beside it. The hierarchy is what the product already tells the user, so it
    is what gets counted. Descent is recursive rather than one level deep:
    cities hang off states, so counting only direct children would report every
    country as having no cities.

  * **One metric per counted tier, not one generic `subregion_count`.** The
    generic name is shorter and wrong for the same reason the two GDP metrics
    stay separate: its unit would change with the level. `subregion_count > 30`
    would mean thirty countries on a continent and thirty states in a country,
    so a threshold would stop meaning one thing. Naming each metric after what
    it counts fixes the unit, and lets one country carry `state_count` and
    `city_count` at once — which is what makes "countries with more than 20
    states and more than 200 cities" a query rather than two.

  * **Zero is a measurement; an unimported tier is not.** A country with no
    subdivisions in Natural Earth gets 0, exactly as a region with no major
    rivers does — seven of them exist and they are uninhabited banks, shoals
    and Bir Tawil. But a *level* with no regions at all gets no metric written,
    which is the important half: writing `city_count = 0` onto every country of
    a database where cities were simply never imported would report 100%
    coverage for a number nobody measured.

This job reads what other jobs wrote, so it is the one whose output goes stale
when a region import runs. Run it after the region importers, not before.
"""

from __future__ import annotations

import logging

from atlasql import config, db
from atlasql.etl import availability

log = logging.getLogger(__name__)

# The counts describe the region snapshot currently loaded — Natural Earth
# boundaries plus the GeoNames dump — rather than a dated release of their own.
# metrics.year is part of the primary key, so this records which snapshot was
# counted; re-importing regions under a new vintage adds rows rather than
# silently overwriting a count of a different world.
VINTAGE_YEAR = 2026


def metric_name(child_level: str) -> str:
    """The metric that counts `child_level` regions. One definition, used to
    register, to write and to read back, so the three cannot drift apart."""
    return f"{child_level}_count"


# Descend from every region at the target level, tagging each descendant with
# the root it came from. depth is carried so the roots themselves (depth 0) can
# be dropped without comparing levels — levels descend strictly today, and this
# keeps the query correct if a tier is ever added that does not.
_COUNT_SQL = """
WITH RECURSIVE tree AS (
    SELECT id AS root_id, id, level, 0 AS depth
    FROM regions
    WHERE level = %(level)s
    UNION ALL
    SELECT t.root_id, r.id, r.level, t.depth + 1
    FROM regions r
    JOIN tree t ON r.parent_id = t.id
),
counted AS (
    SELECT root_id, level AS child_level, count(*)::double precision AS n
    FROM tree
    WHERE depth > 0
    GROUP BY root_id, level
)
INSERT INTO metrics (region_id, metric_name, value, year)
SELECT r.id, target.metric_name, COALESCE(c.n, 0), %(year)s
FROM regions r
CROSS JOIN unnest(%(child_levels)s::text[], %(metric_names)s::text[])
     AS target(child_level, metric_name)
LEFT JOIN counted c ON c.root_id = r.id AND c.child_level = target.child_level
WHERE r.level = %(level)s
ON CONFLICT (region_id, metric_name, year) DO UPDATE SET value = EXCLUDED.value
"""


# Which levels actually appear *underneath* regions at the target level. Being
# lower in config.LEVELS is not enough, and the county tier is what proved it:
# cities are parented to states, so no city is a descendant of a county. Asking
# only "does the city level hold regions" would answer yes, write city_count = 0
# onto all 49,015 counties, and report it at 100% coverage — a number nobody
# measured, dressed as complete data, which is the exact failure this module was
# written to avoid. Reachability by descent is the honest question.
_DESCENDANT_LEVELS_SQL = """
WITH RECURSIVE tree AS (
    SELECT id, level, 0 AS depth FROM regions WHERE level = %(level)s
    UNION ALL
    SELECT r.id, r.level, t.depth + 1
    FROM regions r JOIN tree t ON r.parent_id = t.id
)
SELECT DISTINCT level FROM tree WHERE depth > 0
"""


def _populated_child_levels(conn, level: str) -> list[str]:
    """Levels that actually hang below `level` in the hierarchy, most general first."""
    if level not in config.LEVELS:
        raise ValueError(f"unknown level {level!r}; expected one of {config.LEVELS}")
    below = config.LEVELS[config.LEVELS.index(level) + 1 :]
    rows = conn.execute(_DESCENDANT_LEVELS_SQL, {"level": level}).fetchall()
    reachable = {row["level"] for row in rows}
    return [child for child in below if child in reachable]


def _describe(child_level: str) -> tuple[str, str, str, str]:
    """(label, unit, description, source) for the metric counting `child_level`."""
    # Imported here rather than at module scope so this job, which is otherwise
    # pure SQL, does not pull in the geospatial stack just to quote a number.
    from atlasql.etl.geonames import POPULATION_FLOOR

    plural = {"country": "countries", "state": "states", "county": "counties", "city": "cities"}[
        child_level
    ]
    shared = (
        f"Number of {plural} inside the region, counted down the administrative "
        "hierarchy rather than by re-testing geometry, so it always agrees with "
        "the parent shown beside a result. Nested tiers are included: a city "
        "counts toward its state and its country alike."
    )
    caveat = {
        "country": "",
        "state": (
            " Subdivisions come from Natural Earth ADM1, so a region with none "
            "there counts zero — which for the handful this affects (uninhabited "
            "banks and shoals, Bir Tawil) is the fact rather than a gap."
        ),
        "county": "",
        "city": (
            f" Cities are the GeoNames cities15000 set, so this counts settlements "
            f"of {POPULATION_FLOOR:,} people or more; zero means none above that "
            "floor, not none at all."
        ),
    }[child_level]
    source = {
        "country": "Natural Earth 1:50m",
        "state": "Natural Earth 1:50m ADM1",
        "county": "geoBoundaries",
        "city": "GeoNames cities15000",
    }[child_level]
    return f"{plural.capitalize()} within", plural, shared + caveat, source


def import_subregions(level: str = "country") -> None:
    """Count every populated lower tier inside each region at `level`.

    Re-runnable and idempotent, like every other metric job. Cheap enough to
    re-run after any region import, which is when it needs re-running.
    """
    with db.connect() as conn:
        child_levels = _populated_child_levels(conn, level)
        if not child_levels:
            log.warning(
                "no populated levels below %s, so there is nothing to count there",
                level,
            )
            return

        names = [metric_name(child) for child in child_levels]
        for child, name in zip(child_levels, names, strict=True):
            label, unit, description, source = _describe(child)
            db.register_metric(
                conn,
                metric_name=name,
                label=label,
                unit=unit,
                description=description,
                source=source,
            )

        log.info("counting %s within each %s", ", ".join(child_levels), level)
        conn.execute(
            _COUNT_SQL,
            {
                "level": level,
                "year": VINTAGE_YEAR,
                "child_levels": child_levels,
                "metric_names": names,
            },
        )
        availability.refresh(conn)

        summary = conn.execute(
            """
            SELECT m.metric_name, r.name, m.value
            FROM metrics m
            JOIN regions r ON r.id = m.region_id
            JOIN LATERAL (
                SELECT max(m2.value) AS top
                FROM metrics m2
                JOIN regions r2 ON r2.id = m2.region_id
                WHERE m2.metric_name = m.metric_name AND r2.level = %(level)s
            ) best ON m.value = best.top
            WHERE m.metric_name = ANY(%(names)s) AND r.level = %(level)s
            ORDER BY m.metric_name, r.name
            """,
            {"level": level, "names": names},
        ).fetchall()
    log.info(
        "most per %s: %s",
        level,
        ", ".join(f"{r['name']} {r['value']:,.0f} ({r['metric_name']})" for r in summary),
    )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_subregions()
