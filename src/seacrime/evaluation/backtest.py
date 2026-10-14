"""Chronological backtests on identical test windows.

    python -m seacrime.evaluation.backtest --category property --horizons 1 3 6 24

Protocol
--------
* Issue times are hourly. For horizon h the target is the count in [t, t+h).
* Train windows end before ``val_start``; validation windows end before
  ``test_start - crime_lag`` (so everything used to fit/tune the model was
  publicly available at ``test_start``); test windows lie in
  [test_start, test_end) where ``test_end`` is the *maturity cutoff* (snapshot
  time minus ``maturity_days``). Windows after the maturity cutoff are scored
  separately as *provisional*.
* All models are scored on exactly the same (issue time, cell) test rows.
* Rolling-origin mode retrains at successive origins (expanding window).
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from ..build import CUBE_ORIGIN, cube_dir
from ..config import Config, load_config
from ..context import load_context
from ..dataset import Cube
from ..features import FeatureBuilder, FeatureConfig
from ..grid import Grid
from ..models.base import ColumnModel, CountModel
from .metrics import bootstrap_ci, calibration_bins, paired_bootstrap_delta, per_issue_stats, summarize

log = logging.getLogger(__name__)

DEEP = ("tcn", "convlstm", "stgnn", "transformer", "hawkes")

ABLATION_GROUPS = {
    "no_weather": ("wx_",),
    "no_events": ("ev_",),
    "no_transit": ("transit_",),
    "no_calls": ("calls_", "city_calls"),
    "no_fire": ("fire_", "city_fire"),
    "no_static": ("st_", "x_km", "y_km"),
    "no_other_cats": ("violent_", "all_located_", "property_"),
    "history_only": ("wx_", "ev_", "transit_", "calls_", "city_calls", "fire_", "city_fire", "st_", "x_km", "y_km"),
}


@dataclass
class Split:
    train_start: pd.Timestamp
    val_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp  # maturity cutoff (exclusive end of mature windows)
    data_end: pd.Timestamp  # end of available data (provisional windows end here)

    def as_dict(self) -> dict:
        return {k: v.isoformat() for k, v in asdict(self).items()}


@dataclass
class Env:
    cfg: Config
    grid: Grid
    active: np.ndarray
    cubes: dict[str, Cube]
    target: Cube
    category: str
    event_time: str
    ctx: object
    cell_attrs: pd.DataFrame
    data_versions: dict = field(default_factory=dict)

    @property
    def n_active(self) -> int:
        return int(self.active.sum())

    def builder(self, fcfg: FeatureConfig) -> FeatureBuilder:
        crime = {k.replace("_report", ""): v for k, v in self.cubes.items() if k.endswith("_report")}
        return FeatureBuilder(
            self.grid,
            self.active,
            self.target,
            crime,
            self.category,
            calls=self.cubes.get("calls"),
            fire=self.cubes.get("fire"),
            context=self.ctx,
            fcfg=fcfg,
        )


def load_env(cfg: Config, category: str, event_time: str = "report", with_context: bool = True) -> Env:
    d = cube_dir(cfg)
    grid = Grid.load(d / "grid.json")
    active = np.load(d / "active.npy")
    names = [f"{c}_report" for c in cfg["categories"]] + ["calls", "fire"]
    cubes = {n: Cube.load(d, n) for n in names if (d / f"{n}.npy").exists()}
    target = cubes[f"{category}_report"] if event_time == "report" else Cube.load(d, f"{category}_{event_time}")
    ctx = load_context(cfg, grid, CUBE_ORIGIN) if with_context else None
    attrs = pd.read_parquet(d / "cell_attrs.parquet")
    return Env(cfg, grid, active, cubes, target, category, event_time, ctx, attrs, data_versions=data_versions(cfg))


def data_versions(cfg: Config) -> dict:
    out = {}
    for src_dir in sorted(cfg.raw_dir.glob("*")):
        snaps = sorted(p for p in src_dir.iterdir() if (p / "manifest.json").exists())
        if snaps:
            out[src_dir.name] = snaps[-1].name
    return out


def default_split(cfg: Config, data_end: pd.Timestamp) -> Split:
    tr = cfg["training"]
    mat = pd.Timedelta(days=cfg["availability"]["maturity_days"])
    test_end = pd.Timestamp(tr["test_end"], tz="UTC") if tr.get("test_end") else (data_end - mat).floor("D")
    return Split(
        train_start=pd.Timestamp(tr["start"], tz="UTC") + pd.Timedelta(days=30),
        val_start=pd.Timestamp(tr["val_start"], tz="UTC"),
        test_start=pd.Timestamp(tr["test_start"], tz="UTC"),
        test_end=test_end,
        data_end=data_end.floor("h"),
    )


def issue_times(origin: pd.Timestamp, start: pd.Timestamp, end: pd.Timestamp, h: int, n: int | None = None, seed: int = 0) -> np.ndarray:
    """Hour indices t with start <= t and t + h <= end (optionally a random subset)."""
    a = int((start - origin) // pd.Timedelta(hours=1))
    b = int((end - origin) // pd.Timedelta(hours=1)) - h
    t = np.arange(a, b + 1, dtype=np.int64)
    if n is not None and n < len(t):
        t = np.sort(np.random.default_rng(seed).choice(t, size=n, replace=False))
    return t


def check_chronology(t_train: np.ndarray, t_val: np.ndarray, t_test: np.ndarray, h: int, lag: int) -> None:
    """Hard guarantees against temporal leakage between splits."""
    assert t_train.max() + h <= t_val.min(), "train windows overlap validation"
    assert t_val.max() + h <= t_test.min() - lag, "validation windows not available at test start"


def build_xy(fb: FeatureBuilder, t: np.ndarray, h: int, chunk: int = 400) -> tuple[pd.DataFrame, np.ndarray]:
    Xs, ys = [], []
    for i in range(0, len(t), chunk):
        X, y = fb.build(t[i : i + chunk], h)
        Xs.append(X)
        ys.append(y)
    return pd.concat(Xs, ignore_index=True), np.concatenate(ys)


def make_model(name: str, **kw) -> CountModel:
    if name == "seasonal":
        return ColumnModel(model_id="seasonal", family="baseline", column="base_seasonal")
    if name == "hotspot":
        return ColumnModel(model_id="hotspot", family="baseline", column="base_hotspot")
    if name == "poisson_glm":
        from ..models.glm import PoissonGLM

        return PoissonGLM(**kw)
    if name == "negbin_glm":
        from ..models.glm import NegBinGLM

        return NegBinGLM(**kw)
    if name == "lgbm":
        from ..models.gbm import LGBMModel

        return LGBMModel(**kw)
    if name == "catboost":
        from ..models.gbm import CatBoostModel

        return CatBoostModel(**kw)
    if name in ("tcn", "convlstm", "stgnn", "transformer", "hawkes"):
        from ..models.deep.wrapper import DeepGridModel

        return DeepGridModel(arch=name, **kw)
    raise ValueError(name)


def tune_baselines(env: Env, fcfg: FeatureConfig, t_val: np.ndarray, h: int) -> FeatureConfig:
    """Pick seasonal shrinkage kappa and hotspot bandwidth on validation deviance."""
    best = {}
    groups = ("crime",)
    for kappa in (26.0, 104.0, 416.0, 1664.0, 6656.0):
        fb = env.builder(FeatureConfig(**{**asdict(fcfg), "seasonal_kappa": kappa, "groups": groups}))
        X, y = build_xy(fb, t_val, h)
        st = per_issue_stats(X["_t"].to_numpy(), y, X["base_seasonal"].to_numpy(), 1 - np.exp(-X["base_seasonal"].to_numpy()), 0.0, env.n_active)
        best.setdefault("kappa", []).append((summarize(st, env.n_active)["poisson_deviance"], kappa))
    for sigma in (0.0, 0.5, 1.0, 1.5, 2.5):
        fb = env.builder(FeatureConfig(**{**asdict(fcfg), "hotspot_sigma": sigma, "groups": groups}))
        X, y = build_xy(fb, t_val, h)
        st = per_issue_stats(X["_t"].to_numpy(), y, X["base_hotspot"].to_numpy(), 1 - np.exp(-X["base_hotspot"].to_numpy()), 0.0, env.n_active)
        best.setdefault("sigma", []).append((summarize(st, env.n_active)["poisson_deviance"], sigma))
    k = min(best["kappa"])[1]
    s = min(best["sigma"])[1]
    log.info("baseline tuning h=%d: kappa=%s sigma=%s (%s)", h, k, s, best)
    return FeatureConfig(**{**asdict(fcfg), "seasonal_kappa": k, "hotspot_sigma": s})


@dataclass
class EvalAccumulator:
    """Collects per-issue stats, calibration samples and per-precinct sums."""

    n_cells: int
    precinct_of_cell: np.ndarray  # (n_active,) labels
    stats: list = field(default_factory=list)
    cal_p: list = field(default_factory=list)
    cal_y: list = field(default_factory=list)
    prec: dict = field(default_factory=dict)

    def add(self, t, y, mu, pany, alpha, ev_city=None, rng=None):
        st = per_issue_stats(t, y, mu, pany, alpha, self.n_cells)
        if ev_city is not None:
            st["ev_city"] = ev_city.reshape(-1, self.n_cells)[:, 0]
        self.stats.append(st)
        rng = rng or np.random.default_rng(0)
        take = rng.random(len(y)) < 0.2
        self.cal_p.append(pany[take].astype(np.float32))
        self.cal_y.append(y[take].astype(np.float32))
        Y = y.reshape(-1, self.n_cells)
        M = mu.reshape(-1, self.n_cells)
        for p in np.unique(self.precinct_of_cell):
            m = self.precinct_of_cell == p
            yy, mm = Y[:, m], M[:, m]
            k = max(1, int(round(0.1 * m.sum())))
            top = np.argsort(-mm, axis=1)[:, :k]
            cap = np.take_along_axis(yy, top, axis=1).sum()
            with np.errstate(divide="ignore", invalid="ignore"):
                dev = 2 * (np.where(yy > 0, yy * np.log(yy / np.maximum(mm, 1e-9)), 0) - (yy - mm))
            d = self.prec.setdefault(str(p), {"obs": 0.0, "pred": 0.0, "cap10": 0.0, "dev": 0.0, "n": 0})
            d["obs"] += float(yy.sum())
            d["pred"] += float(mm.sum())
            d["cap10"] += float(cap)
            d["dev"] += float(dev.sum())
            d["n"] += int(yy.size)

    def result(self, origin: pd.Timestamp, ci: bool = True) -> dict:
        st = pd.concat(self.stats, ignore_index=True)
        r = summarize(st, self.n_cells)
        if ci:
            r["ci90"] = bootstrap_ci(st, self.n_cells, origin)
        bins, ece = calibration_bins(np.concatenate(self.cal_p), np.concatenate(self.cal_y))
        r["ece"] = ece
        r["_bins"] = bins
        r["_precinct"] = {
            p: {"obs": d["obs"], "calib": d["pred"] / max(d["obs"], 1e-9), "capture_10_within": d["cap10"] / max(d["obs"], 1e-9), "deviance": d["dev"] / max(d["n"], 1)}
            for p, d in self.prec.items()
        }
        month = (origin + pd.to_timedelta(st["t"], unit="h")).dt.tz_convert("America/Los_Angeles").dt.strftime("%Y-%m")
        r["_monthly"] = [
            {"month": m, **{k: v for k, v in summarize(g, self.n_cells).items() if k in ("capture_10", "poisson_deviance", "observed_total", "calibration_in_large")}}
            for m, g in st.groupby(month.to_numpy())
        ]
        if "ev_city" in st:
            ev = st[st["ev_city"] > 0]
            if len(ev):
                r["_event_windows"] = {k: v for k, v in summarize(ev, self.n_cells).items() if k in ("capture_10", "poisson_deviance", "observed_total", "n_issue_times", "calibration_in_large")}
        r["_stats"] = st
        return r


def run_backtest(
    cfg: Config,
    category: str,
    horizons: list[int],
    models: list[str],
    out_root: Path,
    event_time: str = "report",
    n_train: int = 3000,
    n_val: int = 700,
    test_stride: int = 1,
    ablations: list[str] | None = None,
    lag_sensitivity: list[int] | None = None,
    save_models: bool = True,
    with_context: bool = True,
    deep_kw: dict | None = None,
    tag: str = "",
) -> dict:
    t0 = time.time()
    deep_kw = deep_kw or {}
    env = load_env(cfg, category, event_time, with_context=with_context)
    data_end = pd.Timestamp(env.target.meta.get("data_end", env.target.meta["end"]))
    split = default_split(cfg, data_end)
    lag = cfg["availability"]["crime_feature_lag_h"]
    base_fcfg = FeatureConfig(crime_lag_h=lag, calls_lag_h=cfg["availability"]["calls_feature_lag_h"], fire_lag_h=cfg["availability"]["fire911_feature_lag_h"])
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = out_root / f"{category}_{event_time}{'_' + tag if tag else ''}" / run_id
    out.mkdir(parents=True, exist_ok=True)
    prec_map = env.cell_attrs.set_index("cell")["precinct"]
    act_flat = np.flatnonzero(env.active.ravel())
    precinct_of_cell = prec_map.reindex(act_flat).fillna("UNKNOWN").astype(str).to_numpy()
    report: dict = {
        "run_id": run_id,
        "tag": tag,
        "models_requested": models,
        "n_train_issue_times": n_train,
        "test_stride_h": test_stride,
        "category": category,
        "event_time": event_time,
        "split": split.as_dict(),
        "n_active_cells": env.n_active,
        "crime_lag_h": lag,
        "data_versions": env.data_versions,
        "weather_mode": base_fcfg.weather_mode,
        "horizons": {},
    }
    origin = env.target.origin
    for h in horizons:
        th = time.time()
        t_train = issue_times(origin, split.train_start, split.val_start, h, n=n_train, seed=h)
        t_val = issue_times(origin, split.val_start, split.test_start - pd.Timedelta(hours=lag), h, n=n_val, seed=100 + h)
        t_test = issue_times(origin, split.test_start, split.test_end, h)[::test_stride]
        t_prov = issue_times(origin, split.test_end, split.data_end, h)[::test_stride]
        check_chronology(t_train, t_val, t_test, h, lag)
        fcfg = tune_baselines(env, base_fcfg, t_val[:300], h)
        fb = env.builder(fcfg)
        X_tr, y_tr = build_xy(fb, t_train, h)
        X_va, y_va = build_xy(fb, t_val, h)
        log.info("h=%d train rows %d (pos %.4f) val rows %d; features %d", h, len(y_tr), (y_tr > 0).mean(), len(y_va), X_tr.shape[1])
        fitted: dict[str, CountModel] = {}
        hres: dict = {"n_train_rows": int(len(y_tr)), "n_val_rows": int(len(y_va)), "n_test_issue_times": int(len(t_test)),
                      "n_provisional_issue_times": int(len(t_prov)), "baseline_params": {"kappa": fcfg.seasonal_kappa, "sigma": fcfg.hotspot_sigma},
                      "models": {}, "provisional": {}, "ablations": {}}
        for m in models:
            tm = time.time()
            model = make_model(m, **(deep_kw if m in DEEP else {}))
            if hasattr(model, "attach"):
                model.attach(fb, h)
            model.fit(X_tr, y_tr, X_va, y_va)
            fitted[m] = model
            log.info("h=%d fitted %s in %.0fs (alpha=%.3f)", h, m, time.time() - tm, model.alpha)
            if save_models:
                model.train_info = {"category": category, "event_time": event_time, "horizon_h": h, "split": split.as_dict(),
                                    "n_train_rows": int(len(y_tr)), "data_versions": env.data_versions, "feature_config": asdict(fcfg)}
                model.save(out / "models" / f"h{h}" / m)
        abl_models: dict[str, CountModel] = {}
        if ablations and "lgbm" in fitted:
            from ..models.gbm import LGBMModel

            for a in ablations:
                pref = ABLATION_GROUPS[a]
                excl = tuple(c for c in fitted["lgbm"].features if c.startswith(pref))
                if not excl:
                    continue
                mm = LGBMModel(model_id=f"lgbm_{a}", exclude=excl)
                mm.fit(X_tr, y_tr, X_va, y_va)
                abl_models[a] = mm
        del X_tr, y_tr
        # ---------------- test (mature) and provisional scoring on identical rows
        for label, tt in (("mature", t_test), ("provisional", t_prov)):
            if len(tt) == 0:
                continue
            accs = {m: EvalAccumulator(env.n_active, precinct_of_cell) for m in list(fitted) + [f"abl:{a}" for a in abl_models]}
            for i in range(0, len(tt), 300):
                X, y = fb.build(tt[i : i + 300], h)
                evc = X["ev_major_city"].to_numpy() if "ev_major_city" in X else None
                for m, model in list(fitted.items()) + [(f"abl:{a}", mm) for a, mm in abl_models.items()]:
                    d = model.predict_dist(X)
                    accs[m].add(X["_t"].to_numpy(), y, d["expected"].to_numpy(), d["p_any"].to_numpy(), model.alpha, evc)
            results = {m: acc.result(origin, ci=(label == "mature")) for m, acc in accs.items()}
            stats_dir = out / "stats" / f"h{h}" / label
            stats_dir.mkdir(parents=True, exist_ok=True)
            for m, r in results.items():
                r.pop("_stats").to_parquet(stats_dir / f"{m.replace(':', '_')}.parquet", index=False)
            if label == "mature":
                for m in fitted:
                    hres["models"][m] = {**results[m], "alpha": fitted[m].alpha, "version": fitted[m].version}
                # paired comparisons vs baselines on identical windows
                st = {m: pd.read_parquet(stats_dir / f"{m}.parquet") for m in fitted}
                for m in fitted:
                    for b in ("seasonal", "hotspot"):
                        if b in st and m != b:
                            hres["models"][m][f"delta_capture_10_vs_{b}"] = paired_bootstrap_delta(st[m], st[b], env.n_active, origin)
                            hres["models"][m][f"lift_capture_10_vs_{b}"] = results[m]["capture_10"] / max(results[b]["capture_10"], 1e-9)
                for a in abl_models:
                    ra = results[f"abl:{a}"]
                    full = results["lgbm"]
                    sa = pd.read_parquet(stats_dir / f"abl_{a}.parquet")
                    hres["ablations"][a] = {
                        "capture_10": ra["capture_10"],
                        "poisson_deviance": ra["poisson_deviance"],
                        "brier": ra["brier"],
                        "delta_capture_10_full_minus_ablated": paired_bootstrap_delta(st["lgbm"], sa, env.n_active, origin),
                        "delta_deviance_full_minus_ablated": full["poisson_deviance"] - ra["poisson_deviance"],
                        "event_windows": ra.get("_event_windows"),
                    }
            else:
                hres["provisional"] = {m: {k: v for k, v in r.items() if not k.startswith("_")} for m, r in results.items() if not m.startswith("abl:")}
        log.info("h=%d done in %.0fs", h, time.time() - th)
        report["horizons"][str(h)] = hres
        (out / "report.json").write_text(json.dumps(report, indent=2, default=_json_default))

    if lag_sensitivity:
        report["lag_sensitivity"] = run_lag_sensitivity(env, split, base_fcfg, lag_sensitivity, n_train, n_val)
    report["elapsed_s"] = time.time() - t0
    (out / "report.json").write_text(json.dumps(report, indent=2, default=_json_default))
    latest = out_root / f"{category}_{event_time}{'_' + tag if tag else ''}" / "LATEST"
    latest.write_text(run_id)
    return report


def run_lag_sensitivity(env: Env, split: Split, base: FeatureConfig, lags: list[int], n_train: int, n_val: int, h: int = 24, stride: int = 6) -> dict:
    """How much would fresher crime data help? Re-train LightGBM with alternative
    crime availability lags (0 = hypothetical instant publication)."""
    from ..models.gbm import LGBMModel

    origin = env.target.origin
    res = {}
    for L in lags:
        fcfg = FeatureConfig(**{**asdict(base), "crime_lag_h": L})
        fb = env.builder(fcfg)
        t_train = issue_times(origin, split.train_start, split.val_start, h, n=n_train, seed=h)
        t_val = issue_times(origin, split.val_start, split.test_start - pd.Timedelta(hours=max(L, 1)), h, n=n_val, seed=100 + h)
        t_test = issue_times(origin, split.test_start, split.test_end, h)[::stride]
        X_tr, y_tr = build_xy(fb, t_train, h)
        X_va, y_va = build_xy(fb, t_val, h)
        m = LGBMModel(model_id=f"lgbm_lag{L}")
        m.fit(X_tr, y_tr, X_va, y_va)
        acc = EvalAccumulator(env.n_active, np.zeros(env.n_active))
        accs = EvalAccumulator(env.n_active, np.zeros(env.n_active))
        for i in range(0, len(t_test), 300):
            X, y = fb.build(t_test[i : i + 300], h)
            d = m.predict_dist(X)
            acc.add(X["_t"].to_numpy(), y, d["expected"].to_numpy(), d["p_any"].to_numpy(), m.alpha)
            s = X["base_seasonal"].to_numpy()
            accs.add(X["_t"].to_numpy(), y, s, 1 - np.exp(-s), 0.0)
        r = acc.result(origin, ci=False)
        rs = accs.result(origin, ci=False)
        res[str(L)] = {"horizon_h": h, "test_stride_h": stride, "lgbm_capture_10": r["capture_10"], "lgbm_deviance": r["poisson_deviance"],
                       "seasonal_capture_10": rs["capture_10"], "seasonal_deviance": rs["poisson_deviance"]}
        log.info("lag %d: %s", L, res[str(L)])
    return res


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, pd.DataFrame):
        return None
    return str(o)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="property")
    ap.add_argument("--event-time", default="report")
    ap.add_argument("--horizons", type=int, nargs="+", default=[1, 3, 6, 24])
    ap.add_argument("--models", nargs="+", default=["seasonal", "hotspot", "poisson_glm", "negbin_glm", "lgbm", "catboost"])
    ap.add_argument("--ablations", nargs="*", default=[])
    ap.add_argument("--lag-sensitivity", type=int, nargs="*", default=[])
    ap.add_argument("--n-train", type=int, default=3000)
    ap.add_argument("--n-val", type=int, default=700)
    ap.add_argument("--test-stride", type=int, default=1)
    ap.add_argument("--no-context", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--deep-epochs", type=int, default=12)
    ap.add_argument("--deep-samples", type=int, default=768)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config()
    out_root = Path(args.out) if args.out else cfg.artifacts_root / "backtests"
    rep = run_backtest(cfg, args.category, args.horizons, args.models, out_root, args.event_time, args.n_train, args.n_val,
                       args.test_stride, args.ablations, args.lag_sensitivity, with_context=not args.no_context,
                       deep_kw={"epochs": args.deep_epochs, "samples_per_epoch": args.deep_samples}, tag=args.tag)
    for h, hr in rep["horizons"].items():
        for m, r in hr["models"].items():
            print(f"h={h:>2} {m:<12} cap5={r['capture_5']:.3f} cap10={r['capture_10']:.3f} cap20={r['capture_20']:.3f} dev={r['poisson_deviance']:.4f} nll={r['nll']:.4f} brier={r['brier']:.5f} ece={r['ece']:.5f} cal={r['calibration_in_large']:.3f}")


if __name__ == "__main__":
    main()
