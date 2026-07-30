# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Phase 1 (country tier) and phase 2 (state tier) are done, and the structured
frontend is built. Next per the plan's build order: the city tier via GeoNames,
then `/parse`. Two design documents govern the work; read both before writing
anything:

- `high-level-vision.md` — the product vision and long-term direction. Read it when deciding *whether* a design is right.
- `geo-query-engine-plan.md` — the concrete v1 architecture, schema, data sources, and build order. Read it before starting each phase for the *how*.

Where they disagree, the plan wins for what to build now and the vision wins for what not to foreclose. Note the vision doc names the product **TerraQuery** while the repo and remote are **AtlasQL** — treat them as the same product.

The plan commits to Python + FastAPI + PostgreSQL/PostGIS for the backend and a JS frontend; follow that unless the user redirects.

## Commands

```bash
docker compose up -d                          # PostGIS on localhost:55432
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m atlasql.cli init-db   # apply sql/*.sql, idempotent
.venv/Scripts/python -m atlasql.cli import-natural-earth
.venv/Scripts/python -m atlasql.cli import-states
.venv/Scripts/python -m atlasql.cli import-world-bank
.venv/Scripts/python -m atlasql.cli import-elevation --level country
.venv/Scripts/python -m atlasql.cli import-rivers --level state
.venv/Scripts/python -m pytest                # DB tests skip if it is not up
.venv/Scripts/python -m uvicorn atlasql.api:app --reload
```

The elevation and rivers imports each download roughly half a gigabyte or more
on first run and take tens of minutes; both resume from cached files and from
partially staged data, so an interrupted run is restarted, not repeated.

Paths above are Windows (`.venv/Scripts`); use `.venv/bin` elsewhere. The host
port is 55432 rather than 5432/5433 because a locally installed Postgres often
holds those, and on Windows it can win the bind race against Docker — which
shows up as a password-authentication failure, not a connection refusal.
Override the connection with `ATLASQL_DATABASE_URL` (see `.env.example`).

Downloaded source archives are cached in `data/` (gitignored); deleting it only
costs a re-download.

## Git workflow

Remote: `https://github.com/zacktiger/AtlasQL.git`

- **Commit and push constantly.** Every self-contained unit of work — one ETL job, one endpoint, one schema migration, one frontend panel — is a commit, pushed immediately. Do not batch a day's work into one commit, and do not wait to be asked to commit.
- **Push straight to the working branch** for normal incremental work. Open a PR only for a large overhaul: schema redesign, replacing a data source, restructuring the query engine, or anything touching the `GeoFilter` contract. If a change would be painful to review as a diff against the last push, it belongs in a PR.
- **No Claude attribution.** Commit messages and PR bodies carry no `Co-Authored-By: Claude`, no "Generated with Claude Code" footer, no mention of AI assistance. Write the message as the author.
- Commit messages describe what changed and why, in the imperative.

## Visual review with Playwright

Once the frontend exists, review it in a real browser every few commits rather than only at the end — use the Playwright tools to load the running app, drive an actual query, and look at the result. Check that the flow the product is built around still works end to end:

1. Metric and level dropdowns populate from `/metadata` (not hardcoded).
2. A two-condition query returns a sensible ranked top-N.
3. A natural language query round-trips: `/parse` → pre-filled editable form → `/query`.
4. An impossible query (e.g. GDP per capita at city level) surfaces the blocking-metric error, not an empty table.

Catch regressions here before they stack up behind several commits of new work.

## Maintaining the essence of the product

The one-line version from the vision doc: **users search the world itself, instead of searching dataset by dataset.** Most geographic data lives in isolated sources — population here, elevation there, boundaries somewhere else — and the product's value is the unified queryable surface over all of it. When making judgment calls, protect these properties over convenience:

- **One query path, many inputs.** Filters, natural language, and edited AI-generated filters all emit the same `GeoFilter`. Natural language is a convenience layer, never the engine. If a feature tempts you to add a second execution path or a special-case query builder, that is drift.
- **No predefined templates.** Users combine conditions freely. Any design that only answers a fixed menu of questions has lost the point, even if it ships faster.
- **Honesty about data coverage** beats returning something. A named error about a missing metric is a correct answer; a silently degraded level or a quietly empty result is not.
- **The hierarchy is the product.** A feature that only works for countries and cannot generalize down the tiers is a warning sign, not a shortcut.
- **Extensibility without redesign.** Adding a dataset or metric should not require touching the engine. This is the property that later entity types depend on — if adding a metric is invasive today, adding rivers as queryable entities will be impossible.

