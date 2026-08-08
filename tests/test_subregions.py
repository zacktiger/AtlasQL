"""The subregion count metrics: how many lower-tier regions a region contains.

These read what `import-subregions` wrote rather than running it, in the style
of the other tier tests, and skip when it has not been run. What they are
actually guarding is the pair of decisions the metric turns on: that counting
follows the hierarchy all the way down rather than one level, and that a tier
with no regions gets no metric rather than a column of honest-looking zeroes.
"""

from __future__ import annotations

import pytest

from atlasql import query
from atlasql.etl.subregions import metric_name
from atlasql.models import Condition, GeoFilter

STATES = metric_name("state")
CITIES = metric_name("city")
COUNTRIES = metric_name("country")


@pytest.fixture(scope="module")
def counted(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM metrics WHERE metric_name = %s", (STATES,)
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("subregion counts not computed; run import-subregions --level country")
    return schema


def test_state_count_matches_an_independent_computation(counted):
    """Direct children, computed without the recursive descent the job uses."""
    mismatched = counted.execute(
        """
        SELECT r.name, m.value,
               (SELECT count(*) FROM regions s
                WHERE s.parent_id = r.id AND s.level = 'state') AS direct
        FROM regions r
        JOIN metrics m ON m.region_id = r.id AND m.metric_name = %s
        WHERE r.level = 'country'
          AND m.value <> (SELECT count(*) FROM regions s
                          WHERE s.parent_id = r.id AND s.level = 'state')
        """,
        (STATES,),
    ).fetchall()
    assert not mismatched, [dict(row) for row in mismatched[:5]]


def test_cities_are_counted_through_states_not_just_direct_children(counted):
    """The whole reason descent is recursive.

    Every city hangs off a state, so a country's direct children include no
    cities at all. A one-level count would report zero for every country on
    earth and still look like a working metric.
    """
    row = counted.execute(
        """
        SELECT count(*) FILTER (WHERE m.value > 0) AS with_cities,
               sum(m.value)                        AS total
        FROM metrics m
        JOIN regions r ON r.id = m.region_id
        WHERE m.metric_name = %s AND r.level = 'country'
        """,
        (CITIES,),
    ).fetchone()
    assert row["with_cities"] > 100

    # No city may be counted twice or dropped: summing over the countries has
    # to come back to the cities that have a country above them.
    reachable = counted.execute(
        """
        SELECT count(*) AS n
        FROM regions c
        JOIN regions s ON s.id = c.parent_id
        WHERE c.level = 'city' AND s.parent_id IS NOT NULL
        """
    ).fetchone()["n"]
    assert row["total"] == reachable


def test_a_region_with_no_subdivisions_counts_zero_rather_than_nothing(counted):
    """Zero is a measurement here, so coverage is total and no country is
    silently dropped from a query that mentions the metric."""
    row = counted.execute(
        """
        SELECT count(*) FILTER (WHERE m.region_id IS NULL) AS missing,
               count(*) FILTER (WHERE m.value = 0)         AS zeroes
        FROM regions r
        LEFT JOIN metrics m ON m.region_id = r.id AND m.metric_name = %s
        WHERE r.level = 'country'
        """,
        (STATES,),
    ).fetchone()
    assert row["missing"] == 0
    # Natural Earth has no ADM1 for a handful of banks, shoals and Bir Tawil.
    assert row["zeroes"] > 0


def test_a_tier_that_exists_but_is_not_below_gets_no_metric(counted):
    """Being lower in config.LEVELS is not the same as hanging underneath.

    Cities are parented to states, so no city is a descendant of a county. The
    first version of this job asked only "does the city level hold regions",
    which is true, and would have written city_count = 0 onto all 49,015
    counties and reported it at 100% coverage. Reachability by descent is the
    question that gives the honest answer.
    """
    if not counted.execute(
        "SELECT 1 FROM regions WHERE level = 'county' LIMIT 1"
    ).fetchone():
        pytest.skip("county tier not imported; run import-counties")

    written = counted.execute(
        """
        SELECT count(*) AS n FROM metrics m
        JOIN regions r ON r.id = m.region_id
        WHERE r.level = 'county' AND m.metric_name = %s
        """,
        (CITIES,),
    ).fetchone()["n"]
    assert written == 0, f"{written} counties carry a city count they cannot have"


def test_counties_are_counted_where_they_do_hang_below(counted):
    """The other half: counties are genuine descendants of states and countries,
    so county_count belongs there and nowhere else."""
    if not counted.execute(
        "SELECT 1 FROM regions WHERE level = 'county' LIMIT 1"
    ).fetchone():
        pytest.skip("county tier not imported")

    rows = counted.execute(
        """
        SELECT r.level, count(*) AS n
        FROM metrics m JOIN regions r ON r.id = m.region_id
        WHERE m.metric_name = %s
        GROUP BY r.level
        """,
        (metric_name("county"),),
    ).fetchall()
    levels = {row["level"] for row in rows}
    assert levels <= {"continent", "country", "state"}
    assert {"country", "state"} <= levels


def test_an_unpopulated_tier_gets_no_metric_at_all(counted):
    """The other half of the zero decision, and the important one.

    The county tier is imported now, so the metric it would have blocked does
    exist — but the rule it tested still holds and is what the city/county case
    above exercises. What remains checkable here is that every registered count
    metric has values somewhere: a name in the registry with no rows behind it
    is a metric the UI offers and no query can answer.
    """
    registry = query.registered_metrics(counted)
    counts = [name for name in registry if name.endswith("_count")]
    for name in counts:
        rows = counted.execute(
            "SELECT count(*) AS n FROM metrics WHERE metric_name = %s", (name,)
        ).fetchone()["n"]
        assert rows > 0, f"{name} is registered but holds no values"


def test_each_count_carries_its_own_unit(counted):
    """Why these are three metrics rather than one subregion_count: a single
    name would need a unit that changes with the level, so a threshold would
    stop meaning one thing."""
    rows = counted.execute(
        "SELECT metric_name, unit FROM metric_definitions WHERE metric_name = ANY(%s)",
        ([STATES, CITIES, COUNTRIES],),
    ).fetchall()
    units = {row["metric_name"]: row["unit"] for row in rows}
    assert units[STATES] == "states"
    assert units[CITIES] == "cities"


def test_the_same_engine_answers_a_subregion_query(counted):
    """No new code path: the ordinary call, a metric derived from the hierarchy."""
    result = query.run(
        GeoFilter(
            level="country",
            conditions=[
                Condition(metric=STATES, op=">", value=20),
                Condition(metric=CITIES, op=">", value=200),
            ],
            sort_by=STATES,
            top_n=10,
        ),
        counted,
    )
    assert result.count > 0
    assert all(row.metrics[STATES].value > 20 for row in result.results)
    assert all(row.metrics[CITIES].value > 200 for row in result.results)
    values = [row.metrics[STATES].value for row in result.results]
    assert values == sorted(values, reverse=True)


def test_auto_lands_on_the_deepest_level_the_counted_tier_leaves_room_for(counted):
    """city_count reaches the state tier; state_count bottoms out at country,
    since a state contains no states."""
    cities = query.run(GeoFilter(conditions=[Condition(metric=CITIES, op=">", value=100)]), counted)
    assert (cities.level, cities.level_chosen_by) == ("state", "auto")

    states = query.run(GeoFilter(conditions=[Condition(metric=STATES, op=">", value=10)]), counted)
    assert (states.level, states.level_chosen_by) == ("country", "auto")


def test_country_count_is_refused_below_the_continent_by_name(counted):
    """A country contains no countries, and saying so beats an empty table."""
    if not counted.execute(
        "SELECT 1 FROM metrics WHERE metric_name = %s LIMIT 1", (COUNTRIES,)
    ).fetchone():
        pytest.skip("continent counts not computed; run import-subregions --level continent")

    with pytest.raises(query.NoLevelWithCoverageError) as excinfo:
        query.run(
            GeoFilter(level="country", conditions=[Condition(metric=COUNTRIES, op=">", value=5)]),
            counted,
        )
    assert excinfo.value.blocking_metric == COUNTRIES
    assert excinfo.value.as_dict()["available_levels"] == ["continent"]


def test_the_continents_partition_the_countries(counted):
    """The counts have to add back up to the tier they came from, or the
    hierarchy has a region hanging off nothing."""
    if not counted.execute(
        "SELECT 1 FROM metrics WHERE metric_name = %s LIMIT 1", (COUNTRIES,)
    ).fetchone():
        pytest.skip("continent counts not computed; run import-subregions --level continent")

    total = counted.execute(
        "SELECT sum(m.value) AS n FROM metrics m JOIN regions r ON r.id = m.region_id "
        "WHERE m.metric_name = %s AND r.level = 'continent'",
        (COUNTRIES,),
    ).fetchone()["n"]
    parented = counted.execute(
        "SELECT count(*) AS n FROM regions WHERE level = 'country' AND parent_id IS NOT NULL"
    ).fetchone()["n"]
    assert total == parented
