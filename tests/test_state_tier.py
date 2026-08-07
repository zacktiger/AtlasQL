"""The phase 2 acceptance check: a second tier, same engine.

The plan calls phase 2 done when the filter engine works unmodified against a
new tier. These tests deliberately exercise the state tier through the same
`query.run` the country tier uses, with no state-specific code path, and check
that the tier's real coverage gap - no GDP below country level - produces a
named refusal rather than a quietly wrong answer.
"""

from __future__ import annotations

import pytest

from atlasql import query
from atlasql.models import Condition, GeoFilter

ELEVATION = "elevation_mean"
RIVERS = "major_river_length_km"
GDP = "gdp_per_capita"


@pytest.fixture(scope="module")
def states(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM regions WHERE level = 'state'"
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("state tier not imported; run import-states")
    return schema


@pytest.fixture(scope="module")
def states_with_metrics(states):
    n = states.execute(
        """
        SELECT count(*) AS n FROM metrics m
        JOIN regions r ON r.id = m.region_id
        WHERE r.level = 'state' AND m.metric_name = %s
        """,
        (ELEVATION,),
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("state metrics not computed; run import-elevation --level state")
    return states


def test_every_state_hangs_off_a_country(states):
    bad = states.execute(
        """
        SELECT r.name FROM regions r
        LEFT JOIN regions p ON p.id = r.parent_id
        WHERE r.level = 'state' AND (p.id IS NULL OR p.level <> 'country')
        """
    ).fetchall()
    assert not bad, f"states not under a country: {[r['name'] for r in bad][:5]}"


def test_state_geometries_are_valid(states):
    row = states.execute(
        """
        SELECT count(*) FILTER (WHERE NOT ST_IsValid(geom))  AS invalid,
               count(*) FILTER (WHERE geom IS NULL)          AS missing
        FROM regions WHERE level = 'state'
        """
    ).fetchone()
    assert row == {"invalid": 0, "missing": 0}


def test_the_same_engine_answers_a_state_query(states_with_metrics):
    """No state-specific code path: the identical call, a different level."""
    result = query.run(
        GeoFilter(
            level="state",
            conditions=[Condition(metric=ELEVATION, op=">", value=1000)],
            top_n=10,
        ),
        states_with_metrics,
    )
    assert result.level == "state"
    assert result.count > 0
    assert all(row.metrics[ELEVATION].value > 1000 for row in result.results)
    # Ranking still holds at the new tier.
    values = [row.metrics[ELEVATION].value for row in result.results]
    assert values == sorted(values, reverse=True)


def test_two_condition_state_query_matches_an_independent_computation(
    states_with_metrics,
):
    elevation_min, river_min, top_n = 500, 50000, 10
    result = query.run(
        GeoFilter(
            level="state",
            conditions=[
                Condition(metric=ELEVATION, op=">", value=elevation_min),
                Condition(metric=RIVERS, op=">", value=river_min),
            ],
            sort_by=ELEVATION,
            top_n=top_n,
        ),
        states_with_metrics,
    )
    expected = states_with_metrics.execute(
        """
        SELECT r.name
        FROM regions r
        JOIN metrics e ON e.region_id = r.id AND e.metric_name = %(elevation)s
        JOIN metrics v ON v.region_id = r.id AND v.metric_name = %(rivers)s
        WHERE r.level = 'state' AND e.value > %(elevation_min)s AND v.value > %(river_min)s
        ORDER BY e.value DESC, r.name ASC
        LIMIT %(top_n)s
        """,
        {
            "elevation": ELEVATION,
            "rivers": RIVERS,
            "elevation_min": elevation_min,
            "river_min": river_min,
            "top_n": top_n,
        },
    ).fetchall()
    assert [r.name for r in result.results] == [r["name"] for r in expected]


def test_gdp_at_state_level_is_refused_by_name(states_with_metrics):
    """The tier's real gap: no subnational GDP source exists, so say so."""
    with pytest.raises(query.NoLevelWithCoverageError) as excinfo:
        query.run(
            GeoFilter(
                level="state",
                conditions=[Condition(metric=GDP, op=">", value=40000)],
            ),
            states_with_metrics,
        )
    assert excinfo.value.blocking_metric == GDP
    assert excinfo.value.as_dict()["level"] == "state"


def test_auto_prefers_the_more_granular_tier_when_both_have_coverage(
    states_with_metrics,
):
    """Auto lands on the deepest level that can answer every metric.

    River length exists at state and country level but not for cities, which
    are points and contain no rivers, so this pair bottoms out at state even
    though elevation alone would now reach the city tier.
    """
    result = query.run(
        GeoFilter(
            conditions=[
                Condition(metric=ELEVATION, op=">", value=2000),
                Condition(metric=RIVERS, op=">", value=1000),
            ],
            top_n=5,
        ),
        states_with_metrics,
    )
    assert result.level == "state"
    assert result.level_chosen_by == "auto"


def test_mixing_a_country_only_metric_with_a_state_metric_falls_back_to_country(
    states_with_metrics,
):
    """Auto must pick the deepest level where *every* metric has coverage."""
    result = query.run(
        GeoFilter(
            conditions=[
                Condition(metric=GDP, op=">", value=20000),
                Condition(metric=ELEVATION, op=">", value=200),
            ],
            top_n=5,
        ),
        states_with_metrics,
    )
    assert result.level == "country"
