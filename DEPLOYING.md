# Deploying AtlasQL

Two things ship, and they are very different sizes:

- **The app** — stateless, no build step, no ETL. A 269 MB container that needs
  a `DATABASE_URL` and nothing else.
- **The database** — PostgreSQL with PostGIS, holding the regions and metrics.
  You build it once locally and copy it up.

The important number: **the tables the API reads restore to about 78 MB, from a
27 MB dump.** A fully built AtlasQL database is 1.36 GB, but 1.27 GB of that is
`hydrorivers_segments`, the 8.5 million HydroRIVERS line segments the rivers ETL
aggregates against and nothing reads afterwards. Excluding it puts this app
inside the free tier of every Postgres host below. Check yours:

```bash
python -m atlasql.cli serving-size
```

## 1. Dump the database

From a checkout with a built database:

```bash
# Against the docker-compose database
docker exec atlasql-db pg_dump -U atlasql -d atlasql \
  --no-owner --no-privileges --exclude-table=hydrorivers_segments \
  -Fc -f /tmp/atlasql.dump
docker cp atlasql-db:/tmp/atlasql.dump ./atlasql.dump
```

`--no-owner --no-privileges` matter: managed Postgres does not give you the role
names the dump would otherwise try to restore as.

If you have never built the database, do that first — see the ETL section of
the README. There is no shortcut; the data is derived from source archives, not
checked in.

## 2. Pick a host

Every option below supports PostGIS, which is not optional here — the app asks
the database for `ST_SimplifyPreserveTopology` and `ST_AsGeoJSON` on every map
request.

| Host | Postgres | Notes |
|---|---|---|
| **Render** | PostGIS on PG 13+ | Blueprint in `render.yaml` deploys app + database from one file. Free web services sleep after 15 min idle; **free Postgres is deleted 30 days after creation**, so use `basic-256mb` for anything you intend to keep. |
| **Fly.io** | Managed Postgres, PostGIS ticked at creation | `fly.toml` included. PostGIS is a provisioning option — you cannot add it to an existing cluster. |
| **Railway** | PostGIS via their Postgres template | Deploys the Dockerfile directly. Hobby plan $5/mo including usage credit. |
| **Neon** or **Supabase** (database only) | PostGIS available; 0.5 GB free | Pair with any app host. This is the free-and-permanent combination: 78 MB fits comfortably, and neither expires the way Render's free database does. |
| **Any VPS** | `docker compose` | `docker-compose.yml` already runs PostGIS; add the app container beside it. Most control, ~$5/mo, most to maintain. |

**The recommendation, if you want it to stay up and stay free:** Neon or
Supabase for the database, Render or Fly for the app. If you would rather have
one dashboard and one bill, use the Render blueprint with the paid database.

## 3. Deploy

### Render (blueprint)

1. Push this repo to GitHub.
2. Render → **New** → **Blueprint** → select the repo. `render.yaml` creates the
   web service and the database.
3. Load the data, using the database's **external** connection string:
   ```bash
   pg_restore --no-owner --no-privileges -d "<external-connection-string>" atlasql.dump
   ```
4. Optional: set `ANTHROPIC_API_KEY` in the dashboard to enable `/parse`.

To use Neon or Supabase instead, delete the `databases:` block from
`render.yaml` and set `DATABASE_URL` in the dashboard to their connection
string.

### Fly.io

```bash
fly launch --no-deploy                 # keeps the included fly.toml
fly mpg create                         # tick PostGIS when prompted
fly mpg attach <cluster>               # sets DATABASE_URL for you
fly secrets set ANTHROPIC_API_KEY=sk-ant-...   # optional
fly deploy

fly mpg connect <cluster> --  \
  pg_restore --no-owner --no-privileges -d "$DATABASE_URL" < atlasql.dump
```

### Anywhere that takes a Dockerfile

```bash
docker build -t atlasql .
docker run -p 8000:8000 -e DATABASE_URL="postgresql://..." atlasql
```

## Configuration

Only `DATABASE_URL` is required. `ATLASQL_DATABASE_URL` overrides it, so a local
`.env` always beats what a platform injects.

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | docker-compose local | Connection string. Render, Railway, Fly and Heroku all inject this name themselves. |
| `PORT` | `8000` | Port to listen on. Set by the host. |
| `ANTHROPIC_API_KEY` | unset | Enables `/parse`. Without it the natural language box is hidden and everything else works. |
| `ATLASQL_POOL_MAX_SIZE` | `8` | Pool ceiling per instance. Raise only with the database's connection limit in mind. |
| `ATLASQL_COVERAGE_THRESHOLD_PCT` | `80` | Coverage a metric needs before a level may be queried. |
| `ATLASQL_WARM_ON_STARTUP` | `1` | Build the basemap and credential caches at boot so the first visitor does not pay for them. |
| `ATLASQL_PREFILTER_MIN_REGIONS` | `10000` | Region count above which the query builder adds the EXISTS pre-filter. A plan choice; it cannot change results. |

## Things that will bite

- **PostGIS must exist before the restore.** The dump creates the extension, but
  the host has to offer it. On Fly it is a checkbox at cluster creation and
  cannot be added later.
- **Do not restore the whole database.** Without `--exclude-table`, you are
  moving 1.36 GB — and paying to store an ETL staging table the app never reads.
- **Re-running the ETL against production is not the plan.** The import jobs
  download gigabytes and take tens of minutes. Build locally, dump, restore.
- **Idle instances sleep.** On Render's free tier the first request after 15
  minutes waits for a cold start. `min_machines_running = 1` in `fly.toml`
  avoids this on Fly.
- **The app image has no ETL.** `python -m atlasql.cli import-*` will not run in
  the container; those need `requirements.txt` and a checkout.
- **Connection limits.** `ATLASQL_POOL_MAX_SIZE` is per instance. Two instances
  at 8 is 16 connections, which is real money on a small managed Postgres.
