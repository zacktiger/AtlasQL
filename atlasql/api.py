"""HTTP surface: GET /metadata and POST /query.

Both read the live database. /metadata in particular is generated from the
metric registry and the coverage table, never from a hardcoded list, so adding
a metric through an ETL job makes it queryable and selectable in the UI without
a code change or a frontend deploy.
"""

from __future__ import annotations

import hashlib
import json
import logging

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from atlasql import config, db, geometry, parser, query
from atlasql.models import GeoFilter, QueryResult

log = logging.getLogger(__name__)

app = FastAPI(
    title="AtlasQL",
    description="A query engine over the world's administrative hierarchy.",
    version="0.1.0",
)

# GeoJSON is repetitive text and compresses about four to one. Without this the
# world basemap is a multi-hundred-kilobyte download on every page load.
app.add_middleware(GZipMiddleware, minimum_size=1024)


class MetricInfo(BaseModel):
    name: str
    label: str
    unit: str | None
    description: str | None
    source: str | None
    # Coverage per level, so a client can grey out combinations that would be
    # rejected rather than letting a user build a query that cannot run.
    coverage_pct: dict[str, float]
    levels_with_data: list[str]


class LevelInfo(BaseModel):
    name: str
    region_count: int


class Metadata(BaseModel):
    levels: list[LevelInfo]
    metrics: list[MetricInfo]
    coverage_threshold_pct: float
    # Lets the UI hide the natural language box when no API key is configured,
    # rather than offering a button that always fails.
    natural_language_enabled: bool


class ParseRequest(BaseModel):
    text: str


class ParseResult(BaseModel):
    text: str
    # The filter is returned for review, never executed here. Running it is a
    # separate, explicit /query call.
    filter: GeoFilter


@app.exception_handler(query.QueryError)
async def _query_error_handler(_, exc: query.QueryError) -> JSONResponse:
    # 422: the request is well formed but cannot be answered against the data
    # that exists. The body names the metric responsible.
    return JSONResponse(status_code=422, content=exc.as_dict())


@app.get("/health")
def health() -> dict:
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/metadata", response_model=Metadata)
def metadata() -> Metadata:
    with db.connect() as conn:
        registry = query.registered_metrics(conn)
        coverage = query.coverage_map(conn)
        level_rows = conn.execute(
            "SELECT level, count(*) AS n FROM regions GROUP BY level"
        ).fetchall()

    counts = {row["level"]: row["n"] for row in level_rows}
    levels = [
        LevelInfo(name=level, region_count=counts[level])
        for level in config.LEVELS
        if level in counts
    ]
    metrics = [
        MetricInfo(
            name=name,
            label=info["label"],
            unit=info["unit"],
            description=info["description"],
            source=info["source"],
            coverage_pct=coverage.get(name, {}),
            levels_with_data=[
                level
                for level in config.LEVELS
                if coverage.get(name, {}).get(level, 0.0) >= config.COVERAGE_THRESHOLD_PCT
            ],
        )
        for name, info in registry.items()
    ]
    return Metadata(
        levels=levels,
        metrics=metrics,
        coverage_threshold_pct=config.COVERAGE_THRESHOLD_PCT,
        natural_language_enabled=parser.is_configured(),
    )


@app.post("/query", response_model=QueryResult)
def run_query(geo_filter: GeoFilter) -> QueryResult:
    return query.run(geo_filter)


def _parse_ids(ids: str) -> list[int]:
    parts = [part for part in ids.split(",") if part.strip()]
    if len(parts) > geometry.MAX_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"at most {geometry.MAX_IDS} region ids per request",
        )
    try:
        return [int(part) for part in parts]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be integers") from None


@app.get("/geometry")
def region_geometry(
    ids: str = Query(description="Comma-separated region ids, as returned by /query."),
    tolerance: float = Query(
        default=0.05,
        description="Simplification tolerance in degrees. 0 is full resolution.",
    ),
) -> JSONResponse:
    """GeoJSON for regions /query already returned.

    Split from /query on purpose: the same result set is drawn at whatever
    detail the current zoom justifies, and asking for more detail must not mean
    re-running the query.
    """
    with db.connect() as conn:
        collection = geometry.for_regions(conn, _parse_ids(ids), tolerance)
    return JSONResponse(collection)


@app.get("/geometry/basemap")
def basemap_geometry(
    request: Request,
    level: str = Query(default="country"),
    tolerance: float = Query(default=0.25),
) -> Response:
    """Coarse outlines for the whole world at one level: the map's context.

    Drawn from our own regions table rather than a tile service, so the
    coastlines on screen are the same boundaries the query ran against.

    This is a few hundred kilobytes and worth not re-sending, but it is cached
    by revalidation rather than by expiry. A freshness lifetime would let a
    browser keep serving outlines an ETL reimport has already replaced, and a
    map disagreeing with the table beside it is exactly the kind of silent
    wrongness this codebase avoids elsewhere. An ETag costs one round trip and
    makes the repeat case a 304.
    """
    if level not in config.LEVELS:
        raise HTTPException(status_code=422, detail=f"unknown level: {level}")
    with db.connect() as conn:
        collection = geometry.basemap(conn, level, tolerance)

    payload = json.dumps(collection, separators=(",", ":"))
    etag = f'"{hashlib.sha256(payload.encode()).hexdigest()[:32]}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(payload, media_type="application/json", headers=headers)


@app.post("/parse", response_model=ParseResult)
def parse_text(request: ParseRequest) -> ParseResult:
    """Natural language in, a GeoFilter out. Deliberately does not execute it.

    The filter comes back for the user to review and edit in the same form they
    would have filled in by hand; running it is a separate /query call. A
    misparse is then something they see and correct, rather than something they
    discover in a result set three steps later.
    """
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="empty query text")
    try:
        return ParseResult(text=text, filter=parser.parse(text))
    except parser.ParserUnavailable as exc:
        # 503 rather than 500: the service is fine, this feature is unconfigured.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except parser.ParseFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class _RevalidatingStaticFiles(StaticFiles):
    """Static files that must be revalidated rather than assumed fresh.

    Without a Cache-Control header a browser is free to guess how long a
    response stays fresh, and they guess generously. The result is a user
    holding yesterday's app.js against today's index.html after a deploy -
    which fails in confusing ways, because the mismatch is silent. ETag and
    Last-Modified are still sent, so revalidation is a cheap 304 rather than a
    re-download.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# The query builder is static files with no build step, served from the same
# origin as the API so there is one process to run and no CORS to configure.
# Mounted last so it cannot shadow an API route.
_FRONTEND_DIR = config.REPO_ROOT / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount(
        "/", _RevalidatingStaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend"
    )
else:  # pragma: no cover - only when running from a partial checkout
    log.warning("frontend directory %s not found; API only", _FRONTEND_DIR)
