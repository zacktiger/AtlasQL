"""Subnational GDP per capita (PPP) from the Kummu gridded dataset.

`gdp_per_capita` from the World Bank stops at the country tier, because the
Bank reports nothing below it. This job fills the tiers underneath from a
different source rather than stretching that one: Kummu, Kosonen and
Masoumzadeh Sayyar's downscaled global GDP per capita PPP, which reports
subnational admin units and covers 1990-2024 on a 5 arc-minute grid.

It is deliberately a *separate metric* from `gdp_per_capita` rather than more
rows under the same name. The two are not the same quantity: the World Bank
figure is current US dollars, this one is 2021 international dollars at
purchasing power parity. Writing both under one name would put a unit on the
metric registry that is wrong at half the levels, and a query filtering
"> 40000" would silently mean two different things depending on which level
auto-detection picked. Three elevation metrics exist for the same reason - one
honest number per thing measured beats one convenient number that hedges.

GDP per capita is a ratio, so it cannot be averaged over cells the way
elevation can. An area-weighted mean of the per-capita grid answers "what does
the average square kilometre here produce per head", which is not a question
anyone asks: it lets empty land outvote the cities. New York State came out at
79.6k that way, against a real figure near 110k, because upstate covers far
more ground than New York City does.

So each region is aggregated the way the quantity is actually defined - total
output over total people:

    GDP per capita = sum(cell GDP) / sum(cell population)

The dataset ships total GDP per cell, and per-capita at the finest admin units
it resolves, on the same 5 arc-minute grid. Dividing one by the other recovers
population per cell, so the weights come from the same source as the values
and need no second dataset. The derived populations are a free check on the
method, and they land where they should: 39.9M for California, 131.8M for
Bihar.

Known limitation, and it is a real one: this dataset reports genuinely
subnational accounts for 89 countries. Elsewhere the downscaling has no
subnational signal to work from, so states within one country differ only by
how population is spread, not by any measured difference in output. Coverage
percentage cannot express that - a downscaled value is present, not missing -
so it is stated in the metric description, which is what /metadata serves to
the frontend.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import rasterio
from rasterio.mask import mask

from atlasql import db
from atlasql.etl import availability, download
from atlasql.etl.metrics import MetricRow, upsert_metrics
from atlasql.etl.regions import geometries_for, points_for

log = logging.getLogger(__name__)

METRIC = "gdp_per_capita_ppp"

# Total output under the boundary: the numerator the per-capita figure is
# already built from, published rather than discarded. It exists only where
# there is a boundary to total under — see `TOTAL_UNSUPPORTED_LEVELS`.
TOTAL_METRIC = "gdp_ppp"

ZENODO_RECORD = "18429133"
BASE_URL = f"https://zenodo.org/api/records/{ZENODO_RECORD}/files/{{}}/content"

# Total GDP per cell, and per-capita at the finest admin units the source
# resolves. Both are the same 5 arc-minute grid, so cell population falls out
# of dividing one by the other. Every polygon tier uses this one pair - a tier
# is a different set of boundaries laid over the same grids, not a different
# file, which is why adding one needs no code.
GDP_TOTAL_RASTER = "rast_gdpTot_1990_2024_5arcmin.tif"
GDP_PER_CAPITA_RASTER = "rast_adm2_gdp_perCapita_1990_2024.tif"

# Continents are dissolved from countries and hold no metrics of their own.
# Averaging per capita figures across countries needs population weights this
# dataset does not carry at that scale, and an unweighted mean of national
# figures is a number nobody would want.
UNSUPPORTED_LEVELS = ("continent",)

# A city is a point. Sampling the per-capita grid at a location is meaningful —
# it is the rate where the city stands — but there is no area to total output
# over, and the grid cell a city sits in is not the city's economy. So the total
# is a polygon-tier metric, absent at city level rather than guessed, exactly as
# elevation_min and elevation_max are.
TOTAL_UNSUPPORTED_LEVELS = ("continent", "city")

DESCRIPTION = (
    "Gross domestic product per person at purchasing power parity, in constant "
    "2021 international dollars. Downscaled from reported subnational accounts "
    "to a 5 arc-minute grid, then totalled over each region's boundary and "
    "divided by the population living inside it. Below the country tier this is "
    "the only GDP measure available, and it is not interchangeable with "
    "gdp_per_capita: that one is the World Bank's national figure in current US "
    "dollars. Coverage is global, but genuinely subnational accounts are "
    "reported for only 89 countries; elsewhere the downscaling has no "
    "subnational signal, so states differ only by how their population is "
    "distributed."
)

TOTAL_DESCRIPTION = (
    "Total economic output inside the region at purchasing power parity, in "
    "constant 2021 international dollars, totalled from a 5 arc-minute grid "
    "over the region's boundary. This is the same sum that gdp_per_capita_ppp "
    "divides by its population, over the same cells, so the two are consistent "
    "with each other by construction and their ratio is the population the "
    "grid puts inside the boundary. Unlike gdp_nominal it reaches subnational "
    "tiers, and unlike gdp_nominal it is at PPP rather than market exchange "
    "rates — the two are different units and a threshold means different "
    "things to each. It is a gridded total rather than a national account, so "
    "it approaches published figures without reproducing them, and the same "
    "downscaling limit applies: genuinely subnational accounts exist for 89 "
    "countries, and elsewhere a region's share reflects how population is "
    "distributed rather than measured output. Two consequences of totalling "
    "over a grid, which do not affect the per-capita figure because they "
    "cancel in its ratio: a region smaller than a 5 arc-minute cell is "
    "credited with that whole cell, so small districts are overstated and two "
    "sitting inside one cell can report the same number; and totals do not add "
    "up across a tier, because a cell on a border counts toward both sides — "
    "summing a country's states overshoots its own total by around 18%, and "
    "summing a state's counties overshoots by about 85%, because the smaller "
    "and more numerous the regions the more border cells there are to count "
    "twice. Compare and rank regions within a tier with it; never add them "
    "together, and do not compare a figure at one tier against another."
)

SOURCE = "Kummu et al. 2025, downscaled gridded GDP per capita PPP (CC BY 4.0)"


def register_metric(conn, totals: bool = False) -> None:
    """Put the PPP GDP metrics in the live registry.

    The total is registered only when it is about to be written. Registering it
    from a city-tier run would advertise a metric this database holds no values
    for at any level.
    """
    db.register_metric(
        conn,
        metric_name=METRIC,
        label="GDP per capita (PPP)",
        unit="2021 int$",
        description=DESCRIPTION,
        source=SOURCE,
    )
    if totals:
        db.register_metric(
            conn,
            metric_name=TOTAL_METRIC,
            label="GDP (PPP)",
            unit="2021 int$",
            description=TOTAL_DESCRIPTION,
            source=SOURCE,
        )


def _band_for_year(path, year: int | None) -> tuple[int, int]:
    """Resolve a band index and the year it holds.

    Bands are named `gdp_pc_<year>`, so the vintage is read off the file rather
    than hardcoded. `year=None` takes the most recent band, which is what a
    v1 with no time series wants.
    """
    with rasterio.open(path) as raster:
        years = []
        for index, description in enumerate(raster.descriptions, start=1):
            _, _, suffix = (description or "").rpartition("_")
            if suffix.isdigit():
                years.append((int(suffix), index))
    if not years:
        raise RuntimeError(f"{path.name} has no year-labelled bands to choose from")
    if year is None:
        chosen_year, band = max(years)
    else:
        matches = [(y, b) for y, b in years if y == year]
        if not matches:
            available = ", ".join(str(y) for y, _ in years)
            raise RuntimeError(f"{path.name} has no band for {year}; has {available}")
        chosen_year, band = matches[0]
    return band, chosen_year


def _fetch_raster(filename: str):
    return download.fetch(BASE_URL.format(filename), filename)


def import_gridded_gdp(level: str = "state", year: int | None = None) -> None:
    """Write GDP per capita (PPP) for every region at `level`. Safe to re-run."""
    if level in UNSUPPORTED_LEVELS:
        raise RuntimeError(
            f"{METRIC} is not computed at {level} level: continents are dissolved "
            "from countries and the source has no accounts at that scale"
        )

    per_capita_path = _fetch_raster(GDP_PER_CAPITA_RASTER)
    band, vintage = _band_for_year(per_capita_path, year)
    log.info("using %s band %d (%d)", per_capita_path.name, band, vintage)

    totals_wanted = level not in TOTAL_UNSUPPORTED_LEVELS
    points = points_for(level)
    if points:
        rows = _sample_points(points, per_capita_path, band, vintage)
        total = len(points)
    else:
        areas = geometries_for(level)
        if not areas:
            raise RuntimeError(
                f"no regions with geometry or centroids at level {level!r}"
            )
        total_path = _fetch_raster(GDP_TOTAL_RASTER)
        total_band, total_vintage = _band_for_year(total_path, vintage)
        assert total_vintage == vintage  # both resolved to the same year by name
        rows = _aggregate_areas(
            areas, total_path, total_band, per_capita_path, band, vintage
        )
        total = len(areas)

    with db.connect() as conn:
        register_metric(conn, totals=totals_wanted)
        upsert_metrics(conn, rows)
        availability.refresh(conn)

    # Counted per metric rather than per row: the polygon path emits two rows
    # per region, so len(rows) is no longer a count of regions covered.
    covered = sum(1 for row in rows if row["metric_name"] == METRIC)
    log.info(
        "%s: %d of %d %s regions covered (%.0f%%)%s",
        METRIC,
        covered,
        total,
        level,
        100 * covered / total if total else 0.0,
        f"; {TOTAL_METRIC} written for the same regions" if totals_wanted else "",
    )


def _aggregate_areas(
    areas,
    total_path,
    total_band: int,
    per_capita_path,
    per_capita_band: int,
    vintage: int,
) -> list[MetricRow]:
    """Total the GDP under each boundary and divide by the people under it.

    Cell population is recovered as total GDP over GDP per capita, so the
    weights and the values come from the same grid and cannot disagree about
    where anyone lives.

    `all_touched` is on so a region smaller than a 5 arc-minute cell - most of
    the island states - still picks up the cell it sits in rather than coming
    back empty.
    """
    log.info("aggregating GDP per capita for %d regions", len(areas))
    rows: list[MetricRow] = []

    with rasterio.open(total_path) as totals, rasterio.open(per_capita_path) as rates:
        for index, area in enumerate(areas, start=1):
            shapes = [area["geometry"]]
            gdp, _ = mask(
                totals,
                shapes,
                crop=True,
                all_touched=True,
                indexes=total_band,
                filled=True,
                nodata=float("nan"),
            )
            rate, _ = mask(
                rates,
                shapes,
                crop=True,
                all_touched=True,
                indexes=per_capita_band,
                filled=True,
                nodata=float("nan"),
            )
            gdp = np.asarray(gdp, dtype="float64")
            rate = np.asarray(rate, dtype="float64")

            # A cell counts only where both grids have a value and the rate is
            # positive; dividing by a zero or absent rate would invent people.
            usable = np.isfinite(gdp) & np.isfinite(rate) & (rate > 0)
            if not usable.any():
                # Nothing under the boundary: islands the grid generalises
                # away, and Antarctica, which has no economy to report. Left
                # absent rather than zeroed - a zero would be a claim.
                continue
            gdp_sum = float(gdp[usable].sum())
            population = float((gdp[usable] / rate[usable]).sum())
            if population <= 0:
                continue

            # Both metrics from the same masked sum, so the published total is
            # exactly the numerator behind the published rate. Totalling over a
            # wider mask — every cell with GDP, including those the rate grid
            # has no value for — would capture marginally more output at the
            # cost of the two metrics disagreeing about the same region, and a
            # user dividing one by the other getting a population that matches
            # neither.
            rows.append(
                {
                    "region_id": area["id"],
                    "metric_name": METRIC,
                    "value": gdp_sum / population,
                    "year": vintage,
                }
            )
            rows.append(
                {
                    "region_id": area["id"],
                    "metric_name": TOTAL_METRIC,
                    "value": gdp_sum,
                    "year": vintage,
                }
            )
            if index % 500 == 0:
                log.info("aggregated %d of %d regions", index, len(areas))
    return rows


def _sample_points(points, path, band: int, vintage: int) -> list[MetricRow]:
    """Read the grid at each location, for tiers that are points not polygons.

    A city has no area to average over, so the honest value is the admin unit
    it stands in.
    """
    log.info("sampling GDP per capita at %d locations", len(points))
    with rasterio.open(path) as raster:
        nodata = raster.nodata
        samples = list(
            raster.sample([(p["x"], p["y"]) for p in points], indexes=band)
        )

    rows: list[MetricRow] = []
    for point, sample in zip(points, samples, strict=True):
        value = float(sample[0])
        # This grid marks empty cells with NaN, so the finiteness check is the
        # one that actually fires; the explicit comparison covers a re-release
        # that switches to a sentinel.
        if not math.isfinite(value):
            continue
        if nodata is not None and math.isfinite(nodata) and value == nodata:
            continue
        rows.append(
            {
                "region_id": point["id"],
                "metric_name": METRIC,
                "value": value,
                "year": vintage,
            }
        )
    return rows


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_gridded_gdp()
