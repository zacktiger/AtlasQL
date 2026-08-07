# Deploying AtlasQL

Two things ship, and they are very different sizes:

- **The app** — stateless, no build step, no ETL. A 269 MB container that needs
  a `DATABASE_URL` and nothing else.
- **The database** — PostgreSQL with PostGIS, holding the regions and metrics.
  You build it once locally and copy it up.

**There is no third thing.** The frontend is not a separate deployment: it is
four static files and two vendored d3 modules, 156 KB in total, with no build
step and no framework, served by the app itself from `/`. Every request it makes
is a same-origin relative path (`/metadata`, `/query`, `/geometry`), and there is
no CORS middleware anywhere in the app because none has ever been needed. Putting
it on a separate static host is possible but is a net loss — see "Why the
frontend does not go on Vercel" at the end.

The important number: **the tables the API reads restore to about 78 MB, from a
27 MB dump.** A built AtlasQL database is 167 MB, of which 77 MB is
`hydrorivers_segments` — the staged river segments the rivers ETL aggregates
against and nothing reads afterwards — plus a small bookkeeping table beside it.
Excluding both puts this app inside the free tier of every Postgres host below.
Check yours:

```bash
python -m atlasql.cli serving-size
```

## 1. Dump the database

From a checkout with a built database:

```bash
# Against the docker-compose database
docker exec atlasql-db pg_dump -U atlasql -d atlasql \
  --no-owner --no-privileges \
  --exclude-table=hydrorivers_segments --exclude-table=hydrorivers_staging_state \
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

### Neon + Render (recommended)

Neon holds the database, Render runs the app. Neon's free tier is 0.5 GB and does
not expire, which is the reason to prefer it over Render's own free Postgres —
that one is deleted 30 days after creation.

**a. Create the database.** Sign up at neon.tech, create a project on Postgres
16, and enable PostGIS in their SQL editor:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
```

**b. Load it.** Neon gives you two connection strings; use the **direct** one
(no `-pooler` in the host) for the restore — a restore is one long session and
gains nothing from a transaction pooler.

```bash
pg_restore --no-owner --no-privileges -d "postgresql://…neon.tech/neondb?sslmode=require" atlasql.dump
```

Then check it landed:

```bash
psql "postgresql://…neon.tech/neondb?sslmode=require" \
  -c "SELECT level, count(*) FROM regions GROUP BY level ORDER BY 1;"
```

You should see 7 continents, 258 countries, 4,596 states and 34,026 cities.

**c. Deploy the app.** In this repo, delete the `databases:` block from
`render.yaml` (Neon is the database now), then Render → **New** → **Blueprint** →
pick the repo. Set `DATABASE_URL` in the dashboard to the Neon connection
string. Either Neon string works here — the app runs its own connection pool, so
the pooled endpoint buys nothing, and the direct one is one less hop.

**d. Optional.** Set `ANTHROPIC_API_KEY` to enable `/parse`. Leave it unset and
the natural-language box stays hidden; nothing else changes.

Two Neon behaviours worth knowing, both already handled:

- **It scales to zero.** After idle, the first query waits a few hundred
  milliseconds for the compute to wake. The app warms its caches at boot, so
  this shows up once rather than on every cold page.
- **It drops idle connections.** The pool checks a connection before handing it
  out and recycles anything idle for more than 180 seconds
  (`ATLASQL_POOL_MAX_IDLE_S`), which is what keeps that from surfacing as an
  intermittent 500.

### Render (blueprint, with Render's own Postgres)

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
- **Do not restore the whole database.** Without `--exclude-table`, you carry
  the staged river segments too — an ETL table the app never reads, and one you
  would then be paying a host to store.
- **Re-running the ETL against production is not the plan.** The import jobs
  download gigabytes and take tens of minutes. Build locally, dump, restore.
- **Idle instances sleep.** On Render's free tier the first request after 15
  minutes waits for a cold start. `min_machines_running = 1` in `fly.toml`
  avoids this on Fly.
- **The app image has no ETL.** `python -m atlasql.cli import-*` will not run in
  the container; those need `requirements.txt` and a checkout.
- **Connection limits.** `ATLASQL_POOL_MAX_SIZE` is per instance. Two instances
  at 8 is 16 connections, which is real money on a small managed Postgres.

## Why the frontend does not go on Vercel

Vercel is the right answer when there is a frontend build to run and a bundle to
push to a CDN. There is neither here. `frontend/` is 156 KB of hand-written
HTML, CSS and two JS modules, plus d3-geo and d3-array checked in — no npm, no
bundler, no framework, nothing to compile. The app already serves it with ETags
and `Cache-Control: no-cache`, so a repeat visit is a 304, and it is gzipped by
the same middleware as everything else.

Splitting it onto a separate origin costs three things and returns none:

1. **CORS becomes mandatory.** The app has no CORS middleware, because the
   frontend has always been same-origin. You would add one and then own the
   allowed-origins list forever.
2. **The API base stops being implicit.** Every call is a relative path today
   (`fetch("/query")`). On another origin they all need an absolute base, which
   means a build-time or runtime config value, which means the frontend now has
   configuration and an environment where it can be wrong.
3. **Two deploys can disagree.** `index.html` on Vercel against an older `app.js`
   on Render is exactly the silent version-skew the `no-cache` headers were added
   to prevent, except now it spans two providers.

You would be adding a deployment surface, a config surface and a failure mode to
serve 156 KB that the app is already serving correctly.

If you want Vercel anyway — for a custom domain, or to put a CDN in front — the
honest way is to point Vercel at the Render app as a rewrite target rather than
hosting the files separately, which keeps one origin and changes nothing in the
code. Custom domains are also available directly on Render, which is simpler
still.
