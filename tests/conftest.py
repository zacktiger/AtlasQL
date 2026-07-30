"""Shared test fixtures.

Tests that touch the database skip rather than fail when it is not running, so
`python -m pytest` is useful without docker-compose up.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from atlasql import config, db


@pytest.fixture(scope="session")
def conn():
    try:
        connection = psycopg.connect(config.DATABASE_URL, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"database not reachable at {config.DATABASE_URL}: {exc}")
    connection.row_factory = dict_row
    with connection:
        yield connection


@pytest.fixture(scope="session")
def schema(conn):
    db.apply_schema()
    return conn
