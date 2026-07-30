"""The phase 1 acceptance check.

The build plan calls phase 1 done when a two-condition country query returns a
correct ranked top-N. "Correct" here means checked against the stored data
independently of the engine: the same answer is recomputed with a
straightforward query and the two must agree, so a bug in the join or the
ranking cannot pass by agreeing with itself.
"""

from __future__ import annotations

import pytest

from atlasql import query
from atlasql.models import Condition, GeoFilter

GDP = "gdp_per_capita"
ELEVATION = "elevation_mean"


@pytest.fixture(scope="module")
def loaded(schema):
    missing = [
        metric
        for metric in (GDP, ELEVATION)
        if not schema.execute(
            "SELECT 1 FROM metrics WHERE metric_name = %s LIMIT 1", (metric,)
        ).fetchone()
    ]
    if missing:
        pytest.skip(f"metrics not imported: {', '.join(missing)}")
    return schema


def test_two_condition_country_query_matches_an_independent_computation(loaded):
    gdp_min, elevation_min, top_n = 40000, 500, 10

    result = query.run(
        GeoFilter(
            conditions=[
                Condition(metric=GDP, op=">", value=gdp_min),
                Condition(metric=ELEVATION, op=">", value=elevation_min),
            ],
            sort_by=ELEVATION,
            order="desc",
            top_n=top_n,
        ),
        loaded,
    )

    expected = loaded.execute(
        """
        SELECT r.name, g.value AS gdp, e.value AS elevation
        FROM regions r
        JOIN metrics g ON g.region_id = r.id AND g.metric_name = %(gdp)s
        JOIN metrics e ON e.region_id = r.id AND e.metric_name = %(elevation)s
        WHERE r.level = 'country'
          AND g.value > %(gdp_min)s
          AND e.value > %(elevation_min)s
        ORDER BY e.value DESC, r.name ASC
        LIMIT %(top_n)s
        """,
        {
            "gdp": GDP,
            "elevation": ELEVATION,
            "gdp_min": gdp_min,
            "elevation_min": elevation_min,
            "top_n": top_n,
        },
    ).fetchall()

    assert [row.name for row in result.results] == [row["name"] for row in expected]
    assert result.level == "country"
    assert result.level_chosen_by == "auto"

    for got, want in zip(result.results, expected, strict=True):
        assert got.metrics[GDP].value == pytest.approx(want["gdp"])
        assert got.metrics[ELEVATION].value == pytest.approx(want["elevation"])
        assert got.metrics[GDP].value > gdp_min
        assert got.metrics[ELEVATION].value > elevation_min


def test_ranking_is_stable_and_descending(loaded):
    result = query.run(
        GeoFilter(
            conditions=[
                Condition(metric=GDP, op=">", value=10000),
                Condition(metric=ELEVATION, op=">", value=100),
            ],
            sort_by=ELEVATION,
            top_n=25,
        ),
        loaded,
    )
    elevations = [row.metrics[ELEVATION].value for row in result.results]
    assert elevations == sorted(elevations, reverse=True)
    assert len({row.region_id for row in result.results}) == len(result.results)
