"""Generic, resumable Socrata (data.seattle.gov) snapshot downloader.

Every download is stored as an immutable *snapshot*:

    data/raw/<source>/<snapshot_id>/part-00000.parquet ...
    data/raw/<source>/<snapshot_id>/manifest.json

The manifest records the exact query, the dataset's own ``rowsUpdatedAt``
(publisher-side version), our fetch time (availability timestamp), row counts and
content hashes. These manifests are the "data-source versions" referenced by every
forecast.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

log = logging.getLogger(__name__)

BASE = "https://data.seattle.gov"
USER_AGENT = "seacrime-research/0.1 (+https://github.com/trevorkw7/seattle-crime-model)"


@dataclass
class SocrataQuery:
    dataset_id: str
    select: str | None = None
    where: str | None = None
    order: str | None = None
    page_size: int = 100_000

    def params(self, offset: int) -> dict[str, Any]:
        p: dict[str, Any] = {"$limit": self.page_size, "$offset": offset}
        if self.select:
            p["$select"] = self.select
        if self.where:
            p["$where"] = self.where
        if self.order:
            p["$order"] = self.order
        return p


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _get(session: requests.Session, url: str, params: dict | None = None, retries: int = 5) -> requests.Response:
    delay = 2.0
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=300)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                log.warning("HTTP %s from %s; retrying in %.0fs", r.status_code, url, delay)
            else:
                r.raise_for_status()
        except requests.RequestException as e:  # network hiccup
            if attempt == retries - 1:
                raise
            log.warning("request error %s; retrying in %.0fs", e, delay)
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"failed after {retries} retries: {url}")


def dataset_metadata(dataset_id: str) -> dict[str, Any]:
    r = _get(_session(), f"{BASE}/api/views/{dataset_id}.json")
    meta = r.json()
    return {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "rowsUpdatedAt": meta.get("rowsUpdatedAt"),
        "rowsUpdatedAt_iso": (
            datetime.fromtimestamp(meta["rowsUpdatedAt"], UTC).isoformat() if meta.get("rowsUpdatedAt") else None
        ),
        "license": (meta.get("license") or {}).get("name"),
        "attribution": meta.get("attribution"),
        "columns": [
            {"field": c["fieldName"], "type": c["dataTypeName"], "description": (c.get("description") or "")[:300]}
            for c in meta.get("columns", [])
        ],
    }


def snapshot(
    source: str,
    query: SocrataQuery,
    raw_dir: Path,
    snapshot_id: str | None = None,
    max_rows: int | None = None,
) -> Path:
    """Download a full query result into a new immutable snapshot directory."""
    fetched_at = datetime.now(UTC)
    snapshot_id = snapshot_id or fetched_at.strftime("%Y%m%dT%H%M%SZ")
    out = raw_dir / source / snapshot_id
    out.mkdir(parents=True, exist_ok=True)
    meta = dataset_metadata(query.dataset_id)
    session = _session()
    url = f"{BASE}/resource/{query.dataset_id}.json"
    offset, part, total = 0, 0, 0
    hashes = []
    while True:
        part_path = out / f"part-{part:05d}.parquet"
        if part_path.exists():  # resumable
            n = pd.read_parquet(part_path, columns=[]).shape[0]
        else:
            r = _get(session, url, query.params(offset))
            rows = r.json()
            n = len(rows)
            if n == 0:
                break
            df = pd.DataFrame.from_records(rows).astype("string")
            df.to_parquet(part_path, index=False)
        hashes.append(hashlib.sha256(part_path.read_bytes()).hexdigest())
        total += n
        offset += n
        part += 1
        log.info("%s: %d rows", source, total)
        if n < query.page_size or (max_rows and total >= max_rows):
            break
    manifest = {
        "source": source,
        "snapshot_id": snapshot_id,
        "fetched_at_utc": fetched_at.isoformat(),
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "endpoint": url,
        "query": {k: v for k, v in query.params(0).items() if not k.startswith("$offset")},
        "dataset_metadata": meta,
        "row_count": total,
        "n_parts": part,
        "sha256_parts": hashes,
        "content_sha256": hashlib.sha256("".join(hashes).encode()).hexdigest(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return out


def read_snapshot(path: Path) -> pd.DataFrame:
    parts = sorted(path.glob("part-*.parquet"))
    if not parts:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)


def latest_snapshot(raw_dir: Path, source: str) -> Path | None:
    d = raw_dir / source
    if not d.exists():
        return None
    snaps = sorted(p for p in d.iterdir() if (p / "manifest.json").exists())
    return snaps[-1] if snaps else None
