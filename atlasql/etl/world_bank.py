"""Import GDP per capita from the World Bank API, country tier only.

There is no reliable global subnational GDP source, so this metric exists at
the country level and nowhere else. That is not a gap to paper over: it is
exactly the case that makes `metric_availability` earn its keep, because a
city-level query mentioning GDP per capita has to be rejected by name rather
than quietly returning nothing.

v1 has no time series, so one row per country is stored: the most recent year
(within the last few) the World Bank has a non-empty value for. The year
travels with the value, so "GDP per capita" is never silently a mix of
vintages without saying which one each country came from.
"""

from __future__ import annotations

import logging
import time
from typing import NamedTuple

import httpx

from atlasql import db
from atlasql.etl import availability
from atlasql.etl.metrics import MetricRow, upsert_metrics
from atlasql.etl.natural_earth import COUNTRY_SOURCE
from atlasql.etl.regions import existing_ids

log = logging.getLogger(__name__)

API_URL = "https://api.worldbank.org/v2/country/all/indicator/{indicator}"
COUNTRY_URL = "https://api.worldbank.org/v2/country"


class Indicator(NamedTuple):
    code: str
    label: str
    unit: str
    description: str


# Every World Bank indicator lands through the same path: the code
# reconciliation, the aggregate filtering and the country join below apply
# whatever the indicator is.
INDICATORS: dict[str, Indicator] = {
    "gdp_per_capita": Indicator(
        code="NY.GDP.PCAP.CD",
        label="GDP per capita",
        unit="current US$",
        description=(
            "Gross domestic product per capita in current US dollars, most "
            "recent year available per country (World Bank NY.GDP.PCAP.CD)."
        ),
    ),
    "population": Indicator(
        code="SP.POP.TOTL",
        label="Population",
        unit="people",
        description=(
            "Total population, most recent year available per country (World "
            "Bank SP.POP.TOTL). At city level this metric comes from GeoNames "
            "and counts the settlement rather than the country."
        ),
    ),
}

# Kept for callers and tests that named the original metric directly.
METRIC_NAME = "gdp_per_capita"

# The World Bank and Natural Earth disagree on a handful of codes. Mapping them
# explicitly beats losing the countries silently.
ISO3_ALIASES = {
    "XKX": "KOS",  # Kosovo
    "SSD": "SDS",  # South Sudan
    "PSE": "PSX",  # West Bank and Gaza -> Palestine
    "ROM": "ROU",  # older World Bank responses use ROM for Romania
    "ZAR": "COD",  # likewise for the Democratic Republic of the Congo
}

# Economies with no single region to attach to. The World Bank reports the
# Channel Islands as one economy while Natural Earth carries Jersey and
# Guernsey separately, and splitting one GDP figure across two territories
# would be inventing data.
NO_REGION_BY_DESIGN = {"CHI"}


