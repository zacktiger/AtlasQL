"""Coastline length, derived from the boundaries already loaded.

The metric is a subtraction — boundary minus shared borders — so every test
here is really asking the same question from a different side: is the
subtraction finding *real* shared borders, or approximately-shared ones that
leave slivers behind? Three independent checks say yes, and each would fail
loudly under a tolerance problem:

  * landlocked regions come out at exactly zero, not nearly zero;
  * an island nation's coast equals its entire boundary, to the metre;
  * a country's coast equals the sum of its states', so the same coastline is
    partitioned between tiers rather than double counted or dropped.
"""

from __future__ import annotations

import pytest

from atlasql import query
from atlasql.etl.coastline import METRIC, _is_below_country
from atlasql.models import Condition, GeoFilter

# Landlocked by geography, not by treaty: no sea boundary of any kind.
LANDLOCKED = ["Switzerland", "Nepal", "Bolivia", "Austria", "Mongolia", "Chad"]

# Islands with no land neighbour at all. Cuba is deliberately absent: Natural
# Earth carries Guantanamo Bay as a separate entity, so Cuba has a land border
# and its coast is legitimately 25 km short of its boundary.
ISLAND_NATIONS = ["Japan", "Iceland", "Madagascar", "Australia", "New Zealand", "Sri Lanka"]


