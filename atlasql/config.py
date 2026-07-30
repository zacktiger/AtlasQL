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

DATABASE_URL = os.environ.get("ATLASQL_DATABASE_URL", DEFAULT_DATABASE_URL)

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
