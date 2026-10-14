"""Shared helpers for the *contextual* ingesters (weather, events, GTFS, boundaries, news).

Every contextual download follows the same immutable-snapshot convention as
:mod:`seacrime.ingest.socrata`::

    data/raw/<source>/<snapshot_id YYYYmmddTHHMMSSZ>/<payload files>
    data/raw/<source>/<snapshot_id>/manifest.json

Payloads are stored exactly as received (gzip-compressed with ``mtime=0`` so the
stored bytes are deterministic); the manifest records each URL + query, the
sha256 of the *received* bytes, our fetch time (the availability timestamp),
row counts, licence/terms notes and any blocker we hit.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from .socrata import USER_AGENT

log = logging.getLogger(__name__)

RETRY_STATUSES = (429, 500, 502, 503, 504)


class HttpError(RuntimeError):
    """Non-retryable HTTP failure (or retries exhausted); keeps the status for manifests."""

    def __init__(self, url: str, status: int | None, detail: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {detail[:300]}")
        self.url = url
        self.status = status
        self.detail = detail


def session(extra_headers: Mapping[str, str] | None = None) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    if extra_headers:
        s.headers.update(extra_headers)
    return s


def http_get(
    sess: requests.Session,
    url: str,
    params: Mapping[str, Any] | None = None,
    *,
    retries: int = 4,
    backoff_s: float = 3.0,
    timeout: float = 180.0,
    headers: Mapping[str, str] | None = None,
) -> requests.Response:
    """GET with polite exponential backoff on 429/5xx and connection errors.

    Other 4xx responses raise :class:`HttpError` immediately (no point retrying
    a 403/404). ``Retry-After`` is honoured when the server sends it.
    """
    delay = backoff_s
    last: HttpError | None = None
    for attempt in range(retries + 1):
        try:
            r = sess.get(url, params=params, timeout=timeout, headers=headers)
        except requests.RequestException as e:  # TLS reset, timeout, ...
            last = HttpError(url, None, repr(e))
        else:
            if r.status_code == 200:
                return r
            last = HttpError(url, r.status_code, r.text)
            if r.status_code not in RETRY_STATUSES:
                raise last
            ra = r.headers.get("Retry-After")
            delay = max(delay, float(ra)) if ra and ra.isdigit() else delay
        if attempt < retries:
            log.warning(
                "%s on %s; retry %d/%d in %.0fs",
                f"HTTP {last.status}" if last.status else last.detail[:80],
                url,
                attempt + 1,
                retries,
                delay,
            )
            time.sleep(delay)
            delay *= 2
    assert last is not None
    raise last


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class RawSnapshot:
    """An immutable raw snapshot directory plus its manifest under construction."""

    def __init__(self, raw_dir: Path, source: str, fetched_at: datetime | None = None) -> None:
        self.fetched_at = fetched_at or datetime.now(UTC)
        self.snapshot_id = self.fetched_at.strftime("%Y%m%dT%H%M%SZ")
        self.source = source
        self.dir = raw_dir / source / self.snapshot_id
        self.dir.mkdir(parents=True, exist_ok=False)
        self.files: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}
        self.blockers: list[dict[str, Any]] = []

    def save(
        self,
        name: str,
        content: bytes,
        url: str | None = None,
        params: Mapping[str, Any] | None = None,
        compress: bool = True,
        **extra: Any,
    ) -> Path:
        """Store a received payload verbatim (gzip, deterministic) and register it."""
        path = self.dir / (name + (".gz" if compress else ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(content, mtime=0) if compress else content)
        self.files.append(
            {
                "file": str(path.relative_to(self.dir)),
                "url": url,
                "params": dict(params) if params else None,
                "received_bytes": len(content),
                "sha256": sha256_bytes(content),
                "retrieved_at_utc": datetime.now(UTC).isoformat(),
                **extra,
            }
        )
        return path

    def blocked(self, what: str, url: str, status: int | None, detail: str) -> None:
        log.warning("BLOCKED %s (%s): HTTP %s %s", what, url, status, detail[:200])
        self.blockers.append(
            {"what": what, "url": url, "http_status": status, "detail": detail[:500], "at_utc": datetime.now(UTC).isoformat()}
        )

    def finish(self, **extra: Any) -> Path:
        manifest = {
            "source": self.source,
            "snapshot_id": self.snapshot_id,
            "fetched_at_utc": self.fetched_at.isoformat(),
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "files": self.files,
            "content_sha256": sha256_bytes("".join(f["sha256"] for f in self.files).encode()),
            "blockers": self.blockers,
            **self.meta,
            **extra,
        }
        (self.dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
        return self.dir / "manifest.json"


def read_gz(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()


def local_to_utc(values: Iterable[Any] | pd.Series, tz: str, ambiguous: str = "earliest") -> pd.Series:
    """Convert *floating* local wall-clock times (no offset) to tz-aware UTC.

    * Nonexistent local times (spring-forward gap, 02:00-02:59) are shifted
      forward to 03:00 local (``nonexistent='shift_forward'``).
    * Ambiguous local times (fall-back hour 01:00-01:59 occurs twice) are
      resolved by ``ambiguous``: ``"earliest"`` = first occurrence (PDT, the
      default), ``"latest"`` = second occurrence (PST), ``"NaT"`` = drop.
      Seattle's open data carry no DST flag, so the choice is unknowable from
      the data itself; the earliest policy is documented in
      docs/DATA_SOURCES_CONTEXT.md.
    """
    s = pd.Series(pd.to_datetime(pd.Series(values), errors="coerce"))
    if getattr(s.dt, "tz", None) is not None:
        return s.dt.tz_convert("UTC")
    if ambiguous == "earliest":
        amb: Any = np.ones(len(s), dtype=bool)
    elif ambiguous == "latest":
        amb = np.zeros(len(s), dtype=bool)
    else:
        amb = ambiguous
    out = s.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous=amb).dt.tz_convert("UTC")
    return out.astype("datetime64[ns, UTC]")


def to_utc(values: Iterable[Any] | pd.Series) -> pd.Series:
    """Parse timestamps that carry an explicit offset / 'Z' (or are known UTC) to UTC."""
    return pd.Series(pd.to_datetime(pd.Series(values), utc=True, errors="coerce", format="mixed")).astype("datetime64[ns, UTC]")


def write_parquet(df: pd.DataFrame, path: Path) -> Path:
    """Atomic parquet write (GeoDataFrames are written as GeoParquet)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)
    return path
