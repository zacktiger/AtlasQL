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
python -m atlasql.cli import-world-bank      # GDP per capita, country tier
python -m atlasql.cli import-elevation       # mean/min/max from GMTED2010
python -m atlasql.cli import-rivers          # length + major river count
python -m atlasql.cli refresh-availability   # every job already does this
```

Every job is idempotent and upserts, so re-running is always safe. Source
archives are cached under `data/raw/` after the first download — around 1.6 GB
of DEM tiles and 0.5 GB of HydroRIVERS, downloaded once.

Each job recomputes `metric_availability` in its own transaction. Coverage that
lags the data it describes does not fail loudly, it silently changes which
level a query runs at.

## API

```bash
python -m uvicorn atlasql.api:app --reload
```

- `GET /metadata` — metrics with per-level coverage, and levels with region
  counts. Generated from the live registry, so adding a metric through an ETL
  job makes it queryable and selectable without a code change.
- `POST /query` — takes a `GeoFilter`, returns a ranked top-N.

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