def _get_json(url: str, params: dict, attempts: int = 4) -> list:
    """GET with retries.

    The World Bank API throttles by returning 400 on requests it served
    happily a minute earlier, so a failure here says nothing about whether the
    request is valid. Retrying with a widening gap is the difference between a
    reliable import and one that fails on whichever indicator happens to be
    second.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.get(url, params=params, timeout=60.0)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
            if attempt == attempts:
                raise
            delay = 2**attempt
            log.warning(
                "World Bank request failed (attempt %d/%d), retrying in %ds: %s",
                attempt,
                attempts,
                delay,
                exc,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def fetch_indicator(indicator: str, years_back: int = 5) -> list[dict]:
    """Fetch the most recent non-empty value of an indicator for every economy.

    Asks for a window of recent observations (`mrv`) rather than the server-side
    `mrnev` filter: the World Bank API intermittently (and, for some indicators,
    persistently) returns 400 for `mrnev` while `mrv` keeps working — reproduced
    with plain `curl` against their API directly, so it is not something a
    retry here can paper over. "Most recent non-empty" is then computed
    client-side by keeping, per economy, the highest-year row with a value.
    """
    params = {
        "format": "json",
        "per_page": "20000",
        "mrv": str(years_back),
    }
    payload = _get_json(API_URL.format(indicator=indicator), params)
    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError(f"unexpected World Bank response shape: {payload!r:.200}")

    header, rows = payload[0], payload[1]
    if header.get("total", 0) > len(rows):
        raise RuntimeError(
            f"World Bank returned {len(rows)} of {header['total']} rows; "
            "raise per_page or paginate"
        )

    latest: dict[str, dict] = {}
    for row in rows:
        if row.get("value") is None:
            continue
        code = row.get("countryiso3code")
        if not code:
            continue
        year = int(row["date"])
        current = latest.get(code)
        if current is None or year > int(current["date"]):
            latest[code] = row

    log.info(
        "fetched %d World Bank rows for %s, %d economies with a value in the last %d years",
        len(rows),
        indicator,
        len(latest),
        years_back,
    )
    return list(latest.values())


def fetch_aggregate_codes() -> set[str]:
    """Codes the World Bank uses for aggregates rather than places.

    "World", "Euro area" and "Middle income" arrive through the same indicator
    endpoint as real economies but carry region id "NA" in the country
    catalogue. They have no region and never will, so separating them here
    keeps the unmatched list meaningful instead of 40 lines of noise.
    """
    payload = _get_json(COUNTRY_URL, {"format": "json", "per_page": "500"})
    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError("unexpected World Bank country catalogue response")
    return {
        row["id"].strip().upper()
        for row in payload[1]
        if (row.get("region") or {}).get("id", "").strip() == "NA"
    }


def import_indicators(names: list[str] | None = None) -> None:
    """Import the named indicators onto country regions. Safe to re-run."""
    selected = list(INDICATORS) if names is None else names
    unknown = [name for name in selected if name not in INDICATORS]
    if unknown:
        raise RuntimeError(f"unknown indicator(s): {', '.join(unknown)}")

    # One catalogue fetch covers every indicator in this run.
    aggregates = fetch_aggregate_codes()

    for metric_name in selected:
        indicator = INDICATORS[metric_name]
        rows = fetch_indicator(indicator.code)

        with db.connect() as conn:
            db.register_metric(
                conn,
                metric_name=metric_name,
                label=indicator.label,
                unit=indicator.unit,
                description=indicator.description,
                source="World Bank",
            )

            country_ids = existing_ids(conn, COUNTRY_SOURCE)
            if not country_ids:
                raise RuntimeError(
                    "no country regions found; run import-natural-earth first"
                )

            metric_rows: list[MetricRow] = []
            unmatched: list[str] = []
            for row in rows:
                value = row.get("value")
                if value is None:
                    continue
                raw_code = (row.get("countryiso3code") or "").strip().upper()
                if not raw_code:
                    continue
                if raw_code in aggregates or raw_code in NO_REGION_BY_DESIGN:
                    continue
                code = ISO3_ALIASES.get(raw_code, raw_code)
                region_id = country_ids.get(code)
                if region_id is None:
                    unmatched.append(
                        f"{raw_code} ({row.get('country', {}).get('value')})"
                    )
                    continue
                metric_rows.append(
                    {
                        "region_id": region_id,
                        "metric_name": metric_name,
                        "value": float(value),
                        "year": int(row["date"]),
                    }
                )

            written = upsert_metrics(conn, metric_rows)
            # In the same transaction as the write: coverage that lags the data
            # it describes would silently change which level a query runs at.
            availability.refresh(conn)

        log.info(
            "%s: %d countries covered of %d (%.0f%%)",
            metric_name,
            written,
            len(country_ids),
            100 * written / max(len(country_ids), 1),
        )
        if unmatched:
            log.warning(
                "%d World Bank economies had no matching country region, add "
                "them to ISO3_ALIASES if they should have one: %s",
                len(unmatched),
                ", ".join(sorted(unmatched)),
            )


def import_gdp_per_capita() -> None:
    """Import GDP per capita only. Kept for callers that want just this one."""
    import_indicators([METRIC_NAME])


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_indicators()
