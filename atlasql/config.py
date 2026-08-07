"""Runtime configuration, read from the environment with docker-compose defaults."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_URL = "postgresql://atlasql:atlasql@localhost:55432/atlasql"


def _load_dotenv() -> None:
    """Read .env into the environment without adding a dependency for it.

    Values already set in the real environment win, so a shell export always
    overrides the file.
    """
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# DATABASE_URL is the name Render, Railway, Fly and Heroku all inject on their
# own, so honouring it means a managed Postgres needs no configuration at all.
# The ATLASQL_ prefix still wins, so a local override beats whatever the host
# supplies.
DATABASE_URL = os.environ.get(
    "ATLASQL_DATABASE_URL",
    os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
)

# Connection pool. Sized for the shared-CPU instances this is meant to run on:
# small enough not to exhaust a managed Postgres connection limit when a couple
# of app instances are up, large enough that the handful of concurrent requests
# a page load makes never queue.
POOL_MIN_SIZE = int(os.environ.get("ATLASQL_POOL_MIN_SIZE", "1"))
POOL_MAX_SIZE = int(os.environ.get("ATLASQL_POOL_MAX_SIZE", "8"))
POOL_TIMEOUT_S = float(os.environ.get("ATLASQL_POOL_TIMEOUT_S", "10"))
# Below the idle timeout of every managed Postgres and connection pooler worth
# naming, so a connection is recycled by us before it is dropped under us.
POOL_MAX_IDLE_S = float(os.environ.get("ATLASQL_POOL_MAX_IDLE_S", "180"))

# Downloaded source archives live here. Everything under it is reproducible,
# so it is gitignored.
DATA_DIR = Path(os.environ.get("ATLASQL_DATA_DIR", REPO_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"

SQL_DIR = REPO_ROOT / "sql"

# Coverage threshold for level auto-detection, as a percentage. The plan starts
# at 80 and tunes once real data is loaded.
COVERAGE_THRESHOLD_PCT = float(os.environ.get("ATLASQL_COVERAGE_THRESHOLD_PCT", "80"))

# Most granular last: level auto-detection walks this in reverse.
LEVELS = ("continent", "country", "state", "county", "city")

# Above this many regions at a level, the query builder adds an EXISTS
# pre-filter per condition so the planner can start from the metric index
# instead of scanning the whole tier. Purely a plan choice — the rows are
# identical either way — and it is a loss on small tiers, so the threshold sits
# between the state tier (4,596) and the city tier (34,026). See
# `query.build_sql` for the measurements.
PREFILTER_MIN_REGIONS = int(os.environ.get("ATLASQL_PREFILTER_MIN_REGIONS", "10000"))


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


# Build the caches a cold process would otherwise build during someone's first
# page load. Off in tests, which import the app far more often than they call it.
WARM_ON_STARTUP = _flag("ATLASQL_WARM_ON_STARTUP", "1")
# What the frontend asks for on page load, so warming it is warming the real
# request rather than a plausible-looking one.
WARM_BASEMAP_LEVEL = os.environ.get("ATLASQL_WARM_BASEMAP_LEVEL", "country")
WARM_BASEMAP_TOLERANCE = float(os.environ.get("ATLASQL_WARM_BASEMAP_TOLERANCE", "0.25"))
