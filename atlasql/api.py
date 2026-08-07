"""HTTP surface: GET /metadata and POST /query.

Both read the live database. /metadata in particular is generated from the
metric registry and the coverage table, never from a hardcoded list, so adding
a metric through an ETL job makes it queryable and selectable in the UI without
a code change or a frontend deploy.
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from atlasql import config, db, geometry, parser, query
from atlasql.models import GeoFilter, QueryResult

log = logging.getLogger(__name__)

def _warm() -> None:
    """Pay the first-request costs before a user arrives.

    A cold process spends about 2.8 s on its first basemap — opening the pool,
    importing the driver, building and compressing half a megabyte of outlines —
    and about 1.9 s on its first /metadata, almost all of it resolving Anthropic
    credentials. Both are once per process, and on a platform that stops idle
    instances the unlucky user paying them is a real one.

    Best-effort by design: it runs on a daemon thread so startup never blocks,
    and every failure is logged and dropped. A database that is not up yet must
    not stop the app from booting — the endpoints will report that themselves,
    with a better error than a failed startup would give.
    """
    try:
        with db.connect() as conn:
            level, tolerance = config.WARM_BASEMAP_LEVEL, config.WARM_BASEMAP_TOLERANCE
            digest = geometry.basemap_digest(conn, level, tolerance)
            collection = geometry.basemap(conn, level, tolerance)
            _BASEMAP_CACHE.put((level, tolerance), _CachedBasemap.build(digest, collection))
        parser.is_configured()
        log.info("warmed the connection pool, the %s basemap and credentials", level)
    except Exception as exc:  # noqa: BLE001 - warming must never break startup
        log.warning("warm-up skipped: %s", exc)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    if config.WARM_ON_STARTUP:
        threading.Thread(target=_warm, name="atlasql-warm", daemon=True).start()
    yield
    db.close_pool()


app = FastAPI(
    title="AtlasQL",
    description="A query engine over the world's administrative hierarchy.",
    version="0.1.0",
    lifespan=_lifespan,
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


@dataclass(frozen=True)
class _CachedBasemap:
    """One built basemap, stored both ways so neither is recomputed."""

    digest: str
    raw: bytes
    gzipped: bytes

    @classmethod
    def build(cls, digest: str, collection: dict) -> _CachedBasemap:
        raw = json.dumps(collection, separators=(",", ":")).encode()
        return cls(digest=digest, raw=raw, gzipped=gzip.compress(raw, 6))


class _BasemapCache:
    """Tiny LRU of built basemaps, keyed by (level, tolerance).

    Bounded on both axes because tolerance is a caller-supplied float, so the
    key space is effectively unbounded and an entry can be large: the country
    tier is half a megabyte at the tolerance the UI asks for, and a request for
    full resolution at a fine tier is tens of megabytes. Oversized payloads are
    served but not stored, which keeps the ceiling on resident memory a
    property of this class rather than of what a client chose to ask for.
    """

    MAX_ENTRIES = 4
    MAX_ENTRY_BYTES = 8 * 1024 * 1024

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, float], _CachedBasemap] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, float], digest: str) -> _CachedBasemap | None:
        with self._lock:
            entry = self._entries.get(key)
            # A digest mismatch means an ETL run replaced the geometry under us.
            if entry is None or entry.digest != digest:
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, key: tuple[str, float], entry: _CachedBasemap) -> None:
        if len(entry.raw) > self.MAX_ENTRY_BYTES:
            return
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.MAX_ENTRIES:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_BASEMAP_CACHE = _BasemapCache()


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

    Building the payload is the expensive part — simplifying 258 country
    polygons and serialising them measured 516 ms, plus 37 ms to gzip — so it
    is cached in the process against the digest. The digest is still computed
    per request, from the raw geometry, exactly as before: the 28 ms that costs
    is what keeps the guarantee above intact, and a reimport invalidates the
    cache on the very next request rather than after some timeout.
    """
    if level not in config.LEVELS:
        raise HTTPException(status_code=422, detail=f"unknown level: {level}")

    tolerance = geometry.clamp_tolerance(tolerance)
    with db.connect() as conn:
        digest = geometry.basemap_digest(conn, level, tolerance)
        etag = f'"{digest}"'
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)

        entry = _BASEMAP_CACHE.get((level, tolerance), digest)
        if entry is None:
            collection = geometry.basemap(conn, level, tolerance)
            entry = _CachedBasemap.build(digest, collection)
            _BASEMAP_CACHE.put((level, tolerance), entry)

    # Starlette's GZipMiddleware leaves a response that already declares an
    # encoding alone, so serving the stored gzip skips recompressing an
    # unchanged half-megabyte on every page load.
    if "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
        return Response(entry.gzipped, media_type="application/json", headers=headers)
    return Response(entry.raw, media_type="application/json", headers=headers)


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
