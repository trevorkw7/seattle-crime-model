"""Issue and freeze live forecasts for every cell, horizon, model and category.

The earliest forecast window starts at the next whole hour after the issue
(wall-clock) time, so every window opens strictly after the forecast is frozen.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from ..build import CUBE_ORIGIN
from ..config import Config
from ..evaluation.backtest import load_env
from ..features import FeatureConfig
from ..incidents import load_store
from ..models.base import CountModel
from ..train import production_dir
from .store import ForecastStore

log = logging.getLogger(__name__)


def prospective_root(cfg: Config):
    return cfg.data_root / "prospective"


def live_weather(cfg: Config, wx_obs: pd.DataFrame | None, origin: pd.Timestamp) -> pd.DataFrame | None:
    """Observed hourly weather for the live issue time: archived NCEI observations
    (lag ~40 h) topped up with real-time METAR observations from api.weather.gov.
    Window weather itself comes from the latest archived MOS run (forecast mode)."""
    try:
        from ..ingest.weather import fetch_nws_observations

        ob = fetch_nws_observations(cfg)
    except Exception as e:  # network or API failure -> archived observations only
        log.warning("NWS observations unavailable: %s", e)
        return wx_obs
    if ob is None or ob.empty:
        return wx_obs
    h = ((pd.to_datetime(ob["ts_utc"], utc=True) - origin) // pd.Timedelta(hours=1)).astype(np.int64)
    f = ob.assign(h=h).set_index("h")[["temp_c", "precip_mm", "wind_ms"]]
    if wx_obs is None:
        return f
    return pd.concat([wx_obs[~wx_obs.index.isin(f.index)], f]).sort_index()


def issue_forecasts(cfg: Config, categories: list[str], models: list[str] | None = None, now: pd.Timestamp | None = None) -> list[str]:
    store = ForecastStore(prospective_root(cfg))
    reg_p = production_dir(cfg) / "registry.json"
    if not reg_p.exists():
        raise FileNotFoundError("no production models; run `python -m seacrime.train` first")
    registry = json.loads(reg_p.read_text())
    issued_at = pd.Timestamp(now) if now is not None else pd.Timestamp(datetime.now(UTC))
    t0 = issued_at.ceil("h")
    if t0 == issued_at:
        t0 = t0 + pd.Timedelta(hours=1)
    horizons = cfg.horizons
    a = cfg["availability"]
    spd = load_store(cfg.processed_dir)
    latest_report = pd.Timestamp(spd.loc[spd["present_in_latest"], "report_ts_utc"].max())
    ids = []
    for cat in categories:
        env = load_env(cfg, cat)
        cube_end = pd.Timestamp(env.target.meta["end"])
        if cube_end < t0 + pd.Timedelta(hours=max(horizons)):
            raise RuntimeError(f"cube ends {cube_end}; rebuild cubes with end >= {t0 + pd.Timedelta(hours=max(horizons))}")
        if env.ctx is not None:
            env.ctx.weather = live_weather(cfg, env.ctx.weather, CUBE_ORIGIN)
        t_idx = int((t0 - env.target.origin) // pd.Timedelta(hours=1))
        fire_latest = _latest_ts(cfg, "fire_911.parquet")
        calls_latest = _latest_ts(cfg, "spd_calls.parquet")
        cutoffs = {
            "spd_crime": min(t0 - pd.Timedelta(hours=a["crime_feature_lag_h"]), latest_report).isoformat(),
            "spd_calls": min(t0 - pd.Timedelta(hours=a["calls_feature_lag_h"]), calls_latest).isoformat() if calls_latest is not None else None,
            "fire_911": min(t0 - pd.Timedelta(hours=a["fire911_feature_lag_h"]), fire_latest).isoformat() if fire_latest is not None else None,
        }
        for m in models or sorted({k for h in registry.get(cat, {}).values() if isinstance(h, dict) for k in h}):
            frames = []
            version = None
            for h in horizons:
                info = registry.get(cat, {}).get(str(h), {}).get(m)
                if info is None:
                    continue
                model = CountModel.load(cfg.artifacts_root / info["path"])
                fcfg = FeatureConfig(**model.train_info["feature_config"])
                fb = env.builder(fcfg)
                X, _ = fb.build(np.array([t_idx]), h, with_target=False)
                d = model.predict_dist(X)
                d["relative_risk"] = d["expected"].to_numpy() / np.maximum(X["base_seasonal"].to_numpy(), 1e-6)
                d["cell"] = X["_cell"].to_numpy()
                d["horizon_h"] = h
                frames.append(d)
                version = model.version if version is None else version
            if not frames:
                continue
            preds = pd.concat(frames, ignore_index=True)
            fid = f"fc_{t0.strftime('%Y%m%dT%H%MZ')}_{m}_{cat}"
            meta = {
                "forecast_id": fid,
                "issued_at": issued_at.isoformat(),
                "t0": t0.isoformat(),
                "model_id": m,
                "model_version": version,
                "category": cat,
                "event_time": env.event_time,
                "horizons": horizons,
                "data_versions": env.data_versions,
                "feature_cutoffs": cutoffs,
                "provenance": "actual",
            }
            try:
                store.freeze(meta, preds)
                ids.append(fid)
                log.info("frozen %s (%d rows)", fid, len(preds))
            except FileExistsError:
                log.info("forecast %s already exists; skipping", fid)
    return ids


def _latest_ts(cfg: Config, name: str) -> pd.Timestamp | None:
    p = cfg.processed_dir / name
    if not p.exists():
        return None
    return pd.Timestamp(pd.read_parquet(p, columns=["ts_utc"])["ts_utc"].max())


def model_feature_config(model: CountModel) -> FeatureConfig:
    return FeatureConfig(**model.train_info["feature_config"])


__all__ = ["issue_forecasts", "prospective_root", "asdict"]
