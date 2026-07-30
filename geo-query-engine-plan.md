# Geo query engine build plan

## Part 1: non-technical plan

### What this is
A tool that answers flexible geography questions across every level of the world's administrative hierarchy (continent, country, state/province, county/district, city) using a shared database of metrics like GDP per capita, elevation, population, river count, and river length. A user gives conditions (for example "GDP per capita > 40,000 and elevation > 500m"), the tool figures out which level the question makes sense at, and returns a ranked list of matches, optionally limited to a top N.

### Example queries it should answer
- Countries with GDP per capita over 40,000 and mean elevation over 500m
- Top 10 US counties by river length
- States with population under 2 million and more than 5 major rivers
- Cities above 2,000m elevation with population over 500,000

### Explicit scope for v1 (keep it small on purpose)
In scope:
- Country tier fully working end to end
- One clean example of a state/county tier
- Structured filter UI
- Natural language input on top of the same filter

Out of scope for v1:
- Historical time series (metrics changing year over year)
- Categorical metrics (climate zone, terrain type)
- Real-time data refresh
- User accounts, saved queries

### Phases, in plain language

1. **Prove the concept, country tier only.** Every country in the world, five to ten real metrics, structured filter working end to end. Done when: any two-condition query across countries returns a correct ranked list.
2. **Add one lower tier.** Pick either US states or Indian states as the second tier, both have decent public data. Done when: the same filter engine works unmodified against this new tier.
3. **Add cities.** Larger volume (tens of thousands of rows), different metric availability, GDP rarely exists at city level. Done when: city queries correctly reject GDP-based filters with a clear message instead of silently failing.
4. **Natural language layer.** Add the LLM parser on top of the already-working structured filter. Done when: parsed queries are shown back to the user as an editable form before running.
5. **Frontend polish.** Dropdown-driven query builder, results table, ranked list display. Done when: someone unfamiliar with the project can build a two-condition query without being told how.

### Rough timeline shape
Phase 1 is the only phase worth estimating carefully, because it forces every real design decision: schema, ETL patterns, query engine logic. Once that's solid, phases 2 through 5 are mostly repetition and UI work and go much faster.

---

## Part 2: technical plan

### 2.1 Architecture recap

```
open data sources
   -> ETL pipeline (Python: GeoPandas, rasterio, rasterstats, shapely)
   -> PostgreSQL + PostGIS (regions, metrics, metric_availability)
   -> Query API (FastAPI: structured filter -> parameterized SQL)
        <- LLM query parser (Claude tool use: NL -> structured filter)
   -> Frontend (JS: query builder + ranked results)
```

### 2.2 Database schema

```sql
CREATE TABLE regions (
  id BIGINT PRIMARY KEY,
  name TEXT NOT NULL,
  level TEXT NOT NULL CHECK (level IN ('continent','country','state','county','city')),
  parent_id BIGINT REFERENCES regions(id),
  geom GEOMETRY(MultiPolygon, 4326),
  centroid GEOMETRY(Point, 4326)
);
CREATE INDEX idx_regions_level ON regions(level);
CREATE INDEX idx_regions_parent ON regions(parent_id);
CREATE INDEX idx_regions_geom ON regions USING GIST(geom);

CREATE TABLE metrics (
  region_id BIGINT REFERENCES regions(id),
  metric_name TEXT NOT NULL,
  value DOUBLE PRECISION,
  year INT,
  PRIMARY KEY (region_id, metric_name, year)
);
CREATE INDEX idx_metrics_name_value ON metrics(metric_name, value);

CREATE TABLE metric_availability (
  metric_name TEXT,
  level TEXT,
  coverage_pct DOUBLE PRECISION,
  PRIMARY KEY (metric_name, level)
);
```

### 2.3 ETL pipeline: sources per metric

