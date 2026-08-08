"""Total GDP, nominal and PPP, beside the per-capita metrics that came first.

Four GDP metrics now exist and none of them is interchangeable with another:

    gdp_nominal          current US$    country            World Bank
    gdp_per_capita       current US$    country            World Bank
    gdp_ppp              2021 int$      country, state     Kummu grid
    gdp_per_capita_ppp   2021 int$      country, state, city

Two axes, kept apart for the reason the registry exists: nominal and PPP are
different units, and total and per-capita are different quantities. A single
"GDP" would make a numeric threshold mean four things depending on which level
auto-detection happened to pick.
"""

from __future__ import annotations

import pytest

from atlasql import query
from atlasql.models import Condition, GeoFilter

NOMINAL = "gdp_nominal"
PER_CAPITA = "gdp_per_capita"
POPULATION = "population"


@pytest.fixture(scope="module")
def nominal(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM metrics WHERE metric_name = %s", (NOMINAL,)
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("nominal GDP not imported; run import-world-bank")
    return schema


def test_nominal_gdp_over_population_reproduces_gdp_per_capita(nominal):
    """Three independently fetched World Bank series have to broadly agree.

    Separate API calls for separate indicators, so this is a real check that
    the right series landed under the right name and the values survived the
    trip — not an identity that holds by construction. A transposed or
    misscaled import would move nearly every country at once.

    It is a majority check rather than a universal one because the agreement
    genuinely is not universal, and the exceptions are the source's rather than
    ours: the series do not always cover the same territory. Cyprus is the
    extreme, at 39% — its GDP covers the government-controlled area while
    SP.POP.TOTL counts the whole island — and Ukraine, Russia, Morocco and
    Tanzania differ by a few percent over Crimea, Western Sahara and Zanzibar.
    That is also the reason `gdp_per_capita` stores the Bank's reported figure
    rather than a total divided by a population: the reported one is the one
    with a consistent denominator behind it.
    """
    rows = nominal.execute(
        """
        SELECT r.name, g.value / p.value AS derived, pc.value AS reported
        FROM regions r
        JOIN metrics g  ON g.region_id  = r.id AND g.metric_name  = %(nominal)s
        JOIN metrics p  ON p.region_id  = r.id AND p.metric_name  = %(population)s
        JOIN metrics pc ON pc.region_id = r.id AND pc.metric_name = %(per_capita)s
        WHERE r.level = 'country' AND p.value > 0 AND g.year = pc.year AND g.year = p.year
        """,
        {"nominal": NOMINAL, "population": POPULATION, "per_capita": PER_CAPITA},
    ).fetchall()
    assert len(rows) > 100, "too few countries carry all three to be a real check"

    agreeing = [r for r in rows if r["derived"] == pytest.approx(r["reported"], rel=0.01)]
    disagreeing = [r for r in rows if r not in agreeing]
    assert len(agreeing) / len(rows) > 0.95, (
        f"only {len(agreeing)}/{len(rows)} countries reconcile; "
        f"worst: {[dict(r) for r in disagreeing[:5]]}"
    )


def test_nominal_gdp_is_a_total_not_a_rate(nominal):
    """The distinction the two metrics exist to keep. Totals are enormous and
    per-capita figures are not, so confusing them is immediately visible."""
    row = nominal.execute(
        """
        SELECT max(value) AS largest, min(value) FILTER (WHERE value > 0) AS smallest
        FROM metrics m JOIN regions r ON r.id = m.region_id
        WHERE m.metric_name = %s AND r.level = 'country'
        """,
        (NOMINAL,),
    ).fetchone()
    # The largest economy is tens of trillions; no per-capita figure comes near.
    assert row["largest"] > 1e13
    assert row["smallest"] < 1e9  # and the smallest are under a billion


def test_nominal_gdp_stops_at_the_country_tier(nominal):
    """The World Bank reports nothing subnational, so a state query naming it
    is refused rather than answered from a downscaled stand-in."""
    with pytest.raises(query.NoLevelWithCoverageError) as excinfo:
        query.run(
            GeoFilter(level="state", conditions=[Condition(metric=NOMINAL, op=">", value=1e9)]),
            nominal,
        )
    assert excinfo.value.blocking_metric == NOMINAL
    assert excinfo.value.as_dict()["available_levels"] == ["country"]


def test_the_largest_economies_rank_as_expected(nominal):
    result = query.run(
        GeoFilter(
            level="country",
            conditions=[Condition(metric=NOMINAL, op=">", value=1e12)],
            top_n=5,
        ),
        nominal,
    )
    names = [row.name for row in result.results]
    assert names[0] == "United States of America"
    assert "People's Republic of China" in names
    values = [row.metrics[NOMINAL].value for row in result.results]
    assert values == sorted(values, reverse=True)


def test_total_and_per_capita_rank_differently(nominal):
    """Why both are worth having. The largest economies are not the richest
    per head, and a metric that conflated them could not express either."""
    biggest = query.run(
        GeoFilter(
            level="country",
            conditions=[Condition(metric=NOMINAL, op=">", value=0)],
            sort_by=NOMINAL,
            top_n=10,
        ),
        nominal,
    )
    richest = query.run(
        GeoFilter(
            level="country",
            conditions=[Condition(metric=PER_CAPITA, op=">", value=0)],
            sort_by=PER_CAPITA,
            top_n=10,
        ),
        nominal,
    )
    assert [r.name for r in biggest.results] != [r.name for r in richest.results]
