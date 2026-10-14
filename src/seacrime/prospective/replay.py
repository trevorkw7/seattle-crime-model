"""Historical *replay* forecasts: reconstruct forecasts as of past issue times
using only data that would have been available then and models trained before
the backtest test period. They exercise the full freeze -> score pipeline on
real outcomes and power the dashboard's replay view, but are always labelled
``provenance='replay'`` and never mixed with live prospective results.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Config
from ..evaluation.backtest import load_env
from ..features import FeatureConfig
from ..models.base import CountModel
from .forecast import prospective_root
from .store import ForecastStore

log = logging.getLogger(__name__)


def replay_forecasts(cfg: Config, run_dir: Path, category: str, models: list[str], t0s: list[pd.Timestamp]) -> list[str]:
    store = ForecastStore(prospective_root(cfg))
    env = load_env(cfg, category)
    horizons = cfg.horizons
    a = cfg["availability"]
    ids = []
    builders: dict = {}
    for m in models:
        loaded = {}
        for h in horizons:
            d = run_dir / "models" / f"h{h}" / m
            if (d / "model.pkl").exists():
                loaded[h] = CountModel.load(d)
        if not loaded:
            continue
        for t0 in t0s:
            t0 = pd.Timestamp(t0).tz_convert("UTC")
            t_idx = int((t0 - env.target.origin) // pd.Timedelta(hours=1))
            frames = []
            for h, model in loaded.items():
                fcfg = FeatureConfig(**model.train_info["feature_config"])
                key = repr(sorted(fcfg.__dict__.items()))
                fb = builders.get(key) or builders.setdefault(key, env.builder(fcfg))
                if hasattr(model, "attach"):
                    model.attach(fb, h)
                X, _ = fb.build(np.array([t_idx]), h, with_target=False)
                d = model.predict_dist(X)
                d["relative_risk"] = d["expected"].to_numpy() / np.maximum(X["base_seasonal"].to_numpy(), 1e-6)
                d["cell"] = X["_cell"].to_numpy()
                d["horizon_h"] = h
                frames.append(d)
            preds = pd.concat(frames, ignore_index=True)
            fid = f"rp_{t0.strftime('%Y%m%dT%H%MZ')}_{m}_{category}"
            meta = {
                "forecast_id": fid,
                "issued_at": (t0 - pd.Timedelta(minutes=10)).isoformat(),
                "t0": t0.isoformat(),
                "model_id": m,
                "model_version": next(iter(loaded.values())).version,
                "category": category,
                "event_time": env.event_time,
                "horizons": sorted(loaded),
                "data_versions": env.data_versions,
                "feature_cutoffs": {
                    "spd_crime": (t0 - pd.Timedelta(hours=a["crime_feature_lag_h"])).isoformat(),
                    "spd_calls": (t0 - pd.Timedelta(hours=a["calls_feature_lag_h"])).isoformat(),
                    "fire_911": (t0 - pd.Timedelta(hours=a["fire911_feature_lag_h"])).isoformat(),
                    "note": "as-of reconstruction from the current snapshot (true historical publication state unknown)",
                },
                "provenance": "replay",
            }
            try:
                store.freeze(meta, preds)
                ids.append(fid)
            except FileExistsError:
                pass
    log.info("replay: froze %d forecasts for %s", len(ids), category)
    return ids


def default_replay_times(data_end: pd.Timestamp, days: int = 45, local_hour: int = 17) -> list[pd.Timestamp]:
    """Daily issue times at a fixed local hour over the last ``days`` days whose
    24 h windows end before ``data_end``."""
    end_local = data_end.tz_convert("America/Los_Angeles")
    out = []
    for k in range(days, 0, -1):
        d = (end_local - pd.Timedelta(days=k)).normalize() + pd.Timedelta(hours=local_hour)
        t0 = d.tz_convert("UTC")
        if t0 + pd.Timedelta(hours=24) <= data_end:
            out.append(t0)
    return out
