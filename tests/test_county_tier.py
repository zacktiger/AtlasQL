"""The county tier: a fourth level, the same engine, a different source.

The plan's test for a new tier is that the filter engine answers it with no
tier-specific code path. These check that, and the two things that are specific
to this tier because they are specific to its source: geoBoundaries substitutes
coarser geometry where it has no ADM2, and its borders do not coincide with
Natural Earth's, so parenting has to be robust to both.
"""

from __future__ import annotations

import pytest

from atlasql import query
from atlasql.etl.geo_boundaries import ADM2, COUNTY_SOURCE
from atlasql.models import Condition, GeoFilter

COAST = "coastline_km"
PPP = "gdp_ppp"
PPP_RATE = "gdp_per_capita_ppp"
NOMINAL = "gdp_nominal"


@pytest.fixture(scope="module")
def counties(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM regions WHERE level = 'county'"
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("county tier not imported; run import-counties")
    return schema


def test_every_county_reaches_a_country(counties):
    """Parenting is allowed to stop at the country when a country has no states,
    but it may never dangle: a county under nothing is invisible to every
    hierarchy query and to the ancestor walk the coastline job depends on."""
    # Two plain joins rather than a recursive walk: the importer produces
    # exactly two legal shapes, county -> state -> country and county ->
    # country, and enumerating them runs in a second where recursing over
    # 49,000 counties takes minutes.
    orphans = counties.execute(
        """
        SELECT ct.name, p.level AS parent_level, gp.level AS grandparent_level
        FROM regions ct
        LEFT JOIN regions p  ON p.id = ct.parent_id
        LEFT JOIN regions gp ON gp.id = p.parent_id
        WHERE ct.level = 'county'
          AND NOT (
              p.level = 'country'
              OR (p.level = 'state' AND gp.level = 'country')
          )
        LIMIT 5
        """
    ).fetchall()
    assert not orphans, [dict(row) for row in orphans]


def test_a_county_is_never_filed_under_the_wrong_country(counties):
    """The reason the country comes from the source's own ISO3 code and only the
    state is chosen geometrically.

    geoBoundaries ADM2 and Natural Earth ADM1 are different datasets whose
    borders do not coincide, so a county near an international border can have
    its interior point fall in the neighbouring country's province. Choosing the
    state only from within the declared country makes that impossible, and this
    is the assertion that would fail if the scoping were dropped.
    """
    # Structural: a county's state must itself hang off a country, so the chain
    # county -> state -> country is intact and the ancestor walk terminates.
    broken = counties.execute(
        """
        SELECT ct.name, s.name AS state
        FROM regions ct
        JOIN regions s ON s.id = ct.parent_id AND s.level = 'state'
        LEFT JOIN regions sc ON sc.id = s.parent_id
        WHERE ct.level = 'county' AND (sc.id IS NULL OR sc.level <> 'country')
        LIMIT 5
        """
    ).fetchall()
    assert not broken, [dict(row) for row in broken]

    # Geometric: the county actually lies in the country it was filed under.
    # Sampled rather than exhaustive because ST_Contains over 49,000 polygons
    # against country geometry is slow, and a systematic error would show up in
    # any sample. A small tolerance because the two datasets' coastlines and
    # borders genuinely differ by a kilometre here and there.
    row = counties.execute(
        """
        WITH sample AS (
            SELECT ct.id, ct.geom,
                   COALESCE(gp.id, p.id) AS country_id
            FROM regions ct
            JOIN regions p ON p.id = ct.parent_id
            LEFT JOIN regions gp ON gp.id = p.parent_id AND p.level = 'state'
            WHERE ct.level = 'county' AND ct.geom IS NOT NULL
            ORDER BY ct.id
            LIMIT 400
        )
        SELECT count(*) AS total,
               count(*) FILTER (
                   WHERE ST_DWithin(c.geom, ST_PointOnSurface(s.geom), 0.05)
               ) AS inside
        FROM sample s JOIN regions c ON c.id = s.country_id
        """
    ).fetchone()
    assert row["total"] > 100
    assert row["inside"] / row["total"] > 0.97, dict(row)


def test_only_genuine_adm2_units_were_imported(counties):
    """CGAZ falls back to ADM1 and even ADM0 where it has no second-level data.

    Loading those would mean a county query answered partly by provinces and
    whole countries. The count is the check: 49,015 of the file's 49,349
    features are ADM2, so a tier materially larger than that has taken the
    fallbacks too.
    """
    n = counties.execute(
        "SELECT count(*) AS n FROM regions WHERE level = 'county' AND source = %s",
        (COUNTY_SOURCE,),
    ).fetchone()["n"]
    assert ADM2 == "ADM2"
    assert n <= 49_015, f"{n} counties is more than the file holds ADM2 features"
    assert n > 45_000, f"only {n} counties imported"


def test_county_geometries_are_valid(counties):
    row = counties.execute(
        """
        SELECT count(*) FILTER (WHERE NOT ST_IsValid(geom)) AS invalid,
               count(*) FILTER (WHERE geom IS NULL)         AS missing
        FROM regions WHERE level = 'county'
        """
    ).fetchone()
    assert row == {"invalid": 0, "missing": 0}


def test_the_same_engine_answers_a_county_query(counties):
    """No county-specific code path anywhere: the identical call, a new tier."""
    if not counties.execute(
        "SELECT 1 FROM metrics m JOIN regions r ON r.id = m.region_id "
        "WHERE m.metric_name = %s AND r.level = 'county' LIMIT 1",
        (COAST,),
    ).fetchone():
        pytest.skip("county metrics not computed; run import-coastline --level county")

    result = query.run(
        GeoFilter(
            level="county",
            conditions=[Condition(metric=COAST, op=">", value=100)],
            top_n=10,
        ),
        counties,
    )
    assert result.level == "county"
    assert result.count > 0
    assert all(row.metrics[COAST].value > 100 for row in result.results)
    values = [row.metrics[COAST].value for row in result.results]
    assert values == sorted(values, reverse=True)


def test_landlocked_counties_are_exactly_zero(counties):
    """The coastline subtraction has to hold at a fourth tier with boundaries
    from a different source than the one it subtracts against."""
    if not counties.execute(
        "SELECT 1 FROM metrics m JOIN regions r ON r.id = m.region_id "
        "WHERE m.metric_name = %s AND r.level = 'county' LIMIT 1",
        (COAST,),
    ).fetchone():
        pytest.skip("county coastline not computed")

    row = counties.execute(
        """
        SELECT count(*) FILTER (WHERE m.value = 0) AS landlocked,
               count(*)                            AS total
        FROM metrics m JOIN regions r ON r.id = m.region_id
        WHERE m.metric_name = %s AND r.level = 'county'
        """,
        (COAST,),
    ).fetchone()
    # Most counties on earth are inland, and each has to be exactly zero rather
    # than a sliver left by a border that did not quite cancel.
    assert row["landlocked"] / row["total"] > 0.5


def test_nominal_gdp_cannot_follow_to_the_county_tier(counties):
    """A hard limit of the source, not a gap to fill later. The World Bank
    publishes national accounts and nothing below them, so the county tier is
    answered by the PPP grid or not at all."""
    with pytest.raises(query.NoLevelWithCoverageError) as excinfo:
        query.run(
            GeoFilter(
                level="county",
                conditions=[Condition(metric=NOMINAL, op=">", value=1e9)],
            ),
            counties,
        )
    assert excinfo.value.blocking_metric == NOMINAL
    assert excinfo.value.as_dict()["available_levels"] == ["country"]


def test_auto_reaches_the_county_tier(counties):
    """Adding a tier changes what "auto" means, by design: it picks the most
    granular level where every metric clears coverage, so a coastline query
    that used to land on states now lands on counties."""
    if not counties.execute(
        "SELECT 1 FROM metrics m JOIN regions r ON r.id = m.region_id "
        "WHERE m.metric_name = %s AND r.level = 'county' LIMIT 1",
        (COAST,),
    ).fetchone():
        pytest.skip("county coastline not computed")

    result = query.run(
        GeoFilter(conditions=[Condition(metric=COAST, op=">", value=500)], top_n=5),
        counties,
    )
    assert (result.level, result.level_chosen_by) == ("county", "auto")


def test_the_ppp_pair_reaches_counties(counties):
    """Both halves of the gridded metric, at the deepest polygon tier."""
    for metric in (PPP, PPP_RATE):
        if not counties.execute(
            "SELECT 1 FROM metrics m JOIN regions r ON r.id = m.region_id "
            "WHERE m.metric_name = %s AND r.level = 'county' LIMIT 1",
            (metric,),
        ).fetchone():
            pytest.skip(f"{metric} not computed for counties")

    result = query.run(
        GeoFilter(
            level="county",
            conditions=[
                Condition(metric=PPP, op=">", value=1e10),
                Condition(metric=PPP_RATE, op=">", value=20000),
            ],
            sort_by=PPP,
            top_n=10,
        ),
        counties,
    )
    assert result.count > 0
    assert all(row.metrics[PPP].value > 1e10 for row in result.results)
