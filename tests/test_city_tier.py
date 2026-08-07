"""The phase 3 acceptance checks: a point tier, and honest refusals.

The plan calls phase 3 done when city queries reject GDP-based filters with a
clear message instead of failing silently. Cities are the first level that
genuinely cannot answer most of the metric registry - no polygon means no zonal
statistics and no river intersection, and GDP has no source below the country -
so this is where the coverage machinery either earns its keep or does not.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atlasql import query
from atlasql.api import app
from atlasql.models import Condition, GeoFilter

ELEVATION = "elevation_mean"
ELEVATION_MAX = "elevation_max"
POPULATION = "population"
RIVERS = "major_river_length_km"
GDP = "gdp_per_capita"


@pytest.fixture(scope="module")
def cities(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM regions WHERE level = 'city'"
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("city tier not imported; run import-cities")
    return schema


@pytest.fixture(scope="module")
def cities_with_elevation(cities):
    n = cities.execute(
        """
        SELECT count(*) AS n FROM metrics m
        JOIN regions r ON r.id = m.region_id
        WHERE r.level = 'city' AND m.metric_name = %s
        """,
        (ELEVATION,),
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("city elevation not computed; run import-elevation --level city")
    return cities


def test_cities_are_points_with_a_location_and_no_polygon(cities):
    row = cities.execute(
        """
        SELECT count(*) FILTER (WHERE geom IS NOT NULL)     AS with_polygon,
               count(*) FILTER (WHERE centroid IS NULL)     AS without_point,
               count(*)                                     AS total
        FROM regions WHERE level = 'city'
        """
    ).fetchone()
    assert row["with_polygon"] == 0
    assert row["without_point"] == 0
    assert 20_000 < row["total"] < 60_000


def test_cities_hang_off_a_state_or_a_country(cities):
    rows = cities.execute(
        """
        SELECT p.level AS parent_level, count(*) AS n
        FROM regions r
        LEFT JOIN regions p ON p.id = r.parent_id
        WHERE r.level = 'city'
        GROUP BY p.level
        """
    ).fetchall()
    counts = {row["parent_level"]: row["n"] for row in rows}
    assert set(counts) <= {"state", "country", None}
    # A handful of far-offshore islands legitimately sit inside no polygon and
    # beyond the snapping tolerance; they are logged, not silently reparented.
    assert counts.get(None, 0) < 20
    assert counts.get("state", 0) > 30_000


def test_gdp_at_city_level_is_refused_by_name(cities_with_elevation):
    """The phase 3 acceptance case, stated in the plan."""
    with pytest.raises(query.NoLevelWithCoverageError) as excinfo:
        query.run(
            GeoFilter(
                level="city",
                conditions=[Condition(metric=GDP, op=">", value=40000)],
            ),
            cities_with_elevation,
        )
    error = excinfo.value
    assert error.blocking_metric == GDP
    assert error.as_dict()["level"] == "city"
    assert error.as_dict()["coverage_pct"] == 0


def test_gdp_at_city_level_is_a_422_through_the_api(cities_with_elevation):
    client = TestClient(app)
    response = client.post(
        "/query",
        json={
            "level": "city",
            "conditions": [{"metric": GDP, "op": ">", "value": 40000}],
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["blocking_metric"] == GDP
    assert "city" in body["error"]


def test_river_metrics_are_refused_at_city_level(cities_with_elevation):
    """A point contains no rivers, and that is a fact, not an empty result."""
    with pytest.raises(query.NoLevelWithCoverageError) as excinfo:
        query.run(
            GeoFilter(
                level="city",
                conditions=[Condition(metric=RIVERS, op=">", value=100)],
            ),
            cities_with_elevation,
        )
    assert excinfo.value.blocking_metric == RIVERS


def test_elevation_range_is_refused_at_city_level(cities_with_elevation):
    """A point has no minimum or maximum, so those metrics are absent here."""
    with pytest.raises(query.NoLevelWithCoverageError) as excinfo:
        query.run(
            GeoFilter(
                level="city",
                conditions=[Condition(metric=ELEVATION_MAX, op=">", value=1000)],
            ),
            cities_with_elevation,
        )
    assert excinfo.value.blocking_metric == ELEVATION_MAX


def test_high_altitude_cities_query_matches_an_independent_computation(
    cities_with_elevation,
):
    """The query both design docs open with: cities above 2,000 m over 500k."""
    elevation_min, population_min, top_n = 2000, 500_000, 10

    result = query.run(
        GeoFilter(
            conditions=[
                Condition(metric=ELEVATION, op=">", value=elevation_min),
                Condition(metric=POPULATION, op=">", value=population_min),
            ],
            sort_by=ELEVATION,
            top_n=top_n,
        ),
        cities_with_elevation,
    )

    assert result.level == "city"
    assert result.level_chosen_by == "auto"

    expected = cities_with_elevation.execute(
        """
        SELECT r.name
        FROM regions r
        JOIN metrics e ON e.region_id = r.id AND e.metric_name = %(elevation)s
        JOIN metrics p ON p.region_id = r.id AND p.metric_name = %(population)s
        WHERE r.level = 'city' AND e.value > %(elevation_min)s
          AND p.value > %(population_min)s
        ORDER BY e.value DESC, r.name ASC
        LIMIT %(top_n)s
        """,
        {
            "elevation": ELEVATION,
            "population": POPULATION,
            "elevation_min": elevation_min,
            "population_min": population_min,
            "top_n": top_n,
        },
    ).fetchall()
    assert [r.name for r in result.results] == [r["name"] for r in expected]


def test_population_is_a_multi_tier_metric(cities_with_elevation):
    """Alone it reaches cities; with GDP it falls back to the country tier."""
    city_level = query.run(
        GeoFilter(conditions=[Condition(metric=POPULATION, op=">", value=1_000_000)]),
        cities_with_elevation,
    )
    assert city_level.level == "city"

    country_level = query.run(
        GeoFilter(
            conditions=[
                Condition(metric=POPULATION, op=">", value=1_000_000),
                Condition(metric=GDP, op=">", value=20_000),
            ]
        ),
        cities_with_elevation,
    )
    assert country_level.level == "country"


def test_auto_now_reaches_cities_for_an_elevation_only_query(cities_with_elevation):
    """Deeper tier wins when it has the data: elevation alone resolves to city."""
    result = query.run(
        GeoFilter(conditions=[Condition(metric=ELEVATION, op=">", value=3000)], top_n=5),
        cities_with_elevation,
    )
    assert result.level == "city"
    assert result.level_chosen_by == "auto"
