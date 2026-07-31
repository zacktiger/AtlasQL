"""The HTTP surface: /metadata and /query."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atlasql.api import app


@pytest.fixture(scope="module")
def client(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM metrics WHERE metric_name = 'gdp_per_capita'"
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("GDP not imported; run import-world-bank")
    return TestClient(app)


def test_metadata_is_generated_from_the_live_registry(client):
    body = client.get("/metadata").json()
    names = {m["name"] for m in body["metrics"]}
    assert "gdp_per_capita" in names
    levels = {level["name"] for level in body["levels"]}
    assert {"country", "continent"} <= levels
    assert body["coverage_threshold_pct"] > 0


def test_metadata_reports_which_levels_a_metric_can_be_queried_at(client):
    body = client.get("/metadata").json()
    gdp = next(m for m in body["metrics"] if m["name"] == "gdp_per_capita")
    assert gdp["levels_with_data"] == ["country"]
    assert gdp["coverage_pct"]["country"] > 80
    assert gdp["label"] and gdp["unit"]


def test_query_returns_a_ranked_top_n(client):
    response = client.post(
        "/query",
        json={
            "conditions": [{"metric": "gdp_per_capita", "op": ">", "value": 40000}],
            "top_n": 5,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["level"] == "country"
    assert body["level_chosen_by"] == "auto"
    values = [row["metrics"]["gdp_per_capita"]["value"] for row in body["results"]]
    assert len(values) == 5
    assert values == sorted(values, reverse=True)


def test_query_without_coverage_returns_422_naming_the_metric(client):
    response = client.post(
        "/query",
        json={
            "level": "continent",
            "conditions": [{"metric": "gdp_per_capita", "op": ">", "value": 40000}],
        },
    )
    assert response.status_code == 422
    assert response.json()["blocking_metric"] == "gdp_per_capita"


def test_unknown_metric_returns_422_not_an_empty_result(client):
    response = client.post(
        "/query",
        json={"conditions": [{"metric": "unicorn_density", "op": ">", "value": 1}]},
    )
    assert response.status_code == 422
    assert response.json()["blocking_metric"] == "unicorn_density"


def test_frontend_is_served_without_shadowing_the_api(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "AtlasQL" in page.text
    # The static mount sits at the root, so this guards against it swallowing
    # the API routes.
    assert client.get("/metadata").status_code == 200
    # Without this, browsers heuristically cache the page and a deploy can
    # leave a stale index.html running against a fresh app.js.
    assert page.headers["cache-control"] == "no-cache"
    assert client.get("/app.js").headers["cache-control"] == "no-cache"


def test_malformed_filter_is_rejected_by_the_schema(client):
    response = client.post(
        "/query",
        json={"conditions": [{"metric": "gdp_per_capita", "op": "LIKE", "value": 1}]},
    )
    assert response.status_code == 422
