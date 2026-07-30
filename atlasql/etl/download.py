"""Cached downloads for ETL source archives.

Source files are large and immutable for a given vintage, so they are fetched
once into data/raw and reused. Re-running an ETL job costs no bandwidth.
"""

from __future__ import annotations

import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from atlasql import config

log = logging.getLogger(__name__)


ATTEMPTS_PER_SOURCE = 3


def fetch(
    url: str,
    filename: str | None = None,
    mirrors: list[str] | None = None,
    attempts: int = ATTEMPTS_PER_SOURCE,
) -> Path:
    """Download `url` into data/raw unless it is already there.

    `mirrors` are tried in order if the primary host fails; Natural Earth's CDN
    in particular goes down often enough to be worth a fallback.

    Each source is retried, because the failure that actually happens on these
    hosts is a truncated body rather than a refused connection - and a
    truncated tile silently shrinks a metric's coverage, which silently changes
    which level a query runs at.
    """
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    target = config.RAW_DIR / (filename or url.rsplit("/", 1)[-1])
    if target.exists() and target.stat().st_size > 0:
        log.info("using cached %s", target.name)
        return target

    errors: list[str] = []
    for candidate in [url, *(mirrors or [])]:
        for attempt in range(1, attempts + 1):
            try:
                log.info("downloading %s", candidate)
                # Written to a .part file first so an interrupted download is
                # never mistaken for a cache hit next run.
                partial = target.with_suffix(target.suffix + ".part")
                with httpx.stream(
                    "GET", candidate, follow_redirects=True, timeout=120.0
                ) as response:
                    response.raise_for_status()
                    expected = response.headers.get("content-length")
                    written = 0
                    with partial.open("wb") as handle:
                        for chunk in response.iter_bytes(chunk_size=1 << 20):
                            handle.write(chunk)
                            written += len(chunk)
                if expected is not None and written != int(expected):
                    raise OSError(
                        f"truncated download: got {written} bytes, expected {expected}"
                    )
                partial.replace(target)
                log.info("saved %s (%.1f MB)", target.name, target.stat().st_size / 1e6)
                return target
            except Exception as exc:  # noqa: BLE001 - every attempt is reported
                errors.append(f"{candidate} (attempt {attempt}): {exc}")
                log.warning(
                    "download failed from %s (attempt %d/%d): %s",
                    candidate,
                    attempt,
                    attempts,
                    exc,
                )

    raise RuntimeError("all download sources failed:\n  " + "\n  ".join(errors))


def fetch_many(
    requests: list[tuple[str, str]], workers: int = 6
) -> dict[str, Path]:
    """Fetch several files concurrently, returning {filename: path}.

    Hosts serving these archives throttle each connection rather than the
    client, so several connections at once is the difference between minutes
    and hours on a tiled dataset. Failures are raised after every download has
    been attempted, so one missing tile does not hide the rest.
    """
    results: dict[str, Path] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch, url, filename): filename for url, filename in requests
        }
        for future in as_completed(futures):
            filename = futures[future]
            try:
                results[filename] = future.result()
            except Exception as exc:  # noqa: BLE001 - collected and reported below
                errors.append(f"{filename}: {exc}")
    if errors:
        log.warning("%d of %d downloads failed:\n  %s", len(errors), len(requests), "\n  ".join(errors))
    return results


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
