"""Schema invariants the rest of the system relies on."""

from __future__ import annotations

import psycopg
import pytest


def test_postgis_available(schema):
    row = schema.execute("SELECT postgis_version() AS v").fetchone()
    assert row["v"]


def test_level_check_constraint_rejects_unknown_levels(schema):
    with pytest.raises(psycopg.errors.CheckViolation):
        with schema.transaction(force_rollback=True):
            schema.execute(
                "INSERT INTO regions (name, level, source, source_id) "
                "VALUES ('Nowhere', 'planet', 'test', 'planet-1')"
            )


def test_metric_must_be_registered_before_values_are_stored(schema):
    """metrics -> metric_definitions FK is what keeps /metadata honest."""
    with schema.transaction(force_rollback=True):
        region = schema.execute(
            "INSERT INTO regions (name, level, source, source_id) "
            "VALUES ('Testland', 'country', 'test', 'TST') RETURNING id"
        ).fetchone()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with schema.transaction(force_rollback=True):
                schema.execute(
                    "INSERT INTO metrics (region_id, metric_name, value, year) "
                    "VALUES (%s, 'not_a_registered_metric', 1.0, 2020)",
                    (region["id"],),
                )


def test_region_upsert_is_idempotent_on_source_key(schema):
    from atlasql.etl.regions import upsert_regions

    row = {
        "name": "Testland",
        "level": "country",
        "parent_id": None,
        "source": "test_source",
        "source_id": "TST",
        "wkb": None,
    }
    with schema.transaction(force_rollback=True):
        first = upsert_regions(schema, [row])
        renamed = dict(row, name="Testland Renamed")
        second = upsert_regions(schema, [renamed])
        assert first["TST"] == second["TST"], "re-import must not mint a new id"
        stored = schema.execute(
            "SELECT name FROM regions WHERE id = %s", (first["TST"],)
        ).fetchone()
        assert stored["name"] == "Testland Renamed"
