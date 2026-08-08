# AtlasQL

A query engine over the world's administrative hierarchy (continent → country →
state → city). You give it numeric conditions — "GDP per capita > 40000 and
mean elevation > 500" — and it picks the appropriate level, filters, ranks, and
returns a top-N list.

See `high-level-vision.md` for where this is going and
`geo-query-engine-plan.md` for what v1 is. `CLAUDE.md` has the invariants and
domain gotchas if you're changing the engine rather than just running it.

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
python -m atlasql.cli import-counties        # geoBoundaries ADM2 under states
python -m atlasql.cli import-cities          # GeoNames cities + population
python -m atlasql.cli import-world-bank      # GDP per capita and population
python -m atlasql.cli import-gridded-gdp --level country   # GDP per capita (PPP)
python -m atlasql.cli import-gridded-gdp --level state
python -m atlasql.cli import-gridded-gdp --level city
python -m atlasql.cli import-elevation --level city
python -m atlasql.cli import-elevation --level country
python -m atlasql.cli import-elevation --level state
python -m atlasql.cli import-rivers --level country
python -m atlasql.cli import-rivers --level state
python -m atlasql.cli import-coastline --level continent   # boundary facing
python -m atlasql.cli import-coastline --level country     # open water; needs
python -m atlasql.cli import-coastline --level state       # no download
python -m atlasql.cli import-subregions --level continent  # how many lower-tier
python -m atlasql.cli import-subregions --level country    # regions are inside
python -m atlasql.cli import-subregions --level state      # each region
python -m atlasql.cli refresh-availability   # every job already does this
```

The metric jobs take `--level`, because giving a new tier its metrics is
running the same job with a different argument. Nothing in the query engine
knows how many tiers exist.

`import-subregions` goes last, and is the only job that reads what the others
wrote: it counts descendants down the `parent_id` hierarchy, so re-importing
regions leaves its numbers stale until it runs again. It touches no source data
and finishes in milliseconds, so re-running it is cheap. It is also the one job
that accepts `--level continent`, because what it measures is the hierarchy
itself and a continent has as much of that as anywhere else — `country_count`
exists at no other level.

Every job is idempotent and upserts, so re-running is always safe. Source
archives are cached under `data/raw/` after the first download — around 1.6 GB
of DEM tiles and 0.5 GB of HydroRIVERS, downloaded once. `import-natural-earth`,
`import-states`, and `import-cities` are quick (seconds to low minutes);
`import-elevation` and `import-rivers` are the slow ones — tens of minutes
each on first run because of those downloads, much faster on a re-run since
the archives are cached.

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

The builder sits beside the map rather than above it, and the globe is drawn
before you have run anything — the empty state lists what this deployment
actually holds, metric by metric with the levels each one reaches, which is the
same coverage model that decides every refusal you will later see. Light and
dark follow the system preference and can be overridden; the globe reads its
palette from the same CSS tokens, so it changes with the page.

Results are drawn on a globe as well as in the table. The two are one selection
seen twice: clicking a row turns the globe to that region, clicking a region
highlights its row. Drag spins, scroll zooms, and zooming out always reaches the
whole planet — the projection is orthographic at every scale, so a single
country and the whole world are the same map, not two. Both the result outlines
and the basemap under them are refetched at finer detail once the zoom justifies
it, and a fly-to never goes closer than the basemap can honestly draw.

A refusal clears the map. Leaving the previous answer up beside an error would
put a legend for one metric next to a message about another, which is the
map-disagreeing-with-the-table problem the rest of the design avoids.

"Compare two queries" splits the builder into Query A / Query B and runs them
independently — each is a normal `POST /query` call with its own `GeoFilter`,
rendered as two ranked tables side by side. There is no separate compare
endpoint: the single-`GeoFilter`-contract rule means two queries are just two
calls, not new API surface. (Compare mode is table-only; the globe view is
single-query for now.)

The basemap is our own `regions` table rather than a tile service, so every
coastline on screen is a boundary some query could have returned. Region fills
encode the metric the results are ranked by, on a single-hue sequential ramp.

The frontend is static files served by the same app — no build step, no second
process. `frontend/vendor/` holds d3-geo and d3-array for the projection and
its spherical clipping; see the README there for why they are checked in.

- `GET /metadata` — metrics with per-level coverage, and levels with region
  counts. Generated from the live registry, so adding a metric through an ETL
  job makes it queryable and selectable without a code change.
- `POST /query` — takes a `GeoFilter`, returns a ranked top-N. This is also
  what compare mode calls twice.
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
| Continent | 7 | coastline, countries within, states within, cities within |
| Country | 258 | GDP (nominal), GDP (PPP), GDP per capita, GDP per capita (PPP), population, elevation (mean/min/max), major river length, major rivers, coastline, states within, cities within |
| State | 4,596 | GDP (PPP), GDP per capita (PPP), elevation (mean/min/max), major river length, major rivers, coastline, cities within |
| County | 49,015 | GDP (PPP), GDP per capita (PPP), coastline |
| City | 34,026 | GDP per capita (PPP), population, elevation (mean) |

The gaps are the interesting part, and each one is a named refusal rather than
an empty table:

- **Four GDP metrics on two axes, deliberately not merged.** Nominal against
  PPP and total against per head. `gdp_nominal` and `gdp_per_capita` are the
  World Bank's figures in current US dollars, country level only, because the
  Bank reports nothing below it. `gdp_ppp` and `gdp_per_capita_ppp` come from
  the Kummu downscaled dataset in 2021 international dollars; the per-capita one
  reaches every tier, the total stops at states because a city is a point with
  no area to total output over. Different units, so one name covering two would
  make `> 40000` mean different things depending on which level auto-detection
  picked — and the axes carry real disagreement, since at market rates the
  United States is the largest economy while at PPP China is. Asking for a World
  Bank metric below the country tier is still a named refusal.
- **`gdp_ppp` ranks but does not sum.** Every gridded job counts each cell a
  boundary clips, so a cell on a border counts toward both sides. Adding up a
  country's states overshoots its own total by around 18% at the median, and by
  more where subdivisions are many and small; a region smaller than a 5
  arc-minute cell is credited with that whole cell, which is why two Hong Kong
  districts inside one cell report the same number. None of this is visible in
  the per-capita figure, where it cancels in the ratio. It is left in rather
  than patched because the published total is the exact numerator of the
  published rate, and a narrower mask would make the two contradict each other
  about the same region.
- **The county tier is second-level administrative division, whatever each
  country calls it.** Brazil contributes 5,570 municipalities, Romania 3,235
  communes, the United States 3,231 counties, Japan 1,731 municipalities. These
  are not comparable units of size or population and the tier does not pretend
  they are — it is a position in each country's own hierarchy. 176 countries
  have ADM2 in geoBoundaries; the rest have no counties at all, because CGAZ
  substitutes provinces and whole countries where it has no second-level data
  and loading those would answer a county query with states. Elevation, rivers
  and population do not reach this tier yet — only coastline and the PPP GDP
  pair, which is what a county query can be answered with today.
- **County boundaries come from a different source than everything above them**,
  which is the one place these metrics stop being comparable across tiers. See
  the coastline note below; the same applies to `gdp_ppp`, where the border-cell
  double count grows from 18% at the state tier to 85% at the county tier.
- **Coastline comes from our own boundaries, and is comparable rather than
  authoritative.** `coastline_km` subtracts from each region's outline every
  border a neighbour is on the other side of, so it needs no new source and the
  number cannot disagree with the coastline on the globe. Landlocked regions are
  exactly 0 — a measurement, which makes `coastline_km == 0` how you find them.
  But coastline length grows without limit as the ruler gets finer, so published
  national figures disagree with each other by factors of two to five and ours
  will not match any particular one. Measuring every region at a tier on the same
  linework is what makes ranking them mean something — within a tier. Across one
  it does not: counties are drawn by geoBoundaries and everything above them by
  Natural Earth 1:50m, and the finer outlines give a county tier totalling about
  36% more coastline than the states containing it, where states match their
  countries to 0.1%. That gap is the ruler changing, not the coast.
- **Subnational GDP is downscaled, not reported.** Genuinely subnational
  accounts exist for 89 countries; elsewhere the grid has no subnational signal
  and a country's states differ only by how population is spread. Coverage
  percentage cannot express that, so it is in the metric description that
  `/metadata` serves.
- **River metrics count major rivers only** — Strahler stream order 5 and above.
  HydroRIVERS is modelled from flow accumulation, so its lower orders are
  computed headwater and ephemeral channels: orders 1 to 4 are 94% of its
  segments and 95% of its length. Including them made "total river length" a
  number dominated by drainage nobody has ever seen (3.9 million km for the
  largest country, against 233,000 km of actual named-river network) and cost
  1.27 GB of staging to produce. There is no metric for total modelled drainage;
  if you want one back it is a threshold change, not a new source.
- **River metrics** stop at the state tier. Cities are points; a point contains
  no rivers.
- **Elevation minimum and maximum** stop at the state tier too. A point has no
  range, so cities carry only `elevation_mean`, sampled at their location.
- **Population** spans city and country but not state, so a query combining it
  with the World Bank GDP figure resolves to country while population alone
  reaches cities.
- **The subregion counts stop where the thing they count runs out.** A country
  contains no countries and a state contains no states, so `country_count`
  exists only on continents and `state_count` only from country up. They are
  the one metric family that reaches the continent tier, because the hierarchy
  is the one thing a continent has as much of as anywhere else. `city_count`
  counts the GeoNames cities15000 set, so it means settlements of 15,000 people
  or more; zero means none above that floor, not none at all. And a tier only
  gets a count for what actually hangs beneath it, which is not the same as
  what sits lower in the hierarchy: cities are parented to states rather than
  counties, so counties carry `county_count`'s absence rather than a column of
  zeroes claiming every county has no cities.

## Tests

```bash
python -m pytest
```

Tests that need the database skip automatically when it is not running; the
one live `/parse` test skips automatically without `ANTHROPIC_API_KEY`.

## Layout

```
sql/                    schema migrations, applied in filename order
atlasql/config.py       environment-driven settings
atlasql/db.py           connections, schema application, metric registry
atlasql/models.py       GeoFilter / QueryResult / metadata Pydantic models
atlasql/query.py        GeoFilter -> parameterized SQL, level auto-detection
atlasql/geometry.py     regions -> simplified GeoJSON for the globe
atlasql/parser.py       natural language -> GeoFilter via Claude tool use
atlasql/api.py          FastAPI app: the endpoints above, serves frontend/
atlasql/cli.py          python -m atlasql.cli <command>
atlasql/etl/            one module per data source, all idempotent
frontend/               static query builder + globe, no build step
tests/                  pytest; DB- and API-key-dependent tests self-skip
```

## Disk footprint and starting over

A full local checkout is roughly 4 GB, almost none of it source:

| What | Size | Recoverable from |
|---|---|---|
| Source + docs | ~1.5 MB | `git clone` |
| `.venv/` | ~446 MB | `pip install -r requirements.txt` |
| `data/raw/` (gitignored ETL downloads) | ~2.5 GB | re-run the ETL commands above; ~1.6 GB DEM + 520 MB HydroRIVERS re-download, the rest re-derives from cache |
| `atlasql-db` Docker volume | ~1.0 GB | `docker compose up -d` then every `import-*` job again |

The database itself is only 167 MB — 208 MB on disk with Postgres's own
overhead. The rest of that volume is recycled write-ahead log, which Postgres
holds for reuse and caps at `max_wal_size` (1 GB by default). It shrinks on its
own as the estimate of future need decays; it is not growth. To reclaim it now,
dump and recreate the volume — which is the same dump you need for a deploy
anyway, so see `DEPLOYING.md`.

None of it is irreplaceable — GitHub is always the full source of truth — but
**a restore is not just `git clone`.** After a fresh clone the database is
empty, so every step under [ETL](#etl) has to run again, in order, starting
from `init-db`. If you deleted `data/raw/` too, budget tens of minutes for
`import-elevation` and `import-rivers` to re-download. If you deleted the
`.env` file (or never had one), `/parse` stays disabled until
`ANTHROPIC_API_KEY` is set again — everything else works without it.

If you're clearing space without deleting the whole checkout, `data/raw/` and
the Docker volume are the two big, safely-deletable, slow-to-rebuild pieces;
`.venv/` is big but cheap to rebuild.
