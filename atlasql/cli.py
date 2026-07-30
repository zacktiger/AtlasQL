"""Command line entry point for setup and ETL jobs.

    python -m atlasql.cli init-db
    python -m atlasql.cli import-natural-earth
    python -m atlasql.cli import-world-bank
    python -m atlasql.cli refresh-availability
"""

from __future__ import annotations

import argparse
import logging

from atlasql import db


def _init_db(_: argparse.Namespace) -> None:
    db.apply_schema()


def _import_natural_earth(_: argparse.Namespace) -> None:
    from atlasql.etl import natural_earth

    natural_earth.import_countries()


def _import_world_bank(_: argparse.Namespace) -> None:
    from atlasql.etl import world_bank

    world_bank.import_gdp_per_capita()


def _refresh_availability(_: argparse.Namespace) -> None:
    from atlasql.etl import availability

    availability.refresh()


COMMANDS = {
    "init-db": (_init_db, "Apply the SQL schema (idempotent)"),
    "import-natural-earth": (
        _import_natural_earth,
        "Import Natural Earth continents and countries",
    ),
    "import-world-bank": (
        _import_world_bank,
        "Import World Bank GDP per capita onto countries",
    ),
    "refresh-availability": (
        _refresh_availability,
        "Recompute metric_availability from what is actually stored",
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlasql")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log at DEBUG level"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text) in COMMANDS.items():
        subparsers.add_parser(name, help=help_text)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    handler, _ = COMMANDS[args.command]
    handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
