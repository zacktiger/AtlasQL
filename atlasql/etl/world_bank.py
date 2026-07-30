"""Import GDP per capita from the World Bank API, country tier only.

There is no reliable global subnational GDP source, so this metric exists at
the country level and nowhere else. That is not a gap to paper over: it is
exactly the case that makes `metric_availability` earn its keep, because a
city-level query mentioning GDP per capita has to be rejected by name rather
than quietly returning nothing.

v1 has no time series, so one row per country is stored: the most recent year
the World Bank has a non-empty value for (`mrnev=1`). The year travels with the
value, so "GDP per capita" is never silently a mix of vintages without saying
which one each country came from.
"""

from __future__ import annotations

import logging

import httpx

from atlasql import db
from atlasql.etl import availability
from atlasql.etl.metrics import MetricRow, upsert_metrics
from atlasql.etl.natural_earth import COUNTRY_SOURCE
from atlasql.etl.regions import existing_ids

log = logging.getLogger(__name__)

INDICATOR = "NY.GDP.PCAP.CD"
API_URL = f"https://api.worldbank.org/v2/country/all/indicator/{INDICATOR}"
COUNTRY_URL = "https://api.worldbank.org/v2/country"

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


def fetch_gdp_per_capita() -> list[dict]:
    """Fetch the most recent non-empty GDP per capita value for every economy."""
    params = {
        "format": "json",
        "per_page": "500",
        "mrnev": "1",  # most recent non-empty value, one row per economy
    }
    response = httpx.get(API_URL, params=params, timeout=60.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError(f"unexpected World Bank response shape: {payload!r:.200}")

    header, rows = payload[0], payload[1]
    if header.get("total", 0) > len(rows):
        raise RuntimeError(
            f"World Bank returned {len(rows)} of {header['total']} rows; "
            "raise per_page or paginate"
        )
    log.info("fetched %d World Bank rows for %s", len(rows), INDICATOR)
    return rows


def fetch_aggregate_codes() -> set[str]:
    """Codes the World Bank uses for aggregates rather than places.

    "World", "Euro area" and "Middle income" arrive through the same indicator
    endpoint as real economies but carry region id "NA" in the country
    catalogue. They have no region and never will, so separating them here
    keeps the unmatched list meaningful instead of 40 lines of noise.
    """
    response = httpx.get(
        COUNTRY_URL, params={"format": "json", "per_page": "500"}, timeout=60.0
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError("unexpected World Bank country catalogue response")
    return {
        row["id"].strip().upper()
        for row in payload[1]
        if (row.get("region") or {}).get("id", "").strip() == "NA"
    }


def import_gdp_per_capita() -> None:
    """Import GDP per capita onto country regions. Safe to re-run."""
    rows = fetch_gdp_per_capita()
    aggregates = fetch_aggregate_codes()

    with db.connect() as conn:
        db.register_metric(
            conn,
            metric_name=METRIC_NAME,
            label="GDP per capita",
            unit="current US$",
            description=(
                "Gross domestic product per capita in current US dollars, most "
                "recent year available per country (World Bank NY.GDP.PCAP.CD)."
            ),
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
                unmatched.append(f"{raw_code} ({row.get('country', {}).get('value')})")
                continue
            metric_rows.append(
                {
                    "region_id": region_id,
                    "metric_name": METRIC_NAME,
                    "value": float(value),
                    "year": int(row["date"]),
                }
            )

        written = upsert_metrics(conn, metric_rows)
        # In the same transaction as the write: coverage that lags the data it
        # describes would silently change which level a query runs at.
        availability.refresh(conn)

    log.info(
        "GDP per capita: %d countries covered of %d (%.0f%%)",
        written,
        len(country_ids),
        100 * written / max(len(country_ids), 1),
    )
    if unmatched:
        log.warning(
            "%d World Bank economies had no matching country region, add them "
            "to ISO3_ALIASES if they should have one: %s",
            len(unmatched),
            ", ".join(sorted(unmatched)),
        )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_gdp_per_capita()
