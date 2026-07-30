"""Metric storage and the coverage table level auto-detection depends on."""

from __future__ import annotations

import pytest

from atlasql.etl import availability
from atlasql.etl.metrics import upsert_metrics
from atlasql.etl.world_bank import METRIC_NAME as GDP


@pytest.fixture(scope="module")
def loaded(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM metrics WHERE metric_name = %s", (GDP,)
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("GDP not imported; run import-world-bank")
    return schema


def test_gdp_lands_only_on_countries(loaded):
    rows = loaded.execute(
        """
        SELECT DISTINCT r.level FROM metrics m
        JOIN regions r ON r.id = m.region_id
        WHERE m.metric_name = %s
        """,
        (GDP,),
    ).fetchall()
    assert [r["level"] for r in rows] == ["country"]


def test_gdp_values_are_plausible(loaded):
    row = loaded.execute(
        "SELECT min(value) AS lo, max(value) AS hi FROM metrics WHERE metric_name = %s",
        (GDP,),
    ).fetchone()
    assert 100 < row["lo"] < 5_000
    assert 50_000 < row["hi"] < 1_000_000


def test_metric_upsert_overwrites_rather_than_duplicating(schema):
    with schema.transaction(force_rollback=True):
        region = schema.execute(
            "INSERT INTO regions (name, level, source, source_id) "
            "VALUES ('Testland', 'country', 'test', 'TST') RETURNING id"
        ).fetchone()
        schema.execute(
            "INSERT INTO metric_definitions (metric_name, label) "
            "VALUES ('test_metric', 'Test metric')"
        )
        row = {
            "region_id": region["id"],
            "metric_name": "test_metric",
            "value": 1.0,
            "year": 2020,
        }
        upsert_metrics(schema, [row])
        upsert_metrics(schema, [dict(row, value=2.0)])
        stored = schema.execute(
            "SELECT value FROM metrics WHERE region_id = %s AND metric_name = 'test_metric'",
            (region["id"],),
        ).fetchall()
        assert [r["value"] for r in stored] == [2.0]


def test_availability_records_zero_coverage_levels(loaded):
    """A missing row and 0% are different answers; the engine needs the 0%."""
    row = loaded.execute(
        "SELECT coverage_pct FROM metric_availability "
        "WHERE metric_name = %s AND level = 'continent'",
        (GDP,),
    ).fetchone()
    assert row is not None, "no availability row for a level that has regions"
    assert row["coverage_pct"] == 0


def test_availability_matches_what_is_actually_stored(loaded):
    availability.refresh(loaded)
    row = loaded.execute(
        """
        SELECT a.coverage_pct,
               100.0 * (SELECT count(DISTINCT m.region_id) FROM metrics m
                        JOIN regions r ON r.id = m.region_id
                        WHERE m.metric_name = %(metric)s AND r.level = 'country'
                          AND m.value IS NOT NULL)
                     / (SELECT count(*) FROM regions WHERE level = 'country') AS expected
        FROM metric_availability a
        WHERE a.metric_name = %(metric)s AND a.level = 'country'
        """,
        {"metric": GDP},
    ).fetchone()
    assert row["coverage_pct"] == pytest.approx(float(row["expected"]))
    loaded.rollback()
