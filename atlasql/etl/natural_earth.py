"""Import country boundaries from Natural Earth, plus the continents above them.

Natural Earth is public domain, which is why the plan picks it over GADM for
the country and state tiers.

Two levels come out of one file. The continent tier is built by dissolving the
admin-0 layer on its CONTINENT attribute, so every country gets a parent and
the hierarchy is connected from the first import rather than being backfilled
later.

Source ids:
    country   -> ADM0_A3 (three letter code, populated for every feature)
    continent -> the Natural Earth continent name

ADM0_A3 is used rather than ISO_A3 because Natural Earth leaves ISO_A3 as '-99'
for several features (France and Norway among them) while ADM0_A3 is always
filled in. It is also the code the World Bank indicator import joins on.
"""

from __future__ import annotations

import logging

import geopandas as gpd

from atlasql import db
from atlasql.etl import download
from atlasql.etl.regions import RegionRow, upsert_regions

log = logging.getLogger(__name__)

ADMIN0_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"
ADMIN0_MIRRORS = [
    "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_admin_0_countries.zip",
]

COUNTRY_SOURCE = "natural_earth_admin0"
CONTINENT_SOURCE = "natural_earth_continent"

# Natural Earth files remote islands under this pseudo continent. It is not a
# continent, so it never becomes a region.
NOT_A_CONTINENT = "Seven seas (open ocean)"

# ...which would otherwise leave those features parentless, including sovereign
# states like the Maldives. A hole in the hierarchy is worse than an explicit
# assignment, so each one is placed by hand on the conventional continent.
CONTINENT_OVERRIDES = {
    "IOT": "Asia",           # British Indian Ocean Territory
    "CLP": "North America",  # Clipperton Island (administered by France)
    "ATF": "Antarctica",     # French Southern and Antarctic Lands
    "HMD": "Antarctica",     # Heard Island and McDonald Islands
    "MDV": "Asia",           # Maldives
    "MUS": "Africa",         # Mauritius
    "SHN": "Africa",         # Saint Helena
    "SYC": "Africa",         # Seychelles
    "SGS": "Antarctica",     # South Georgia and the South Sandwich Islands
}


def load_admin0() -> gpd.GeoDataFrame:
    """Download (once) and read the Natural Earth admin-0 layer in EPSG:4326."""
    archive = download.fetch(
        ADMIN0_URL, filename="ne_10m_admin_0_countries.zip", mirrors=ADMIN0_MIRRORS
    )
    directory = download.unzip(archive, subdir="ne_10m_admin_0_countries")
    shapefiles = list(directory.glob("*.shp"))
    if len(shapefiles) != 1:
        raise RuntimeError(f"expected exactly one .shp in {directory}, found {shapefiles}")

    gdf = gpd.read_file(shapefiles[0])
    if gdf.crs is None:
        raise RuntimeError("Natural Earth layer has no CRS")
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    log.info("read %d admin-0 features", len(gdf))
    return gdf


def _country_name(row) -> str:
    """Prefer the English name, fall back to the admin name."""
    for column in ("NAME_EN", "ADMIN", "NAME"):
        value = row.get(column)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise RuntimeError(f"no usable name for feature {row.get('ADM0_A3')!r}")


def import_countries() -> None:
    """Import continents and countries. Safe to re-run."""
    gdf = load_admin0()

    missing = [c for c in ("ADM0_A3", "CONTINENT", "geometry") if c not in gdf.columns]
    if missing:
        raise RuntimeError(f"Natural Earth layer missing expected columns: {missing}")

    duplicates = gdf["ADM0_A3"].duplicated(keep=False)
    if duplicates.any():
        # Not fatal on its own, but it would silently collapse two countries
        # into one region, so it has to be visible rather than swallowed.
        raise RuntimeError(
            "duplicate ADM0_A3 codes in the source layer: "
            f"{sorted(gdf.loc[duplicates, 'ADM0_A3'].unique())}"
        )

    continents = gdf[gdf["CONTINENT"] != NOT_A_CONTINENT].dissolve(by="CONTINENT")
    log.info("dissolved %d continents from admin-0", len(continents))

    continent_rows: list[RegionRow] = [
        {
            "name": str(name),
            "level": "continent",
            "parent_id": None,
            "source": CONTINENT_SOURCE,
            "source_id": str(name),
            "wkb": row.geometry.wkb,
        }
        for name, row in continents.iterrows()
    ]

    with db.connect() as conn:
        continent_ids = upsert_regions(conn, continent_rows)

        country_rows: list[RegionRow] = []
        orphans: list[str] = []
        for _, row in gdf.iterrows():
            code = str(row["ADM0_A3"])
            continent = CONTINENT_OVERRIDES.get(code, row["CONTINENT"])
            parent_id = continent_ids.get(continent)
            if parent_id is None:
                orphans.append(f"{_country_name(row)} [{code}]")
            country_rows.append(
                {
                    "name": _country_name(row),
                    "level": "country",
                    "parent_id": parent_id,
                    "source": COUNTRY_SOURCE,
                    "source_id": code,
                    "wkb": row["geometry"].wkb,
                }
            )

        upsert_regions(conn, country_rows)

    if orphans:
        # Reachable only if Natural Earth adds a feature under the pseudo
        # continent that CONTINENT_OVERRIDES has not been taught yet.
        log.warning(
            "%d features have no continent parent, add them to "
            "CONTINENT_OVERRIDES: %s",
            len(orphans),
            ", ".join(sorted(orphans)),
        )
    log.info("imported %d countries under %d continents", len(country_rows), len(continent_rows))


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_countries()