### Correctness before capability

Both docs are emphatic that stages build on each other: a reliable structured engine first, then coverage, then more entity types, then natural language, then ranking and similarity. Do not pull later stages forward. Concretely, these are deliberately excluded from v1 — do not add schema or API surface in anticipation of them:

- Historical time series, categorical metrics (climate, terrain), real-time refresh, accounts and saved queries.

### Where the vision goes beyond v1

Know these so today's decisions don't foreclose them, but do not build them yet:

- **Non-administrative entities** — rivers, mountains, lakes, forests, deserts, protected areas as first-class queryable things ("rivers longer than 4,000 km"). The v1 `regions.level` CHECK constraint only permits the five administrative levels, so this is a known future migration. Keep entity-type assumptions out of the query engine and metric tables even while the constraint stands.
- **Geographic reasoning** — "countries similar to Japan", "cities like Zurich but warmer", "rich mountainous countries". These need ranking, similarity, and multidimensional comparison rather than the fixed numeric thresholds `Condition` expresses today. Expect `GeoFilter` to grow a similarity mode eventually; do not contort it now.

## What AtlasQL is

A query engine over the world's administrative hierarchy (continent → country → state → county → city). Users supply numeric conditions ("GDP per capita > 40000 and mean elevation > 500"), and the engine picks the appropriate hierarchy level, filters, ranks, and returns a top-N list.

## Architecture

```
open data sources
   -> ETL (Python: GeoPandas, rasterio, rasterstats, shapely)
   -> PostgreSQL + PostGIS: regions, metrics, metric_availability
   -> FastAPI: structured GeoFilter -> parameterized SQL
        <- Claude tool use: natural language -> GeoFilter
   -> JS frontend: query builder + ranked results
```

The pivot of the whole design is that **`GeoFilter` is the single contract**. Natural language input and the structured UI both produce the same Pydantic model; the SQL builder has exactly one input type. Adding a new input path means emitting a `GeoFilter`, never touching the query engine.

The second pivot is that **`metric_availability` drives level auto-detection**. It is a derived table (coverage percentage per metric per level), recomputed after every ETL run. When `level="auto"`, the engine picks the most granular level where every requested metric clears the coverage threshold (start at 80%). This is why ETL jobs must recompute availability — stale availability silently changes query semantics.

## Invariants to preserve

- **Never let the LLM produce SQL.** Claude emits a `GeoFilter` via forced `tool_choice` against the JSON schema; the server re-validates that `level` is real and every `condition.metric` exists in the live metric registry, then builds parameterized SQL itself (SQLAlchemy Core / asyncpg bound params). No string concatenation anywhere in the SQL path.
- **`/parse` never executes.** It returns a `GeoFilter` for the user to review in a pre-filled form. Execution is a separate `/query` call.
- **Fail loudly on missing coverage.** If no level clears the threshold for all conditions, return an error naming the blocking metric. Never guess a level. This is what makes "GDP per capita at city level" a clear rejection instead of an empty result set.
- **Metrics and levels are never hardcoded in the frontend.** Dropdowns populate from `GET /metadata`, so adding a metric to the database needs no frontend deploy.
- **ETL jobs are idempotent**, upserting on `(region_id, metric_name, year)`.
- **Avoid GADM** for boundary data — restrictive redistribution terms. Use Natural Earth (country/state) and geoBoundaries (county and below).

## Domain gotchas

- **River counting**: HydroRIVERS stores rivers as many segments. Group by main-stem ID before counting or one river counts as dozens.
- **Elevation**: expose mean, min, and max as three separate metrics rather than picking one canonical "elevation".
- **Cities** are point data (GeoNames), not polygons, so raster zonal statistics and polygon intersection do not apply the same way as for the polygon tiers.

## Build order

`geo-query-engine-plan.md` §2.8 has the checklist. The sequencing is deliberate: get the country tier fully working end to end (boundaries → GDP → elevation → rivers → availability → `/query`) before adding a second tier. Phase 1 forces every real schema and ETL design decision; later tiers are meant to reuse the filter engine unmodified. If a new tier requires changing the query engine, that is a signal the abstraction is wrong.

Out of scope for v1: historical time series, categorical metrics, real-time refresh, user accounts and saved queries.
