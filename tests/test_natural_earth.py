"""Checks on the imported country tier.

These read whatever is in the database rather than re-running the import, so
they are cheap and skip cleanly on a database that has not been loaded yet.
"""

from __future__ import annotations

import pytest

from atlasql.etl.natural_earth import CONTINENT_SOURCE, COUNTRY_SOURCE


@pytest.fixture(scope="module")
def loaded(schema):
    count = schema.execute(
        "SELECT count(*) AS n FROM regions WHERE source = %s", (COUNTRY_SOURCE,)
    ).fetchone()["n"]
    if count == 0:
        pytest.skip("country tier not imported; run import-natural-earth")
    return schema


def test_country_count_is_plausible(loaded):
    n = loaded.execute("SELECT count(*) AS n FROM regions WHERE level='country'").fetchone()["n"]
    assert 190 <= n <= 300, f"unexpected country count {n}"


def test_every_country_has_a_continent_parent(loaded):
    """A hole in the hierarchy means CONTINENT_OVERRIDES needs a new entry."""
    rows = loaded.execute(
        "SELECT name FROM regions WHERE level='country' AND parent_id IS NULL"
    ).fetchall()
    assert not rows, f"countries with no continent: {[r['name'] for r in rows]}"


def test_parents_of_countries_are_continents(loaded):
    bad = loaded.execute(
        """
        SELECT r.name FROM regions r
        JOIN regions p ON p.id = r.parent_id
        WHERE r.level = 'country' AND p.level <> 'continent'
        """
    ).fetchall()
    assert not bad


def test_geometries_are_valid_multipolygons_in_4326(loaded):
    row = loaded.execute(
        """
        SELECT
          count(*) FILTER (WHERE NOT ST_IsValid(geom))            AS invalid,
          count(*) FILTER (WHERE ST_SRID(geom) <> 4326)           AS wrong_srid,
          count(*) FILTER (WHERE GeometryType(geom) <> 'MULTIPOLYGON') AS not_multi,
          count(*) FILTER (WHERE geom IS NULL OR centroid IS NULL) AS missing
        FROM regions WHERE source IN (%s, %s)
        """,
        (COUNTRY_SOURCE, CONTINENT_SOURCE),
    ).fetchone()
    assert row == {"invalid": 0, "wrong_srid": 0, "not_multi": 0, "missing": 0}


def test_known_countries_are_present_under_the_right_continent(loaded):
    rows = loaded.execute(
        """
        SELECT r.source_id, p.name AS continent
        FROM regions r JOIN regions p ON p.id = r.parent_id
        WHERE r.source = %s AND r.source_id IN ('USA','IND','FRA','MDV','NOR')
        """,
        (COUNTRY_SOURCE,),
    ).fetchall()
    got = {r["source_id"]: r["continent"] for r in rows}
    assert got == {
        "USA": "North America",
        "IND": "Asia",
        # France and Norway are the reason source_id is ADM0_A3 and not
        # ISO_A3, which Natural Earth leaves as -99 for both.
        "FRA": "Europe",
        "NOR": "Europe",
        # Maldives is filed under "Seven seas (open ocean)" upstream.
        "MDV": "Asia",
    }
