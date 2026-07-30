"""Cached downloads for ETL source archives.

Source files are large and immutable for a given vintage, so they are fetched
once into data/raw and reused. Re-running an ETL job costs no bandwidth.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import httpx

from atlasql import config

log = logging.getLogger(__name__)


def fetch(url: str, filename: str | None = None, mirrors: list[str] | None = None) -> Path:
    """Download `url` into data/raw unless it is already there.

    `mirrors` are tried in order if the primary host fails; Natural Earth's CDN
    in particular goes down often enough to be worth a fallback.
    """
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    target = config.RAW_DIR / (filename or url.rsplit("/", 1)[-1])
    if target.exists() and target.stat().st_size > 0:
        log.info("using cached %s", target.name)
        return target

    errors: list[str] = []
    for candidate in [url, *(mirrors or [])]:
        try:
            log.info("downloading %s", candidate)
            # Written to a .part file first so an interrupted download is never
            # mistaken for a cache hit next run.
            partial = target.with_suffix(target.suffix + ".part")
            with httpx.stream(
                "GET", candidate, follow_redirects=True, timeout=120.0
            ) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=1 << 20):
                        handle.write(chunk)
            partial.replace(target)
            log.info("saved %s (%.1f MB)", target.name, target.stat().st_size / 1e6)
            return target
        except Exception as exc:  # noqa: BLE001 - report every mirror's failure
            errors.append(f"{candidate}: {exc}")
            log.warning("download failed from %s: %s", candidate, exc)

    raise RuntimeError("all download sources failed:\n  " + "\n  ".join(errors))


def unzip(archive: Path, subdir: str | None = None) -> Path:
    """Extract `archive` into data/raw/<subdir> and return the directory."""
    destination = config.RAW_DIR / (subdir or archive.stem)
    if destination.exists() and any(destination.iterdir()):
        log.info("using extracted %s", destination.name)
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(destination)
    log.info("extracted %s -> %s", archive.name, destination)
    return destination
