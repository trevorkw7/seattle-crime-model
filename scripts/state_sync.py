#!/usr/bin/env python
"""Sync persistent state (data/, artifacts/, export) with object storage.

    python scripts/state_sync.py pull|push

Uses SEACRIME_STATE_URI=gs://bucket/prefix (Google Cloud Storage via the
google-cloud-storage client, credentials from the environment / workload
identity). No-op when the variable is unset. Immutable raw snapshots and frozen
forecasts are only ever *added* remotely, never overwritten or deleted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

IMMUTABLE_PREFIXES = ("data/raw/", "data/prospective/forecasts/")


def _dirs() -> dict[str, Path]:
    root = Path(__file__).resolve().parents[1]
    return {
        "data": Path(os.environ.get("SEACRIME_DATA_ROOT", root / "data")),
        "artifacts": Path(os.environ.get("SEACRIME_ARTIFACTS_ROOT", root / "artifacts")),
        "export": Path(os.environ.get("SEACRIME_EXPORT_DIR", root / "web" / "public" / "data")),
    }


def main() -> None:
    uri = os.environ.get("SEACRIME_STATE_URI")
    if not uri:
        print("SEACRIME_STATE_URI not set; nothing to sync")
        return
    if not uri.startswith("gs://"):
        raise SystemExit("only gs:// URIs are implemented")
    from google.cloud import storage  # optional dependency

    bucket_name, _, prefix = uri[5:].partition("/")
    bucket = storage.Client().bucket(bucket_name)
    mode = sys.argv[1]
    for name, local in _dirs().items():
        rprefix = f"{prefix.rstrip('/')}/{name}/".lstrip("/")
        if mode == "pull":
            for blob in bucket.list_blobs(prefix=rprefix):
                dest = local / blob.name[len(rprefix):]
                if dest.exists() and dest.stat().st_size == blob.size:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                blob.download_to_filename(dest)
        elif mode == "push":
            if not local.exists():
                continue
            existing = {b.name: b.size for b in bucket.list_blobs(prefix=rprefix)}
            for f in local.rglob("*"):
                if not f.is_file():
                    continue
                key = rprefix + str(f.relative_to(local))
                rel = f"{name}/{f.relative_to(local)}"
                if key in existing and (rel.startswith(IMMUTABLE_PREFIXES) or existing[key] == f.stat().st_size):
                    continue
                bucket.blob(key).upload_from_filename(f)
        else:
            raise SystemExit("usage: state_sync.py pull|push")


if __name__ == "__main__":
    main()