| Metric | Source | Method |
|---|---|---|
| Boundaries (country/state) | Natural Earth | Direct import |
| Boundaries (county and below) | geoBoundaries | Direct import. Open license. Avoid GADM for anything beyond personal or academic use, its redistribution terms are restrictive |
| Cities (point data, population, elevation) | GeoNames | Direct import |
| GDP per capita | World Bank API | Country level only, no reliable global subnational source exists |
| Elevation (mean, min, max) | SRTM or GMTED2010 DEM raster | Zonal statistics via `rasterstats` per polygon |
| Rivers (count, length) | HydroRIVERS (HydroSHEDS) | Spatial intersection with polygon. Group segments by main-stem ID before counting, otherwise one river counts as dozens |

Tooling: Python, GeoPandas, rasterio, rasterstats, shapely, psycopg2 or GeoAlchemy2 for writes.

Each ETL job should be idempotent, upsert on `(region_id, metric_name, year)`, so re-running never duplicates rows.

After each ETL run, recompute `metric_availability` (percentage of non-null rows per metric per level). This is what the level auto-detection in the query engine depends on.

### 2.4 Query engine

Shared schema, used identically by both input paths:

```python
from typing import Literal
from pydantic import BaseModel, Field

class Condition(BaseModel):
    metric: str
    op: Literal[">", "<", ">=", "<=", "=="]
    value: float

class GeoFilter(BaseModel):
    level: Literal["continent","country","state","county","city","auto"] = "auto"
    conditions: list[Condition]
    sort_by: str | None = None
    order: Literal["asc","desc"] = "desc"
    top_n: int = Field(default=10, le=100)
```

Level auto-detection: look up `metric_availability` for every condition's metric, pick the most granular level where all requested metrics clear a coverage threshold (start at 80 percent). If no level clears it for every condition, return an error naming the blocking metric, don't silently guess a level.

SQL builder: one join against `metrics` per condition, parameterized with SQLAlchemy Core or asyncpg bound params. Never string-concatenated SQL, never LLM-generated SQL directly.

### 2.5 API endpoints

- `GET /metadata` — available metrics (name, label, unit, levels with data) and levels, read from the live metric registry, not hardcoded.
- `POST /query` — accepts a `GeoFilter`, returns ranked results.
- `POST /parse` — accepts natural language text, returns a `GeoFilter`, does not execute it.

### 2.6 LLM query parser

Use Claude tool use with forced `tool_choice` against the exact `GeoFilter` JSON schema. Re-validate the returned object server-side: confirm `level` is a real level, confirm every `condition.metric` exists in the live metric registry. Show the parsed filter back to the user as a pre-filled structured form before running the query, so a misparse is caught immediately instead of three steps downstream.

### 2.7 Frontend contract

Frontend only ever sends and receives `GeoFilter` JSON. Dropdowns for metric and level populate from `/metadata`, never hardcoded, so adding a metric to the database doesn't require a frontend deploy.

### 2.8 Build order checklist

- [ ] Stand up Postgres + PostGIS, apply schema
- [ ] Import Natural Earth country boundaries
- [ ] Import World Bank GDP per capita, country level
- [ ] Import SRTM/GMTED2010, compute elevation zonal stats for countries
- [ ] Import HydroRIVERS, compute river count and length per country
- [ ] Populate `metric_availability` for the country tier
- [ ] Build `/metadata` and `/query`, structured filter only
- [ ] Verify a 2-condition country query returns a correct ranked top-N
- [ ] Add one lower tier end to end (state), same pipeline
- [ ] Add city tier via GeoNames
- [ ] Add `/parse` with Claude tool use and server-side validation
- [ ] Build frontend query builder driven entirely by `/metadata`
- [ ] Wire NL input -> `/parse` -> pre-filled form -> `/query`

### 2.9 Open decisions to resolve early (they affect the schema)

- Which elevation variant counts as "the" elevation for a region: mean, max, or expose all three as separate metrics? Recommend exposing all three.
- Coverage threshold for auto level detection. Start at 80 percent, tune once real data is in.
- Do regions with missing data get excluded from results, or shown with a null flag? Recommend excluded by default, with an option to include.
- Data refresh cadence: a static one-time import is fine for v1, don't build a scheduler until something actually needs refreshing.
