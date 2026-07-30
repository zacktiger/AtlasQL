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
```

Every job is idempotent and upserts, so re-running is always safe. Source
archives are cached under `data/raw/` after the first download.

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
