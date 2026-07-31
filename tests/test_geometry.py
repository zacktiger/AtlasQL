"""Region geometry as GeoJSON, and the map that draws it."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from atlasql import geometry
from atlasql.api import app


@pytest.fixture(scope="module")
def client(schema):
    n = schema.execute(
        "SELECT count(*) AS n FROM regions WHERE level = 'country'"
    ).fetchone()["n"]
    if n == 0:
        pytest.skip("no countries imported; run import-natural-earth")
    return TestClient(app)


@pytest.fixture(scope="module")
def country_ids(schema):
    rows = schema.execute(
        "SELECT id FROM regions WHERE level = 'country' AND geom IS NOT NULL LIMIT 5"
    ).fetchall()
    return [row["id"] for row in rows]


def _signed_area(ring: list[list[float]]) -> float:
    """Shoelace area. Positive is counterclockwise in lon/lat order."""
    return (
        sum(
            ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
            for i in range(len(ring) - 1)
        )
        / 2
    )


def _exterior_rings(geojson: dict):
    if geojson["type"] == "Polygon":
        yield geojson["coordinates"][0]
    elif geojson["type"] == "MultiPolygon":
        for polygon in geojson["coordinates"]:
            yield polygon[0]


def test_exterior_rings_wind_clockwise_for_the_globe(client, country_ids):
    """The invariant the globe depends on, and it is the counter-intuitive one.

    d3-geo takes the clockwise side of an exterior ring as the interior, which
    is the opposite of RFC 7946. Wind these counterclockwise "correctly" and
    every country becomes the whole planet with a country-shaped hole in it —
    a failure that looks like a broken projection rather than like bad data,
    which is exactly why it is pinned here.
    """
    body = client.get(
        f"/geometry?ids={','.join(str(i) for i in country_ids)}"
    ).json()
    assert body["features"]
    for feature in body["features"]:
        for ring in _exterior_rings(feature["geometry"]):
            assert _signed_area(ring) < 0, feature["properties"]["name"]


def test_geometry_is_returned_for_the_requested_ids(client, country_ids):
    body = client.get(
        f"/geometry?ids={','.join(str(i) for i in country_ids)}"
    ).json()
    assert body["type"] == "FeatureCollection"
    assert {f["properties"]["region_id"] for f in body["features"]} == set(country_ids)
    for feature in body["features"]:
        assert feature["properties"]["kind"] == "polygon"
        assert feature["properties"]["name"]


def test_a_coarser_tolerance_returns_less_data(client, country_ids):
    ids = ",".join(str(i) for i in country_ids)
    detailed = client.get(f"/geometry?ids={ids}&tolerance=0.005").json()
    coarse = client.get(f"/geometry?ids={ids}&tolerance=0.5").json()
    assert len(json.dumps(coarse)) < len(json.dumps(detailed)) / 2


def test_cities_come_back_as_points_not_missing_shapes(client, schema):
    row = schema.execute("SELECT id FROM regions WHERE level = 'city' LIMIT 1").fetchone()
    if row is None:
        pytest.skip("no cities imported; run import-cities")
    body = client.get(f"/geometry?ids={row['id']}").json()
    feature = body["features"][0]
    # Cities are point data, so the map draws a marker. Saying so in the
    # feature beats making the client sniff the geometry type.
    assert feature["properties"]["kind"] == "point"
    assert feature["geometry"]["type"] == "Point"


def test_basemap_covers_the_whole_level(client, schema):
    expected = schema.execute(
        "SELECT count(*) AS n FROM regions WHERE level = 'country' AND geom IS NOT NULL"
    ).fetchone()["n"]
    body = client.get("/geometry/basemap?level=country").json()
    assert len(body["features"]) == expected


def test_basemap_is_revalidated_rather_than_assumed_fresh(client):
    """A cached basemap must not outlive the boundaries it was drawn from.

    With a freshness lifetime, a browser keeps serving outlines that an ETL
    reimport has already replaced, and the map quietly disagrees with the table
    beside it. Revalidation makes the repeat request a 304 instead.
    """
    first = client.get("/geometry/basemap?level=country")
    assert first.headers["cache-control"] == "no-cache"
    etag = first.headers["etag"]

    again = client.get(
        "/geometry/basemap?level=country", headers={"If-None-Match": etag}
    )
    assert again.status_code == 304


def test_basemap_rejects_a_level_that_does_not_exist(client):
    assert client.get("/geometry/basemap?level=province").status_code == 422


def test_too_many_ids_is_refused(client):
    ids = ",".join(str(i) for i in range(geometry.MAX_IDS + 1))
    response = client.get(f"/geometry?ids={ids}")
    assert response.status_code == 422


def test_non_numeric_ids_are_refused(client):
    assert client.get("/geometry?ids=1,%27;DROP%20TABLE%20regions").status_code == 422


def test_unknown_ids_yield_no_features_rather_than_an_error(client):
    body = client.get("/geometry?ids=999999999").json()
    assert body["features"] == []


def test_tolerance_is_clamped_rather_than_trusted():
    assert geometry.clamp_tolerance(-5) == geometry.MIN_TOLERANCE
    assert geometry.clamp_tolerance(1e6) == geometry.MAX_TOLERANCE
