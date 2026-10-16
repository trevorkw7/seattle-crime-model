#!/usr/bin/env python
"""State housekeeping for the scheduled live-forecast job (.github/workflows/live-forecast.yml).

    python scripts/live_state.py status [--retrain-days 7]   # what this run must do
    python scripts/live_state.py prune                        # keep only the latest raw snapshot per source
    python scripts/live_state.py pack-store OUT.tar.gz        # archive the frozen-forecast store
    python scripts/live_state.py unpack-store IN.tar.gz       # restore it (refuses to overwrite)

The job keeps processed data, production models and the forecast store in the
GitHub Actions cache between runs; cubes are rebuilt every run. Raw snapshots are
only needed for change detection (manifests) and reprocessing the newest pull, so
older ones are dropped before the cache is saved. The forecast store is also
backed up as a release asset, because cache entries can be evicted.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seacrime.config import load_config  # noqa: E402
from seacrime.prospective.forecast import prospective_root  # noqa: E402
from seacrime.train import production_dir  # noqa: E402


def status(retrain_days: float) -> dict:
    cfg = load_config()
    reg_p = production_dir(cfg) / "registry.json"
    bootstrap = not (cfg.processed_dir / "spd_offenses.parquet").exists()
    retrain, trained_at = True, None
    if reg_p.exists():
        reg = json.loads(reg_p.read_text())
        stamps = [info.get("trained_at") for cat, hs in reg.items() if not cat.startswith("_") for ms in hs.values() for info in ms.values()]
        stamps = [datetime.fromisoformat(s) for s in stamps if s]
        if stamps:
            trained_at = min(stamps)
            retrain = datetime.now(UTC) - trained_at > timedelta(days=retrain_days)
    store = prospective_root(cfg) / "registry.sqlite"
    out = {"bootstrap": bootstrap, "retrain": bootstrap or retrain, "trained_at": trained_at.isoformat() if trained_at else None,
           "has_store": store.exists()}
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            for k in ("bootstrap", "retrain", "has_store"):
                f.write(f"{k}={'true' if out[k] else 'false'}\n")
    return out


def prune() -> dict:
    cfg = load_config()
    removed = 0
    freed = 0
    if cfg.raw_dir.exists():
        for src in sorted(p for p in cfg.raw_dir.iterdir() if p.is_dir()):
            snaps = sorted(p for p in src.iterdir() if p.is_dir() and (p / "manifest.json").exists())
            for old in snaps[:-1]:
                freed += sum(f.stat().st_size for f in old.rglob("*") if f.is_file())
                shutil.rmtree(old)
                removed += 1
    # leftovers from interrupted incremental merges
    for tmp in cfg.processed_dir.glob("_*.parquet"):
        tmp.unlink()
    return {"removed_snapshots": removed, "freed_mb": round(freed / 1e6, 1)}


def pack_store(out: Path) -> dict:
    root = prospective_root(load_config())
    if not (root / "registry.sqlite").exists():
        raise SystemExit(f"no forecast store at {root}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        tar.add(root, arcname="prospective")
    return {"archive": str(out), "mb": round(out.stat().st_size / 1e6, 2)}


def unpack_store(src: Path) -> dict:
    root = prospective_root(load_config())
    if (root / "registry.sqlite").exists():
        raise SystemExit(f"refusing to overwrite the existing store at {root}")
    root.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name == "prospective" or m.name.startswith("prospective/")]
        bad = [m.name for m in members if m.issym() or m.islnk() or ".." in Path(m.name).parts]
        if bad:
            raise SystemExit(f"unsafe archive members: {bad[:3]}")
        tar.extractall(root.parent, members=members, filter="data")
    return {"restored_to": str(root)}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--retrain-days", type=float, default=7.0)
    sub.add_parser("prune")
    p = sub.add_parser("pack-store")
    p.add_argument("out", type=Path)
    u = sub.add_parser("unpack-store")
    u.add_argument("src", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "status":
        res = status(a.retrain_days)
    elif a.cmd == "prune":
        res = prune()
    elif a.cmd == "pack-store":
        res = pack_store(a.out)
    else:
        res = unpack_store(a.src)
    print(json.dumps(res))


if __name__ == "__main__":
    main()
