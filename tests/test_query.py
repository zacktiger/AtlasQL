"""The query engine: level resolution, SQL construction, and refusals."""

from __future__ import annotations

import pytest

from atlasql import query
from atlasql.models import Condition, GeoFilter


@pytest.fixture(scope="module")
def loaded(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM metrics WHERE metric_name = 'gdp_per_capita'"
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("GDP not imported; run import-world-bank")
    return schema


def test_auto_level_picks_country_for_a_country_only_metric(loaded):
    result = query.run(
        GeoFilter(conditions=[Condition(metric="gdp_per_capita", op=">", value=40000)]),
        loaded,
    )
    assert result.level == "country"
    assert result.level_chosen_by == "auto"
    assert result.count > 0


def test_results_are_ranked_and_capped_at_top_n(loaded):
    result = query.run(
        GeoFilter(
            conditions=[Condition(metric="gdp_per_capita", op=">", value=1000)],
            top_n=7,
        ),
        loaded,
    )
    values = [row.metrics["gdp_per_capita"].value for row in result.results]
    assert len(values) == 7
    assert values == sorted(values, reverse=True)


def test_order_ascending_reverses_the_ranking(loaded):
    result = query.run(
        GeoFilter(
            conditions=[Condition(metric="gdp_per_capita", op=">", value=1000)],
            order="asc",
            top_n=5,
        ),
        loaded,
    )
    values = [row.metrics["gdp_per_capita"].value for row in result.results]
    assert values == sorted(values)


def test_every_returned_row_satisfies_the_conditions(loaded):
    threshold = 40000
    result = query.run(
        GeoFilter(
            conditions=[Condition(metric="gdp_per_capita", op=">", value=threshold)],
            top_n=100,
        ),
        loaded,
    )
    assert all(r.metrics["gdp_per_capita"].value > threshold for r in result.results)


def test_unknown_metric_is_rejected_by_name(loaded):
    with pytest.raises(query.UnknownMetricError) as excinfo:
        query.run(
            GeoFilter(conditions=[Condition(metric="unicorn_density", op=">", value=1)]),
            loaded,
        )
    assert excinfo.value.blocking_metric == "unicorn_density"


def test_explicit_level_without_coverage_names_the_blocking_metric(loaded):
    """The 'GDP per capita at city level' case: a named refusal, not an empty table."""
    with pytest.raises(query.NoLevelWithCoverageError) as excinfo:
        query.run(
            GeoFilter(
                level="continent",  # no metric has continent coverage
                conditions=[Condition(metric="gdp_per_capita", op=">", value=40000)],
            ),
            loaded,
        )
    error = excinfo.value
    assert error.blocking_metric == "gdp_per_capita"
    assert "coverage" in error.message
    assert error.as_dict()["level"] == "continent"


def test_values_are_bound_parameters_not_concatenated(loaded):
    """A value that would break out of a literal must survive as a parameter."""
    geo_filter = GeoFilter(
        conditions=[Condition(metric="gdp_per_capita", op=">", value=40000)]
    )
    statement, params = query.build_sql(geo_filter, "country")
    rendered = statement.as_string(loaded)
    assert "40000" not in rendered, "condition value was inlined into the SQL"
    assert "gdp_per_capita" not in rendered, "metric name was inlined into the SQL"
    assert 40000 in params.values()
    assert "gdp_per_capita" in params.values()


def test_sort_by_a_metric_outside_the_conditions(loaded):
    result = query.run(
        GeoFilter(
            conditions=[Condition(metric="gdp_per_capita", op=">", value=1000)],
            sort_by="gdp_per_capita",
            top_n=3,
        ),
        loaded,
    )
    assert result.count == 3
