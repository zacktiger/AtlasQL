# AtlasQL

A query engine over the world's administrative hierarchy (continent → country →
state → county → city). You give it numeric conditions — "GDP per capita >
40000 and mean elevation > 500" — and it picks the appropriate level, filters,
ranks, and returns a top-N list.

See `high-level-vision.md` for where this is going and
`geo-query-engine-plan.md` for what v1 is.

## Setup

```bash
docker compose up -d                 # PostGIS on localhost:55432
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt     # macOS/Linux
```

Connection settings default to the docker-compose database. Override with
`ATLASQL_DATABASE_URL` (see `.env.example`).

## ETL

```bash
python -m atlasql.cli init-db                # apply sql/*.sql, idempotent
python -m atlasql.cli import-natural-earth   # continents + countries
python -m atlasql.cli import-states          # states/provinces under countries
python -m atlasql.cli import-cities          # GeoNames cities + population
python -m atlasql.cli import-world-bank      # GDP per capita and population
python -m atlasql.cli import-elevation --level city
python -m atlasql.cli import-elevation --level country
python -m atlasql.cli import-elevation --level state
python -m atlasql.cli import-rivers --level country
python -m atlasql.cli import-rivers --level state
python -m atlasql.cli refresh-availability   # every job already does this
```

The metric jobs take `--level`, because giving a new tier its metrics is
running the same job with a different argument. Nothing in the query engine
knows how many tiers exist.

Every job is idempotent and upserts, so re-running is always safe. Source
archives are cached under `data/raw/` after the first download — around 1.6 GB
of DEM tiles and 0.5 GB of HydroRIVERS, downloaded once.

Each job recomputes `metric_availability` in its own transaction. Coverage that
lags the data it describes does not fail loudly, it silently changes which
level a query runs at.

## App and API

```bash
python -m uvicorn atlasql.api:app --reload   # UI and API on localhost:8000
```

Open <http://localhost:8000/> for the query builder: pick a level (or leave it
on Auto), add conditions, run. The metric and level dropdowns, their units and
their coverage percentages all come from `/metadata`, so a metric added by an
ETL job appears after a reload with no frontend change. Every condition shows
which levels actually have data for it, and a query that cannot be answered
shows the API's refusal naming the blocking metric rather than an empty table.

Results are drawn on a globe as well as in the table. The two are one selection
seen twice: clicking a row turns the globe to that region, clicking a region
highlights its row. Drag spins, scroll zooms, and zooming out always reaches the
whole planet — the projection is orthographic at every scale, so a single
country and the whole world are the same map, not two.

The basemap is our own `regions` table rather than a tile service, so every
coastline on screen is a boundary some query could have returned. Region fills
encode the metric the results are ranked by, on a single-hue sequential ramp.

The frontend is static files served by the same app — no build step, no second
process. `frontend/vendor/` holds d3-geo and d3-array for the projection and
its spherical clipping; see the README there for why they are checked in.

- `GET /metadata` — metrics with per-level coverage, and levels with region
  counts. Generated from the live registry, so adding a metric through an ETL
  job makes it queryable and selectable without a code change.
- `POST /query` — takes a `GeoFilter`, returns a ranked top-N.
- `GET /geometry?ids=…&tolerance=…` — GeoJSON for regions `/query` returned,
  simplified to the detail the current zoom justifies. Separate from `/query`
  so asking for more detail never re-runs the query, and so `GeoFilter` in,
  `QueryResult` out stays the one contract the engine speaks.
- `GET /geometry/basemap?level=…` — coarse outlines for the map's context.
  Revalidated with an ETag rather than given a freshness lifetime: a cached
  basemap must not outlive an ETL reimport.
- `POST /parse` — takes `{"text": "..."}`, returns a `GeoFilter`. **It does not
  execute anything**: the filter comes back for the user to review and edit in
  the same form they would have filled in by hand, and running it is a separate
  `/query` call.

## Natural language

`/parse` needs an Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # or: $env:ANTHROPIC_API_KEY on Windows
```

Without one, everything else works normally and the UI simply doesn't offer the
natural language box — `/metadata` reports `natural_language_enabled: false`
rather than presenting a button that always fails.

Claude's only job is to emit a `GeoFilter`. It never sees the database, never
writes SQL, and never executes anything. The tool schema is generated from the
live metric registry, so the `metric` field is an enum of names that actually
exist, and `tool_choice` is forced so the model cannot answer in prose. Whatever
comes back is re-validated server-side — parsed by the same Pydantic model, then
checked against the registry — before it is shown to the user.

```bash
curl -X POST localhost:8000/query -H 'content-type: application/json' -d '{
  "conditions": [
    {"metric": "gdp_per_capita",  "op": ">", "value": 40000},
    {"metric": "elevation_mean",  "op": ">", "value": 500}
  ],
  "sort_by": "elevation_mean", "top_n": 10
}'
```

A query naming a metric with too little coverage at the level it would run at
comes back as HTTP 422 naming the blocking metric and its actual coverage. That
is the intended answer, not an empty result set.

## What is loaded

| Tier | Regions | Metrics |
|---|---|---|
| Continent | 7 | none yet |
| Country | 258 | GDP per capita, population, elevation (mean/min/max), river length, major rivers |
| State | 4,596 | elevation (mean/min/max), river length, major rivers |
| City | 34,021 | population, elevation (mean) |

The gaps are the interesting part, and each one is a named refusal rather than
an empty table:

- **GDP per capita** exists at country level and nowhere below it, because no
  reliable global subnational source does.
- **River metrics** stop at the state tier. Cities are points; a point contains
  no rivers.
- **Elevation minimum and maximum** stop at the state tier too. A point has no
  range, so cities carry only `elevation_mean`, sampled at their location.
- **Population** spans city and country but not state, so a query combining it
  with GDP resolves to country while population alone reaches cities.

## Tests

```bash
python -m pytest
```

Tests that need the database skip automatically when it is not running.

## Layout

```
sql/               schema migrations, applied in filename order
atlasql/config.py  environment-driven settings
atlasql/db.py      connections, schema application, metric registry
atlasql/etl/       one module per data source, all idempotent
atlasql/cli.py     python -m atlasql.cli <command>
```
