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
PPP = "gdp_ppp"
PPP_PER_CAPITA = "gdp_per_capita_ppp"


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


@pytest.fixture(scope="module")
def ppp(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM metrics WHERE metric_name = %s", (PPP,)
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("PPP total not imported; run import-gridded-gdp --level country")
    return schema


def test_the_ppp_total_and_rate_agree_on_population(ppp):
    """The two are the numerator and the quotient of one masked sum, so their
    ratio is the population the grid puts inside the boundary.

    Checking it against the World Bank's count is the real test: it comes from
    a different source entirely, so agreeing says the grid's population model is
    sound and the total was totalled over the cells the rate was divided by.
    Totalling over a wider mask would show up here as a systematic excess.

    Two thresholds because the agreement is genuinely uneven, in a way that is
    the method's rather than the arithmetic's. `all_touched` counts every cell a
    boundary clips, so a region gets whole 5 arc-minute cells that extend past
    it — a rounding error for Brazil and a third of the answer for Hong Kong.
    The median ratio catches a systematic error; the large-country check catches
    a broken one where the grid is reliable enough for it to mean something.
    """
    rows = ppp.execute(
        """
        SELECT r.name, t.value / pc.value AS derived, p.value AS reported
        FROM regions r
        JOIN metrics t  ON t.region_id  = r.id AND t.metric_name  = %(ppp)s
        JOIN metrics pc ON pc.region_id = r.id AND pc.metric_name = %(rate)s
        JOIN metrics p  ON p.region_id  = r.id AND p.metric_name  = %(population)s
        WHERE r.level = 'country' AND pc.value > 0 AND p.value > 1000000
        """,
        {"ppp": PPP, "rate": PPP_PER_CAPITA, "population": POPULATION},
    ).fetchall()
    assert len(rows) > 100

    ratios = sorted(row["derived"] / row["reported"] for row in rows)
    median = ratios[len(ratios) // 2]
    assert median == pytest.approx(1.0, rel=0.05), f"median ratio {median:.3f}"

    # Where a border cell is a rounding error, the two sources should agree.
    big = [row for row in rows if row["reported"] > 20_000_000]
    close = [r for r in big if r["derived"] == pytest.approx(r["reported"], rel=0.1)]
    assert len(close) / len(big) > 0.9, (
        f"only {len(close)}/{len(big)} large countries recover their population "
        f"within 10%: {[dict(r) for r in big if r not in close][:5]}"
    )


def test_ppp_reorders_the_largest_economies_against_nominal(ppp):
    """Why both totals exist. At market rates the United States is the largest
    economy; at purchasing power parity China is. One metric cannot say both,
    and the answer a user gets should not depend on which one we happened to
    store under the name "GDP"."""
    if not ppp.execute(
        "SELECT 1 FROM metrics WHERE metric_name = %s LIMIT 1", (NOMINAL,)
    ).fetchone():
        pytest.skip("nominal GDP not imported")

    def top(metric):
        return [
            row.name
            for row in query.run(
                GeoFilter(
                    level="country",
                    conditions=[Condition(metric=metric, op=">", value=0)],
                    sort_by=metric,
                    top_n=2,
                ),
                ppp,
            ).results
        ]

    assert top(NOMINAL)[0] == "United States of America"
    assert top(PPP)[0] == "People's Republic of China"


def test_the_ppp_total_reaches_subdivisions(ppp):
    """The gap gdp_nominal cannot fill: the World Bank publishes nothing below
    the country, so this is the only total available at state level."""
    if not ppp.execute(
        "SELECT 1 FROM metrics m JOIN regions r ON r.id = m.region_id "
        "WHERE m.metric_name = %s AND r.level = 'state' LIMIT 1",
        (PPP,),
    ).fetchone():
        pytest.skip("state PPP total not imported; run import-gridded-gdp --level state")

    result = query.run(
        GeoFilter(
            level="state",
            conditions=[Condition(metric=PPP, op=">", value=1e11)],
            top_n=5,
        ),
        ppp,
    )
    assert result.count == 5
    values = [row.metrics[PPP].value for row in result.results]
    assert values == sorted(values, reverse=True)


def test_summing_states_overshoots_the_country_in_one_direction(ppp):
    """Grid totals are not additive across a tier, and the error has a sign.

    `all_touched` credits every cell a boundary clips to the region, so a cell
    on a border counts toward both sides. Summing a country's states therefore
    overshoots its own total — never undershoots, measured across 170 countries
    — by about 18% at the median, and more where the subdivisions are many and
    small: countries with 10 or fewer states run 1.09, those with 50 or more
    run 1.64.

    This is asserted rather than fixed because the alternative is worse. The
    published total is the numerator the published rate is divided by; totalling
    over a narrower mask would buy additivity at the price of the two metrics
    contradicting each other about the same region. The property is in the
    metric description instead, where a user summing them will read it.
    """
    if not ppp.execute(
        "SELECT 1 FROM metrics m JOIN regions r ON r.id = m.region_id "
        "WHERE m.metric_name = %s AND r.level = 'state' LIMIT 1",
        (PPP,),
    ).fetchone():
        pytest.skip("state PPP total not imported")

    rows = ppp.execute(
        """
        SELECT p.name, mc.value AS country_total, sum(ms.value) AS state_total
        FROM regions p
        JOIN metrics mc ON mc.region_id = p.id AND mc.metric_name = %(ppp)s
        JOIN regions s ON s.parent_id = p.id AND s.level = 'state'
        JOIN metrics ms ON ms.region_id = s.id AND ms.metric_name = %(ppp)s
        WHERE p.level = 'country' AND mc.value > 1e10
        GROUP BY p.id, p.name, mc.value
        """,
        {"ppp": PPP},
    ).fetchall()
    assert len(rows) > 100

    # One-sided: double counting can only add. A country whose states came to
    # less than the country would mean cells being dropped, which is a
    # different and much worse bug than the one being tolerated here.
    short = [r for r in rows if r["state_total"] < r["country_total"] * 0.999]
    assert not short, [dict(r) for r in short[:5]]

    ratios = sorted(r["state_total"] / r["country_total"] for r in rows)
    median = ratios[len(ratios) // 2]
    assert 1.0 <= median < 1.5, f"median state/country total {median:.3f}"


def test_the_ppp_total_is_absent_at_city_level(ppp):
    """A point has no area to total output over. The rate is sampled there and
    the total is not, so a city query naming it is refused by name."""
    with pytest.raises(query.NoLevelWithCoverageError) as excinfo:
        query.run(
            GeoFilter(level="city", conditions=[Condition(metric=PPP, op=">", value=0)]),
            ppp,
        )
    assert excinfo.value.blocking_metric == PPP


def test_the_four_gdp_metrics_stay_distinct(ppp):
    """Two axes — nominal against PPP, total against per head — and four names,
    because one threshold has to mean one thing."""
    rows = ppp.execute(
        "SELECT metric_name, unit, label FROM metric_definitions WHERE metric_name = ANY(%s)",
        ([NOMINAL, PER_CAPITA, PPP, PPP_PER_CAPITA],),
    ).fetchall()
    if len(rows) < 4:
        pytest.skip("all four GDP metrics must be imported")
    by_name = {row["metric_name"]: row for row in rows}
    # Different unit across the nominal/PPP axis.
    assert by_name[NOMINAL]["unit"] != by_name[PPP]["unit"]
    assert by_name[PER_CAPITA]["unit"] != by_name[PPP_PER_CAPITA]["unit"]
    # Same unit along the total/per-capita axis, so only the name separates
    # them — which is why the labels have to differ too.
    assert by_name[NOMINAL]["unit"] == by_name[PER_CAPITA]["unit"]
    assert by_name[PPP]["unit"] == by_name[PPP_PER_CAPITA]["unit"]
    assert len({row["label"] for row in rows}) == 4


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
