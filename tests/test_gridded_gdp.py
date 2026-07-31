"""Subnational GDP per capita from the gridded source.

The band-selection tests build a raster rather than downloading one, so they
run without the 300 MB of source grids. The tier tests skip unless the import
has actually been run.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from atlasql.etl import gridded_gdp


@pytest.fixture
def yearly_raster(tmp_path):
    """A three-band raster labelled like the real one: gdp_pc_2020..2022."""
    path = tmp_path / "years.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=3,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(0, 2, 1, 1),
        nodata=float("nan"),
    ) as raster:
        for band in (1, 2, 3):
            raster.write(np.full((2, 2), band, dtype="float32"), band)
        raster.descriptions = ("gdp_pc_2020", "gdp_pc_2021", "gdp_pc_2022")
    return path


def test_the_latest_band_is_chosen_by_default(yearly_raster):
    band, year = gridded_gdp._band_for_year(yearly_raster, None)
    assert (band, year) == (3, 2022)


def test_a_year_can_be_asked_for_by_name(yearly_raster):
    assert gridded_gdp._band_for_year(yearly_raster, 2020) == (1, 2020)


def test_a_year_the_raster_does_not_have_is_refused(yearly_raster):
    # Silently falling back to the nearest year would change what the stored
    # value means without changing what it is labelled.
    with pytest.raises(RuntimeError, match="no band for 1990"):
        gridded_gdp._band_for_year(yearly_raster, 1990)


def test_continents_are_refused_rather_than_guessed():
    with pytest.raises(RuntimeError, match="continent"):
        gridded_gdp.import_gridded_gdp(level="continent")


def _values_at(conn, level):
    return conn.execute(
        """
        SELECT count(*) AS n, min(value) AS lo, max(value) AS hi
        FROM metrics m JOIN regions r ON r.id = m.region_id
        WHERE m.metric_name = %s AND r.level = %s
        """,
        (gridded_gdp.METRIC, level),
    ).fetchone()


@pytest.mark.parametrize("level", ["country", "state", "city"])
def test_the_metric_is_populated_and_plausible(schema, level):
    row = _values_at(schema, level)
    if not row["n"]:
        pytest.skip(f"not imported; run import-gridded-gdp --level {level}")
    # No country is poorer than a few hundred international dollars a head, and
    # none is richer than the oil enclaves at a few hundred thousand. A run that
    # lands outside this has picked up the wrong band or divided by the wrong
    # grid, both of which still produce finite-looking numbers.
    assert 100 < row["lo"] < row["hi"] < 1_000_000


def test_states_clear_the_coverage_threshold(schema):
    row = schema.execute(
        """
        SELECT coverage_pct FROM metric_availability
        WHERE metric_name = %s AND level = 'state'
        """,
        (gridded_gdp.METRIC,),
    ).fetchone()
    if row is None:
        pytest.skip("not imported; run import-gridded-gdp --level state")
    # The whole point of the metric: the World Bank figure cannot reach this
    # tier, so if this one stops clearing the bar, GDP queries silently fall
    # back to countries again.
    from atlasql import config

    assert row["coverage_pct"] > config.COVERAGE_THRESHOLD_PCT


def test_the_two_gdp_metrics_stay_separate(schema):
    """They are different units; merging them would make a threshold ambiguous."""
    rows = schema.execute(
        """
        SELECT metric_name, unit FROM metric_definitions
        WHERE metric_name IN ('gdp_per_capita', %s)
        """,
        (gridded_gdp.METRIC,),
    ).fetchall()
    units = {row["metric_name"]: row["unit"] for row in rows}
    if len(units) < 2:
        pytest.skip("both GDP metrics must be imported")
    assert units["gdp_per_capita"] != units[gridded_gdp.METRIC]
