"""HTTP surface: GET /metadata and POST /query.

Both read the live database. /metadata in particular is generated from the
metric registry and the coverage table, never from a hardcoded list, so adding
a metric through an ETL job makes it queryable and selectable in the UI without
a code change or a frontend deploy.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from atlasql import config, db, parser, query
from atlasql.models import GeoFilter, QueryResult

log = logging.getLogger(__name__)

app = FastAPI(
    title="AtlasQL",
    description="A query engine over the world's administrative hierarchy.",
    version="0.1.0",
)


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
