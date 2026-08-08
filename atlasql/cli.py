"""Command line entry point for setup and ETL jobs.

    python -m atlasql.cli init-db
    python -m atlasql.cli import-natural-earth
    python -m atlasql.cli import-states
    python -m atlasql.cli import-world-bank
    python -m atlasql.cli import-gridded-gdp --level state
    python -m atlasql.cli import-elevation --level state
    python -m atlasql.cli import-subregions --level country
    python -m atlasql.cli refresh-availability

Metric jobs take --level, because the same job computes a tier's metrics
whatever the tier is. Adding a tier is running them again with a different
argument, not writing new code.
"""

from __future__ import annotations

import argparse
import logging

from atlasql import config, db


def _init_db(_: argparse.Namespace) -> None:
    db.apply_schema()


def _import_natural_earth(_: argparse.Namespace) -> None:
    from atlasql.etl import natural_earth

    natural_earth.import_countries()


def _import_states(_: argparse.Namespace) -> None:
    from atlasql.etl import natural_earth

    natural_earth.import_states()


def _import_counties(_: argparse.Namespace) -> None:
    from atlasql.etl import geo_boundaries

    geo_boundaries.import_counties()


def _import_cities(_: argparse.Namespace) -> None:
    from atlasql.etl import geonames

    geonames.import_cities()


def _import_world_bank(_: argparse.Namespace) -> None:
    from atlasql.etl import world_bank

    world_bank.import_indicators()


def _import_gridded_gdp(args: argparse.Namespace) -> None:
    from atlasql.etl import gridded_gdp

    gridded_gdp.import_gridded_gdp(level=args.level)


def _import_elevation(args: argparse.Namespace) -> None:
    from atlasql.etl import elevation

    elevation.import_elevation(level=args.level)


def _import_rivers(args: argparse.Namespace) -> None:
    from atlasql.etl import rivers

    rivers.import_rivers(level=args.level)


def _import_coastline(args: argparse.Namespace) -> None:
    from atlasql.etl import coastline

    coastline.import_coastline(level=args.level)


def _import_subregions(args: argparse.Namespace) -> None:
    from atlasql.etl import subregions

    subregions.import_subregions(level=args.level)


def _refresh_availability(_: argparse.Namespace) -> None:
    from atlasql.etl import availability

    availability.refresh()


def _serving_size(_: argparse.Namespace) -> None:
    """Report what a deployed database actually has to hold.

    Most of a built AtlasQL database is HydroRIVERS staging — 8.5 million line
    segments the rivers job aggregates against and nothing reads afterwards. It
    is worth knowing that number before paying to host it, because the tables
    the API reads are a small fraction of the total and fit on tiers the whole
    database does not.
    """
    from atlasql.etl import rivers

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT c.relname AS name,
                   pg_total_relation_size(c.oid) AS bytes,
                   pg_size_pretty(pg_total_relation_size(c.oid)) AS pretty
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY pg_total_relation_size(c.oid) DESC
            """
        ).fetchall()

    staging_tables = {rivers.STAGING_TABLE, rivers.STATE_TABLE}
    served, staged = 0, 0
    print(f"{'table':<28}{'size':>12}   read by the API?")
    print("-" * 62)
    for row in rows:
        is_staging = row["name"] in staging_tables
        # spatial_ref_sys is PostGIS's own; it comes with the extension rather
        # than with a dump of ours.
        is_postgis = row["name"] == "spatial_ref_sys"
        if is_staging:
            staged += row["bytes"]
        elif not is_postgis:
            served += row["bytes"]
        note = "no - ETL staging" if is_staging else ("postgis" if is_postgis else "yes")
        print(f"{row['name']:<28}{row['pretty']:>12}   {note}")

    print("-" * 62)
    print(f"{'needed to serve':<28}{_human(served):>12}")
    print(f"{'ETL staging, excludable':<28}{_human(staged):>12}")
    excludes = " ".join(f"--exclude-table={name}" for name in sorted(staging_tables))
    print(
        "\nDump only what is served with:\n"
        f"  pg_dump --no-owner --no-privileges {excludes} "
        "-Fc <source-url> > atlasql.dump"
    )


def _human(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# Which levels a metric job's --level accepts. Most measure something the world
# has at a place, and continents are dissolved from countries and carry none of
# it, so they take every level below that. The subregion counts are the
# exception: what they measure is the hierarchy itself, which a continent has as
# much of as anything else, and it is the only level where country_count exists.
MEASURED_LEVELS = tuple(level for level in config.LEVELS if level != "continent")
ALL_LEVELS = config.LEVELS
# Anything measured off an outline. Cities are point data and have none.
POLYGON_LEVELS = tuple(level for level in config.LEVELS if level != "city")

# name -> (handler, help, --level choices or None)
COMMANDS = {
    "init-db": (_init_db, "Apply the SQL schema (idempotent)", None),
    "import-natural-earth": (
        _import_natural_earth,
        "Import Natural Earth continents and countries",
        None,
    ),
    "import-states": (
        _import_states,
        "Import Natural Earth states and provinces under their countries",
        None,
    ),
    "import-counties": (
        _import_counties,
        "Import geoBoundaries ADM2 counties under their states",
        None,
    ),
    "import-cities": (
        _import_cities,
        "Import GeoNames cities with population, parented spatially",
        None,
    ),
    "import-world-bank": (
        _import_world_bank,
        "Import World Bank indicators (GDP per capita, population) onto countries",
        None,
    ),
    "import-gridded-gdp": (
        _import_gridded_gdp,
        "Aggregate GDP per capita (PPP) from the Kummu grid for one level",
        MEASURED_LEVELS,
    ),
    "import-elevation": (
        _import_elevation,
        "Compute mean/min/max elevation from GMTED2010 for one level",
        MEASURED_LEVELS,
    ),
    "import-rivers": (
        _import_rivers,
        "Stage HydroRIVERS and aggregate river length/count for one level",
        MEASURED_LEVELS,
    ),
    "import-coastline": (
        _import_coastline,
        "Measure the boundary facing open water for one level (no download)",
        POLYGON_LEVELS,
    ),
    "import-subregions": (
        _import_subregions,
        "Count the regions of each lower tier inside each region at one level",
        ALL_LEVELS,
    ),
    "refresh-availability": (
        _refresh_availability,
        "Recompute metric_availability from what is actually stored",
        None,
    ),
    "serving-size": (
        _serving_size,
        "Report which tables a deployment must carry, and how big they are",
        None,
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlasql")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log at DEBUG level"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text, level_choices) in COMMANDS.items():
        subparser = subparsers.add_parser(name, help=help_text)
        if level_choices:
            subparser.add_argument(
                "--level",
                default="country",
                choices=list(level_choices),
                help="which tier to compute the metric for (default: country)",
            )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    handler, _, _ = COMMANDS[args.command]
    handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
