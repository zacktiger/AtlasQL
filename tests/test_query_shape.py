"""The SQL shapes chosen for speed must not change the answer.

`build_sql` emits one of two statements depending on how many regions the level
has: above `PREFILTER_MIN_REGIONS` each condition also gets an EXISTS
pre-filter, so the planner can start from the metric index. That is a plan
choice, not a semantic one, and these tests are what keeps it that way — a
pre-filter written with the wrong operator, or one applied to `sort_by` (which
carries no threshold to test), would silently drop rows.
"""

from __future__ import annotations

import pytest

from atlasql import config, query
from atlasql.models import Condition, GeoFilter


@pytest.fixture(scope="module")
def loaded(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM metrics WHERE metric_name = 'gdp_per_capita'"
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("GDP not imported; run import-world-bank")
    return schema


def _rows(conn, geo_filter, level, region_count):
    statement, params = query.build_sql(geo_filter, level, region_count)
    return conn.execute(statement, params).fetchall()


def _comparable(rows):
    """Row identity plus every metric value, so a dropped column is caught too."""
    return [
        (row["id"], row["parent_name"], tuple(sorted(
            (k, v) for k, v in row.items() if k.startswith("m")
        )))
        for row in rows
    ]


FILTERS = [
    pytest.param(
        GeoFilter(
            level="country",
            conditions=[Condition(metric="gdp_per_capita", op=">", value=20000)],
            top_n=25,
        ),
        id="single-condition",
    ),
    pytest.param(
        GeoFilter(
            level="country",
            conditions=[
                Condition(metric="gdp_per_capita", op=">=", value=5000),
                Condition(metric="elevation_mean", op="<", value=1000),
            ],
            top_n=40,
        ),
        id="two-conditions-mixed-operators",
    ),
    pytest.param(
        GeoFilter(
            level="country",
            conditions=[Condition(metric="gdp_per_capita", op=">", value=1000)],
            # sort_by adds a metric with no threshold of its own; it must not
            # acquire a pre-filter, which would need an operator it does not have.
            sort_by="elevation_mean",
            order="asc",
            top_n=30,
        ),
        id="sort-by-outside-the-conditions",
    ),
    pytest.param(
        GeoFilter(
            level="country",
            conditions=[Condition(metric="gdp_per_capita", op="<=", value=3000)],
            order="asc",
            top_n=20,
        ),
        id="ascending-with-le",
    ),
]


@pytest.mark.parametrize("geo_filter", FILTERS)
def test_the_prefilter_never_changes_the_result(loaded, geo_filter):
    """Both shapes, same level, same rows.

    The region count is forced rather than read, so this compares the two
    statements against each other on identical data instead of depending on
    which tier happens to be big in the loaded database.
    """
    without = _rows(loaded, geo_filter, "country", region_count=0)
    with_prefilter = _rows(
        loaded, geo_filter, "country", region_count=config.PREFILTER_MIN_REGIONS
    )
    assert without, "fixture query matched nothing, so this proves nothing"
    assert _comparable(with_prefilter) == _comparable(without)


def test_the_prefilter_is_only_emitted_above_the_threshold(loaded):
    geo_filter = FILTERS[1].values[0]
    small, _ = query.build_sql(geo_filter, "country", config.PREFILTER_MIN_REGIONS - 1)
    large, _ = query.build_sql(geo_filter, "country", config.PREFILTER_MIN_REGIONS)
    assert "EXISTS" not in small.as_string(loaded)
    assert large.as_string(loaded).count("EXISTS") == len(geo_filter.conditions)


def test_the_prefilter_keeps_values_as_bound_parameters(loaded):
    """The pre-filter repeats the condition, so it is a second chance to inline
    a caller-supplied value into the SQL text. It must not take it."""
    geo_filter = GeoFilter(
        level="country",
        conditions=[Condition(metric="gdp_per_capita", op=">", value=40000)],
    )
    statement, params = query.build_sql(
        geo_filter, "country", config.PREFILTER_MIN_REGIONS
    )
    rendered = statement.as_string(loaded)
    assert "40000" not in rendered
    assert "gdp_per_capita" not in rendered
    # One placeholder for the real test, one for the pre-filter, both bound.
    assert list(params.values()).count(40000) == 1


def test_parent_name_survives_being_joined_after_the_limit(loaded):
    """The parent join moved outside the LIMIT; it must still populate."""
    result = query.run(
        GeoFilter(
            level="state",
            conditions=[Condition(metric="elevation_mean", op=">", value=100)],
            top_n=5,
        ),
        loaded,
    )
    if result.count == 0:
        pytest.skip("state tier not imported")
    assert all(row.parent_name for row in result.results)


def test_region_count_is_cached_but_reports_the_truth(loaded):
    query._forget_level_counts()
    first = query.region_count(loaded, "country")
    actual = loaded.execute(
        "SELECT count(*) AS n FROM regions WHERE level = 'country'"
    ).fetchone()["n"]
    assert first == actual
    # Second call is served from the cache and must agree with the first.
    assert query.region_count(loaded, "country") == actual