@pytest.fixture(scope="module")
def coasts(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM metrics WHERE metric_name = %s", (METRIC,)
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("coastline not computed; run import-coastline --level country")
    return schema


def test_is_below_country_gates_the_country_subtraction():
    """Pure function, no database. Getting this wrong reported every continent
    as landlocked, because a continent is *made of* countries rather than
    bordered by them."""
    assert not _is_below_country("continent")
    assert not _is_below_country("country")
    assert _is_below_country("state")
    assert _is_below_country("county")
    with pytest.raises(ValueError):
        _is_below_country("planet")


def test_landlocked_countries_are_exactly_zero(coasts):
    """Exactly, not approximately. A sliver left by a border that did not quite
    cancel would show up here as a few stray kilometres."""
    rows = coasts.execute(
        """
        SELECT r.name, m.value FROM regions r
        JOIN metrics m ON m.region_id = r.id AND m.metric_name = %s
        WHERE r.level = 'country' AND r.name = ANY(%s)
        """,
        (METRIC, LANDLOCKED),
    ).fetchall()
    assert len(rows) == len(LANDLOCKED)
    assert all(row["value"] == 0 for row in rows), [dict(r) for r in rows]


def test_an_island_nations_coast_is_its_whole_boundary(coasts):
    """With no neighbour to subtract, nothing may be subtracted."""
    rows = coasts.execute(
        """
        SELECT r.name, m.value AS coast,
               ST_Length(ST_Boundary(r.geom)::geography) / 1000.0 AS boundary
        FROM regions r
        JOIN metrics m ON m.region_id = r.id AND m.metric_name = %s
        WHERE r.level = 'country' AND r.name = ANY(%s)
        """,
        (METRIC, ISLAND_NATIONS),
    ).fetchall()
    assert len(rows) == len(ISLAND_NATIONS)
    for row in rows:
        assert row["coast"] == pytest.approx(row["boundary"], abs=0.001), dict(row)


def test_a_countrys_coast_is_partitioned_among_its_states(coasts):
    """The same coastline measured at two tiers has to come to the same length.

    This is the check that the state tier subtracts internal borders correctly:
    if a state kept the border with its neighbouring state, the sum would run
    well over the country's own figure.
    """
    if not coasts.execute(
        "SELECT 1 FROM metrics m JOIN regions r ON r.id = m.region_id "
        "WHERE m.metric_name = %s AND r.level = 'state' LIMIT 1",
        (METRIC,),
    ).fetchone():
        pytest.skip("state coastline not computed; run import-coastline --level state")

    row = coasts.execute(
        """
        SELECT sum(country_coast) AS countries, sum(state_coast) AS states
        FROM (
            SELECT mc.value AS country_coast, sum(ms.value) AS state_coast
            FROM regions p
            JOIN metrics mc ON mc.region_id = p.id AND mc.metric_name = %(metric)s
            JOIN regions s ON s.parent_id = p.id AND s.level = 'state'
            JOIN metrics ms ON ms.region_id = s.id AND ms.metric_name = %(metric)s
            WHERE p.level = 'country' AND mc.value > 100
            GROUP BY p.id, mc.value
        ) AS per_country
        """,
        {"metric": METRIC},
    ).fetchone()
    assert row["states"] == pytest.approx(row["countries"], rel=0.01)


def test_counties_measure_more_coast_than_the_states_holding_them(coasts):
    """The one place this metric is not comparable across tiers, asserted so it
    cannot invert unnoticed.

    Counties come from geoBoundaries and everything above them from Natural
    Earth 1:50m. Finer linework measures more coast, so a state's counties
    total well above the state itself — about 36% — where a country's states
    match it to within 0.1% because both are the same source. The number is not
    the point; the direction and the rough size are, because a county tier that
    came back *shorter* than its states would mean the subtraction was eating
    real coastline rather than the ruler having changed.
    """
    if not coasts.execute(
        "SELECT 1 FROM metrics m JOIN regions r ON r.id = m.region_id "
        "WHERE m.metric_name = %s AND r.level = 'county' LIMIT 1",
        (METRIC,),
    ).fetchone():
        pytest.skip("county coastline not computed; run import-coastline --level county")

    row = coasts.execute(
        """
        SELECT sum(state_km) AS states, sum(county_km) AS counties
        FROM (
            SELECT ms.value AS state_km, sum(mc.value) AS county_km
            FROM regions s
            JOIN metrics ms ON ms.region_id = s.id AND ms.metric_name = %(metric)s
            JOIN regions ct ON ct.parent_id = s.id AND ct.level = 'county'
            JOIN metrics mc ON mc.region_id = ct.id AND mc.metric_name = %(metric)s
            WHERE s.level = 'state'
            GROUP BY s.id, ms.value
        ) AS per_state
        """,
        {"metric": METRIC},
    ).fetchone()
    ratio = row["counties"] / row["states"]
    assert 1.1 < ratio < 1.8, f"county/state coastline ratio {ratio:.3f}"


def test_continents_are_not_all_landlocked(coasts):
    """The regression this metric shipped with a bug for.

    A continent's parent is NULL, so the "neighbouring countries" term matched
    every country on it — all of which it intersects, being made of them — and
    subtracted the entire landmass. Every continent came back at zero.
    """
    if not coasts.execute(
        "SELECT 1 FROM metrics m JOIN regions r ON r.id = m.region_id "
        "WHERE m.metric_name = %s AND r.level = 'continent' LIMIT 1",
        (METRIC,),
    ).fetchone():
        pytest.skip("continent coastline not computed; run import-coastline --level continent")

    rows = coasts.execute(
        """
        SELECT r.name, m.value FROM regions r
        JOIN metrics m ON m.region_id = r.id AND m.metric_name = %s
        WHERE r.level = 'continent'
        """,
        (METRIC,),
    ).fetchall()
    assert all(row["value"] > 0 for row in rows), [dict(r) for r in rows]


def test_a_continents_coast_exceeds_any_of_its_countries(coasts):
    """A sanity check with real content: dissolving countries into a continent
    removes the borders between them but none of the coast, so the continent is
    never shorter than any country on it.

    Never *shorter*, not always longer: Antarctica the continent holds exactly
    one country, Antarctica, and the two are the same outline to within the last
    bits of a float. Hence the relative slack rather than a strict comparison.
    """
    if not coasts.execute(
        "SELECT 1 FROM metrics m JOIN regions r ON r.id = m.region_id "
        "WHERE m.metric_name = %s AND r.level = 'continent' LIMIT 1",
        (METRIC,),
    ).fetchone():
        pytest.skip("continent coastline not computed")

    bad = coasts.execute(
        """
        SELECT cont.name AS continent, ctry.name AS country,
               mcont.value AS continent_km, mctry.value AS country_km
        FROM regions ctry
        JOIN regions cont ON cont.id = ctry.parent_id
        JOIN metrics mctry ON mctry.region_id = ctry.id AND mctry.metric_name = %(metric)s
        JOIN metrics mcont ON mcont.region_id = cont.id AND mcont.metric_name = %(metric)s
        WHERE ctry.level = 'country' AND mctry.value > mcont.value * (1 + 1e-9)
        """,
        {"metric": METRIC},
    ).fetchall()
    assert not bad, [dict(row) for row in bad[:5]]


def test_cities_have_no_coastline(coasts):
    """Points have no outline, so the metric is absent there rather than zero —
    and a city query naming it is refused by name."""
    with pytest.raises(query.NoLevelWithCoverageError) as excinfo:
        query.run(
            GeoFilter(level="city", conditions=[Condition(metric=METRIC, op=">", value=0)]),
            coasts,
        )
    assert excinfo.value.blocking_metric == METRIC


def test_the_same_engine_ranks_countries_by_coastline(coasts):
    result = query.run(
        GeoFilter(
            level="country",
            conditions=[Condition(metric=METRIC, op=">", value=1000)],
            top_n=5,
        ),
        coasts,
    )
    assert result.count == 5
    assert result.results[0].name == "Canada"
    values = [row.metrics[METRIC].value for row in result.results]
    assert values == sorted(values, reverse=True)


def test_zero_coastline_is_queryable_as_landlocked(coasts):
    """The point of storing zero rather than leaving the row out."""
    result = query.run(
        GeoFilter(
            level="country",
            conditions=[Condition(metric=METRIC, op="==", value=0)],
            top_n=100,
        ),
        coasts,
    )
    names = {row.name for row in result.results}
    assert {"Switzerland", "Nepal", "Mongolia"} <= names
