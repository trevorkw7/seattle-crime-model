"""Append-only store for frozen forecasts, scores and job runs.

* Metadata lives in SQLite (``data/prospective/registry.sqlite``).
* Cell-level predictions live in ``data/prospective/forecasts/<id>.parquet``.
  At freeze time the file is written once, made read-only and its SHA-256 is
  recorded; scoring verifies the hash so a forecast cannot be silently edited
  after its window opened.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
  forecast_id TEXT PRIMARY KEY,
  issued_at TEXT NOT NULL,          -- wall-clock UTC time the forecast was frozen
  t0 TEXT NOT NULL,                 -- start of the earliest window (>= issued_at)
  model_id TEXT NOT NULL,
  model_version TEXT NOT NULL,
  category TEXT NOT NULL,
  event_time TEXT NOT NULL,
  horizons TEXT NOT NULL,           -- json list
  data_versions TEXT NOT NULL,      -- json {source: snapshot_id}
  feature_cutoffs TEXT NOT NULL,    -- json {source: iso time of newest usable data}
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  provenance TEXT NOT NULL          -- actual | demo
);
CREATE TABLE IF NOT EXISTS scores (
  forecast_id TEXT NOT NULL,
  horizon_h INTEGER NOT NULL,
  scored_at TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  maturity TEXT NOT NULL,           -- provisional | mature
  metrics TEXT NOT NULL,            -- json
  PRIMARY KEY (forecast_id, horizon_h, snapshot_id)
);
CREATE TABLE IF NOT EXISTS jobs (
  run_id TEXT NOT NULL,
  job TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  message TEXT,
  PRIMARY KEY (run_id, job)
);
"""


class ForecastStore:
    def __init__(self, root: Path):
        self.root = root
        self.fdir = root / "forecasts"
        self.fdir.mkdir(parents=True, exist_ok=True)
        self.db = root / "registry.sqlite"
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ------------------------------------------------------------- forecasts
    def freeze(self, meta: dict, preds: pd.DataFrame) -> str:
        """Persist a forecast immutably. ``meta['t0']`` must not precede issue time."""
        issued = pd.Timestamp(meta["issued_at"])
        t0 = pd.Timestamp(meta["t0"])
        if t0 < issued:
            raise ValueError(f"forecast window starts {t0} before it was frozen {issued}")
        fid = meta["forecast_id"]
        path = self.fdir / f"{fid}.parquet"
        if path.exists():
            raise FileExistsError(f"forecast {fid} already frozen")
        preds.to_parquet(path, index=False)
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        digest = sha256(path)
        with self.conn() as c:
            c.execute(
                "INSERT INTO forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fid,
                    issued.isoformat(),
                    t0.isoformat(),
                    meta["model_id"],
                    meta["model_version"],
                    meta["category"],
                    meta.get("event_time", "report"),
                    json.dumps(meta["horizons"]),
                    json.dumps(meta["data_versions"]),
                    json.dumps(meta["feature_cutoffs"]),
                    str(path.relative_to(self.root)),
                    digest,
                    meta.get("provenance", "actual"),
                ),
            )
        return fid

    def list_forecasts(self) -> pd.DataFrame:
        with self.conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM forecasts ORDER BY t0")]
        return pd.DataFrame(rows)

    def load_predictions(self, forecast_id: str, verify: bool = True) -> pd.DataFrame:
        with self.conn() as c:
            r = c.execute("SELECT path, sha256 FROM forecasts WHERE forecast_id=?", (forecast_id,)).fetchone()
        if r is None:
            raise KeyError(forecast_id)
        path = self.root / r["path"]
        if verify and sha256(path) != r["sha256"]:
            raise RuntimeError(f"integrity check failed for frozen forecast {forecast_id}")
        return pd.read_parquet(path)

    # ---------------------------------------------------------------- scores
    def add_score(self, forecast_id: str, horizon: int, snapshot_id: str, maturity: str, metrics: dict) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?)",
                (forecast_id, horizon, now_iso(), snapshot_id, maturity, json.dumps(metrics)),
            )

    def scores(self) -> pd.DataFrame:
        with self.conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM scores")]
        df = pd.DataFrame(rows)
        if len(df):
            df["metrics"] = df["metrics"].map(json.loads)
        return df

    def latest_scores(self) -> pd.DataFrame:
        """Most recent score per (forecast, horizon); mature scores are final."""
        s = self.scores()
        if s.empty:
            return s
        s = s.sort_values(["forecast_id", "horizon_h", "scored_at"])
        return s.groupby(["forecast_id", "horizon_h"]).tail(1).reset_index(drop=True)

    # ------------------------------------------------------------------ jobs
    def job_start(self, run_id: str, job: str) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?)", (run_id, job, now_iso(), None, "running", None))

    def job_end(self, run_id: str, job: str, status: str, message: str = "") -> None:
        with self.conn() as c:
            c.execute("UPDATE jobs SET finished_at=?, status=?, message=? WHERE run_id=? AND job=?", (now_iso(), status, message[:2000], run_id, job))

    def jobs(self) -> pd.DataFrame:
        with self.conn() as c:
            return pd.DataFrame([dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY started_at")])


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
