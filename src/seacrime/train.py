"""Train *production* models used for live (prospective) forecasting.

    python -m seacrime.train --categories property violent all_located

Training windows end before the maturity cutoff (``now - maturity_days``) so
that targets are not biased low by late-published reports; the final
``val_days`` before the cutoff are used for early stopping and to estimate the
negative-binomial dispersion. Out-of-sample accuracy comes from the backtests.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime

import pandas as pd

from .config import Config, load_config
from .evaluation.backtest import build_xy, issue_times, load_env, make_model, tune_baselines
from .features import FeatureConfig

log = logging.getLogger(__name__)


def production_dir(cfg: Config):
    return cfg.artifacts_root / "models" / "production"


def train_production(
    cfg: Config,
    categories: list[str],
    horizons: list[int],
    models: list[str],
    n_train: int = 3000,
    n_val: int = 700,
    val_days: int = 120,
) -> dict:
    reg_p = production_dir(cfg) / "registry.json"
    registry = json.loads(reg_p.read_text()) if reg_p.exists() else {}
    for cat in categories:
        env = load_env(cfg, cat)
        data_end = pd.Timestamp(env.target.meta.get("data_end", env.target.meta["end"]))
        cutoff = (data_end - pd.Timedelta(days=cfg["availability"]["maturity_days"])).floor("D")
        val_start = cutoff - pd.Timedelta(days=val_days)
        train_start = pd.Timestamp(cfg["training"]["start"], tz="UTC") + pd.Timedelta(days=30)
        a = cfg["availability"]
        base = FeatureConfig(crime_lag_h=a["crime_feature_lag_h"], calls_lag_h=a["calls_feature_lag_h"], fire_lag_h=a["fire911_feature_lag_h"])
        origin = env.target.origin
        for h in horizons:
            t_tr = issue_times(origin, train_start, val_start, h, n=n_train, seed=h)
            t_va = issue_times(origin, val_start, cutoff, h, n=n_val, seed=100 + h)
            fcfg = tune_baselines(env, base, t_va[:300], h)
            fb = env.builder(fcfg)
            X_tr, y_tr = build_xy(fb, t_tr, h)
            X_va, y_va = build_xy(fb, t_va, h)
            for m in models:
                model = make_model(m)
                model.fit(X_tr, y_tr, X_va, y_va)
                model.train_info = {
                    "category": cat,
                    "event_time": env.event_time,
                    "horizon_h": h,
                    "train_window": [train_start.isoformat(), val_start.isoformat()],
                    "val_window": [val_start.isoformat(), cutoff.isoformat()],
                    "n_train_rows": int(len(y_tr)),
                    "data_versions": env.data_versions,
                    "feature_config": asdict(fcfg),
                }
                d = production_dir(cfg) / cat / f"h{h}" / m
                model.save(d)
                registry.setdefault(cat, {}).setdefault(str(h), {})[m] = {
                    "version": model.version,
                    "path": str(d.relative_to(cfg.artifacts_root)),
                    "trained_at": model.trained_at,
                    "alpha": model.alpha,
                }
                log.info("trained %s/%s/h%d %s", cat, m, h, model.version)
        registry["_updated_at"] = datetime.now(UTC).isoformat()
        reg_p.parent.mkdir(parents=True, exist_ok=True)
        reg_p.write_text(json.dumps(registry, indent=2))
    return registry


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", nargs="+", default=["property", "violent", "all_located"])
    ap.add_argument("--horizons", type=int, nargs="+", default=None)
    ap.add_argument("--models", nargs="+", default=["seasonal", "hotspot", "lgbm"])
    ap.add_argument("--n-train", type=int, default=3000)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config()
    train_production(cfg, args.categories, args.horizons or cfg.horizons, args.models, n_train=args.n_train)


if __name__ == "__main__":
    main()
