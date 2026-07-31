"""Command line entry point for setup and ETL jobs.

    python -m atlasql.cli init-db
    python -m atlasql.cli import-natural-earth
    python -m atlasql.cli import-states
    python -m atlasql.cli import-world-bank
    python -m atlasql.cli import-elevation --level state
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


def _import_cities(_: argparse.Namespace) -> None:
    from atlasql.etl import geonames

    geonames.import_cities()


def _import_world_bank(_: argparse.Namespace) -> None:
    from atlasql.etl import world_bank

    world_bank.import_indicators()


def _import_elevation(args: argparse.Namespace) -> None:
    from atlasql.etl import elevation

    elevation.import_elevation(level=args.level)


def _import_rivers(args: argparse.Namespace) -> None:
    from atlasql.etl import rivers

    rivers.import_rivers(level=args.level)


def _refresh_availability(_: argparse.Namespace) -> None:
    from atlasql.etl import availability

    availability.refresh()


# name -> (handler, help, takes --level)
COMMANDS = {
    "init-db": (_init_db, "Apply the SQL schema (idempotent)", False),
    "import-natural-earth": (
        _import_natural_earth,
        "Import Natural Earth continents and countries",
        False,
    ),
    "import-states": (
        _import_states,
        "Import Natural Earth states and provinces under their countries",
        False,
    ),
    "import-cities": (
        _import_cities,
        "Import GeoNames cities with population, parented spatially",
        False,
    ),
    "import-world-bank": (
        _import_world_bank,
        "Import World Bank indicators (GDP per capita, population) onto countries",
        False,
    ),
    "import-elevation": (
        _import_elevation,
        "Compute mean/min/max elevation from GMTED2010 for one level",
        True,
    ),
    "import-rivers": (
        _import_rivers,
        "Stage HydroRIVERS and aggregate river length/count for one level",
        True,
    ),
    "refresh-availability": (
        _refresh_availability,
        "Recompute metric_availability from what is actually stored",
        False,
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlasql")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log at DEBUG level"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text, takes_level) in COMMANDS.items():
        subparser = subparsers.add_parser(name, help=help_text)
        if takes_level:
            subparser.add_argument(
                "--level",
                default="country",
                # Every level that holds regions with data of its own. Continents
                # are dissolved from countries and carry no metrics.
                choices=[level for level in config.LEVELS if level != "continent"],
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
