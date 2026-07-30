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
from atlasql.etl import availability, download
from atlasql.etl.regions import RegionRow, existing_ids, upsert_regions

log = logging.getLogger(__name__)

ADMIN0_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"
ADMIN0_MIRRORS = [
    "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_admin_0_countries.zip",
]

ADMIN1_URL = (
    "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip"
)
ADMIN1_MIRRORS = [
    "https://naturalearth.s3.amazonaws.com/10m_cultural/"
    "ne_10m_admin_1_states_provinces.zip",
]

COUNTRY_SOURCE = "natural_earth_admin0"
CONTINENT_SOURCE = "natural_earth_continent"
STATE_SOURCE = "natural_earth_admin1"

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


def _load_layer(url: str, filename: str, mirrors: list[str]) -> gpd.GeoDataFrame:
    """Download (once) and read a Natural Earth layer in EPSG:4326.

    Column names are lowercased because Natural Earth is not consistent about
    their case between the admin-0 and admin-1 layers.
    """
    archive = download.fetch(url, filename=filename, mirrors=mirrors)
    directory = download.unzip(archive, subdir=filename.removesuffix(".zip"))
    shapefiles = list(directory.glob("*.shp"))
    if len(shapefiles) != 1:
        raise RuntimeError(f"expected exactly one .shp in {directory}, found {shapefiles}")

    gdf = gpd.read_file(shapefiles[0])
    if gdf.crs is None:
        raise RuntimeError("Natural Earth layer has no CRS")
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    gdf.columns = [c if c == "geometry" else c.lower() for c in gdf.columns]
    log.info("read %d features from %s", len(gdf), filename)
    return gdf


def load_admin0() -> gpd.GeoDataFrame:
    """The Natural Earth admin-0 (country) layer."""
    return _load_layer(ADMIN0_URL, "ne_10m_admin_0_countries.zip", ADMIN0_MIRRORS)


def load_admin1() -> gpd.GeoDataFrame:
    """The Natural Earth admin-1 (state/province) layer."""
    return _load_layer(
        ADMIN1_URL, "ne_10m_admin_1_states_provinces.zip", ADMIN1_MIRRORS
    )


def _feature_name(row, columns: tuple[str, ...], key: str) -> str:
    """Prefer the English name, fall back to whatever the layer provides."""
    for column in columns:
        value = row.get(column)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise RuntimeError(f"no usable name for feature {row.get(key)!r}")


def _country_name(row) -> str:
    return _feature_name(row, ("name_en", "admin", "name"), "adm0_a3")


def import_countries() -> None:
    """Import continents and countries. Safe to re-run."""
    gdf = load_admin0()

    missing = [c for c in ("adm0_a3", "continent", "geometry") if c not in gdf.columns]
    if missing:
        raise RuntimeError(f"Natural Earth layer missing expected columns: {missing}")

    duplicates = gdf["adm0_a3"].duplicated(keep=False)
    if duplicates.any():
        # Not fatal on its own, but it would silently collapse two countries
        # into one region, so it has to be visible rather than swallowed.
        raise RuntimeError(
            "duplicate adm0_a3 codes in the source layer: "
            f"{sorted(gdf.loc[duplicates, 'adm0_a3'].unique())}"
        )

    continents = gdf[gdf["continent"] != NOT_A_CONTINENT].dissolve(by="continent")
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
            code = str(row["adm0_a3"])
            continent = CONTINENT_OVERRIDES.get(code, row["continent"])
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
        # Adding regions moves the denominator of every coverage percentage, so
        # a boundary import invalidates availability just as a metric import
        # does.
        availability.refresh(conn)

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


def import_states() -> None:
    """Import the admin-1 tier (states and provinces) worldwide. Safe to re-run.

    Every country's first-level divisions are imported, not one chosen
    country's. The plan asks for one clean example of a state tier, and the
    honest way to get one is to load the layer as it comes: filtering to a
    single country would be a special case in the pipeline, and a tier that
    only works for one country is the thing the vision warns against.

    States hang off the country regions by adm0_a3, the same code the country
    import uses as its source id.
    """
    gdf = load_admin1()

    missing = [c for c in ("adm1_code", "adm0_a3", "geometry") if c not in gdf.columns]
    if missing:
        raise RuntimeError(f"Natural Earth admin-1 layer missing columns: {missing}")

    duplicates = gdf["adm1_code"].duplicated(keep=False)
    if duplicates.any():
        raise RuntimeError(
            "duplicate adm1_code values in the source layer: "
            f"{sorted(gdf.loc[duplicates, 'adm1_code'].unique())[:10]}"
        )

    with db.connect() as conn:
        country_ids = existing_ids(conn, COUNTRY_SOURCE)
        if not country_ids:
            raise RuntimeError("no country regions found; run import-natural-earth first")

        rows: list[RegionRow] = []
        orphans: set[str] = set()
        skipped_empty = 0
        for _, row in gdf.iterrows():
            geometry = row["geometry"]
            if geometry is None or geometry.is_empty:
                # Natural Earth carries a few admin-1 records with no polygon.
                # A region with no geometry cannot take zonal statistics or a
                # river intersection, so it is left out rather than stored as a
                # row that silently never matches anything.
                skipped_empty += 1
                continue
            code = str(row["adm0_a3"])
            parent_id = country_ids.get(code)
            if parent_id is None:
                orphans.add(code)
                continue
            rows.append(
                {
                    "name": _feature_name(row, ("name_en", "name", "adm1_code"), "adm1_code"),
                    "level": "state",
                    "parent_id": parent_id,
                    "source": STATE_SOURCE,
                    "source_id": str(row["adm1_code"]),
                    "wkb": geometry.wkb,
                }
            )

        upsert_regions(conn, rows)
        availability.refresh(conn)

    if skipped_empty:
        log.warning("%d admin-1 features had no geometry and were skipped", skipped_empty)
    if orphans:
        log.warning(
            "%d admin-1 features reference a country with no region: %s",
            len(orphans),
            ", ".join(sorted(orphans)),
        )
    log.info("imported %d states under %d countries", len(rows), len(country_ids))


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import_countries()
