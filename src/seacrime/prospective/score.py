"""Score frozen forecasts against newly published incidents.

A window is *mature* once ``window_end + maturity_days`` has passed at the time
of the SPD snapshot used for scoring; before that, scores are *provisional* and
are recomputed on every new snapshot (late reports may still arrive). An
incident missing from an immature snapshot is never treated as definitively
nonexistent: provisional scores are labelled as such and stored per snapshot.
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from ..build import cube_dir
from ..config import Config
from ..evaluation.metrics import per_issue_stats, summarize
from ..grid import Grid
from ..incidents import incidents, load_store
from .forecast import prospective_root
from .store import ForecastStore

log = logging.getLogger(__name__)


def observed_counts(inc: pd.DataFrame, grid: Grid, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    m = (inc["ts_utc"] >= start) & (inc["ts_utc"] < end)
    cells = grid.locate(inc.loc[m, "lon"].to_numpy(), inc.loc[m, "lat"].to_numpy())
    cells = cells[cells >= 0]
    return pd.Series(cells).value_counts()


def score_all(cfg: Config, now: pd.Timestamp | None = None) -> int:
    store = ForecastStore(prospective_root(cfg))
    fc = store.list_forecasts()
    if fc.empty:
        return 0
    spd = load_store(cfg.processed_dir)
    snapshot_id = str(spd["last_seen_snapshot"].max())
    snapshot_time = pd.Timestamp(spd["last_seen_at_utc"].max())
    now = pd.Timestamp(now) if now is not None else snapshot_time
    maturity = pd.Timedelta(days=cfg["availability"]["maturity_days"])
    grid = Grid.load(cube_dir(cfg) / "grid.json")
    latest = store.latest_scores()
    final = set()
    if not latest.empty:
        final = set(zip(latest.loc[latest["maturity"] == "mature", "forecast_id"], latest.loc[latest["maturity"] == "mature", "horizon_h"], strict=True))
    # windows already scored against this snapshot (cycles run hourly; SPD publishes daily)
    done = store.scored_against(snapshot_id)
    inc_cache: dict[str, pd.DataFrame] = {}
    n = 0
    for _, f in fc.iterrows():
        t0 = pd.Timestamp(f["t0"])
        cat = f["category"]
        if cat not in inc_cache:
            inc_cache[cat] = incidents(spd, cfg["categories"][cat], event_time=f["event_time"])
        inc = inc_cache[cat]
        preds = None
        for h in json.loads(f["horizons"]):
            end = t0 + pd.Timedelta(hours=h)
            if end > snapshot_time or (f["forecast_id"], h) in final or (f["forecast_id"], h) in done:
                continue  # window not over (in published data), already final, or unchanged since last scored
            if preds is None:
                preds = store.load_predictions(f["forecast_id"])  # verifies SHA-256
            p = preds[preds["horizon_h"] == h]
            if p.empty:
                continue
            obs = observed_counts(inc, grid, t0, end).reindex(p["cell"].to_numpy(), fill_value=0).to_numpy().astype(float)
            mu = p["expected"].to_numpy()
            st = per_issue_stats(np.zeros(len(mu), dtype=np.int64), obs, mu, p["p_any"].to_numpy(), 0.0, len(mu))
            met = summarize(st, len(mu))
            met["observed_cells"] = {str(c): int(v) for c, v in zip(p["cell"].to_numpy(), obs, strict=True) if v > 0}
            maturity_label = "mature" if end + maturity <= snapshot_time else "provisional"
            met["window_age_days_at_scoring"] = float((snapshot_time - end) / pd.Timedelta(days=1))
            store.add_score(f["forecast_id"], int(h), snapshot_id, maturity_label, met)
            n += 1
    log.info("scored %d forecast windows against snapshot %s", n, snapshot_id)
    return n
