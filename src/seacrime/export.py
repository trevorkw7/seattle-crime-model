"""Export dashboard documents (docs/API_CONTRACT.md) to ``web/public/data``.

    python -m seacrime.export [--out web/public/data]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .build import cube_dir
from .config import REPO_ROOT, Config, load_config
from .dataset import Cube
from .grid import Grid
from .prospective.forecast import prospective_root
from .prospective.store import ForecastStore
from .train import production_dir

log = logging.getLogger(__name__)
TZ = "America/Los_Angeles"

CATEGORY_LABELS = {"property": "Property crime", "violent": "Violent crime (located)", "all_located": "All located offenses"}
MODEL_LABELS = {
    "lgbm": "LightGBM",
    "lgbm_resid": "LightGBM (seasonal offset)",
    "catboost": "CatBoost",
    "seasonal": "Seasonal (hour-of-week)",
    "hotspot": "Hotspot (KDE)",
    "poisson_glm": "Poisson GLM",
    "negbin_glm": "Neg. binomial GLM",
    "tcn": "TCN",
    "convlstm": "ConvLSTM",
    "stgnn": "ST-GNN",
    "transformer": "Event transformer",
    "hawkes": "Neural Hawkes",
}
DISCLAIMERS = [
    "Forecasts are area-level (500 m grid cells) expectations of incidents reported to the Seattle Police Department; they say nothing about any individual.",
    "Reported crime reflects both crime and reporting/recording practices (e.g. online reporting, batch approvals, patrol presence). Some areas and offense types are systematically under- or over-recorded.",
    "SPD blurs locations to the hundred block and redacts sensitive offenses (about 27% of violent offenses have no usable location), so maps understate those categories.",
    "Predictive associations (events, weather, transit, neighbourhood attributes) are not causal and must not be used for enforcement targeting.",
    "Recent windows are provisional: SPD publishes finalized reports daily with multi-day delays; scores become final after 30 days.",
]


def _iso(ts) -> str | None:
    if ts is None or (isinstance(ts, float) and np.isnan(ts)):
        return None
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _prov(kind: str, note: str = "") -> dict:
    return {"kind": kind, "generated_at": _iso(pd.Timestamp.now("UTC")), "note": note}


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), default=_json_default))


def _json_default(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (pd.Timestamp,)):
        return _iso(o)
    return str(o)


def _r(x, n=4):
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else round(float(x), n)


class Exporter:
    def __init__(self, cfg: Config, out: Path):
        self.cfg = cfg
        self.out = out
        d = cube_dir(cfg)
        self.grid = Grid.load(d / "grid.json")
        self.active = np.load(d / "active.npy")
        self.act_flat = np.flatnonzero(self.active.ravel())
        self.attrs = pd.read_parquet(d / "cell_attrs.parquet").set_index("cell")
        self.store = ForecastStore(prospective_root(cfg))
        self.cell_id = {int(f): self.grid.cell_id(f) for f in self.act_flat}

    # ----------------------------------------------------------------- meta
    def meta(self, backtests: dict) -> dict:
        reg_p = production_dir(self.cfg) / "registry.json"
        reg = json.loads(reg_p.read_text()) if reg_p.exists() else {}
        models = {}
        for cat, hs in reg.items():
            if cat.startswith("_"):
                continue
            for ms in hs.values():
                for m, info in ms.items():
                    models.setdefault(m, {"id": m, "label": MODEL_LABELS.get(m, m), "version": info["version"], "family": _family(m), "trained_at": info["trained_at"]})
        for rep in backtests.values():
            for hr in rep.get("horizons", {}).values():
                for m, r in hr.get("models", {}).items():
                    models.setdefault(m, {"id": m, "label": MODEL_LABELS.get(m, m), "version": r.get("version"), "family": _family(m), "trained_at": None, "backtest_only": True})
        default = "lgbm" if "lgbm" in models else next(iter(models), None)
        for m in models.values():
            m["is_default"] = m["id"] == default
        lon0, lat0, lon1, lat1 = self.cfg["grid"]["bbox"]
        return {
            "provenance": _prov("actual", "Model registry and configuration"),
            "categories": [{"id": c, "label": CATEGORY_LABELS.get(c, c)} for c in self.cfg["categories"]],
            "horizons_h": self.cfg.horizons,
            "models": sorted(models.values(), key=lambda m: (not m["is_default"], m["family"] != "gbm", m["id"])),
            "grid": {"cell_size_m": self.grid.cell_size_m, "n_cells": int(len(self.act_flat)), "crs": self.grid.crs, "bbox": [lon0, lat0, lon1, lat1]},
            "event_time": self.cfg["forecast"]["event_time"],
            "disclaimers": DISCLAIMERS,
        }

    def grid_geojson(self) -> dict:
        props = {}
        for f in self.act_flat:
            a = self.attrs.loc[f] if f in self.attrs.index else None
            props[int(f)] = {
                "neighborhood": None if a is None or pd.isna(a["neighborhood"]) or a["neighborhood"] == "-" else str(a["neighborhood"]),
                "precinct": None if a is None or pd.isna(a["precinct"]) else str(a["precinct"]),
                "in_city": True,
            }
        return self.grid.to_geojson(self.active.ravel(), props)

    # ------------------------------------------------------------ forecasts
    def forecasts(self, max_live: int = 400, max_replay: int = 400) -> dict:
        fc = self.store.list_forecasts()
        scores = self.store.latest_scores()
        items, latest = [], {}
        if fc.empty:
            return {"provenance": _prov("actual", "No forecasts frozen yet"), "latest": {}, "items": []}
        fc = fc.sort_values("t0")
        keep = pd.concat([fc[fc["provenance"] == "actual"].tail(max_live), fc[fc["provenance"] != "actual"].tail(max_replay)])
        # drop documents of forecasts no longer exported so a snapshot holds only what the index lists
        fdir = self.out / "forecasts"
        if fdir.exists():
            for f in fdir.glob("*.json"):
                if f.name != "index.json" and f.stem not in set(keep["forecast_id"]):
                    f.unlink()
        data_end = None
        try:
            data_end = pd.Timestamp(Cube.load(cube_dir(self.cfg), "property_report", mmap=True).meta.get("data_end"))
        except Exception:
            pass
        for _, f in keep.iterrows():
            fid = f["forecast_id"]
            preds = self.store.load_predictions(fid)
            sc = scores[scores["forecast_id"] == fid] if not scores.empty else pd.DataFrame()
            hz = {}
            for h in json.loads(f["horizons"]):
                p = preds[preds["horizon_h"] == h]
                ws = pd.Timestamp(f["t0"])
                hz[str(h)] = {
                    "window_start": _iso(ws),
                    "window_end": _iso(ws + pd.Timedelta(hours=h)),
                    "total_expected": _r(p["expected"].sum(), 3),
                    "cells": {self.cell_id[int(c)]: [_r(e, 4), _r(pa, 4), _r(rr, 3), _r(lo, 0), _r(hi, 0)]
                              for c, e, pa, rr, lo, hi in zip(p["cell"], p["expected"], p["p_any"], p["relative_risk"], p["lo90"], p["hi90"], strict=True)
                              if int(c) in self.cell_id},
                }
            score = None
            status = "frozen"
            if not sc.empty:
                mat = "mature" if (sc["maturity"] == "mature").all() else "provisional"
                status = f"scored_{mat}"
                score = {"maturity": mat, "scored_at": _iso(sc["scored_at"].max()), "per_horizon": {}, "observed": {}}
                for _, s in sc.iterrows():
                    m = s["metrics"]
                    score["per_horizon"][str(s["horizon_h"])] = {
                        "maturity": s["maturity"],
                        "observed_total": m["observed_total"],
                        "predicted_total": _r(m["predicted_total"], 2),
                        "capture_5": _r(m["capture_5"]), "capture_10": _r(m["capture_10"]), "capture_20": _r(m["capture_20"]),
                        "poisson_deviance": _r(m["poisson_deviance"], 5), "brier": _r(m["brier"], 5),
                        "window_age_days_at_scoring": _r(m.get("window_age_days_at_scoring"), 2),
                    }
                    score["observed"][str(s["horizon_h"])] = {self.cell_id[int(c)]: v for c, v in m.get("observed_cells", {}).items() if int(c) in self.cell_id}
            elif f["provenance"] == "actual" and data_end is not None and pd.Timestamp(f["t0"]) >= data_end:
                status = "awaiting_data"
            kind = f["provenance"]
            doc = {
                "provenance": _prov(kind, "Historical replay: forecast reconstructed as of a past time using only data available then, with models trained before the test period." if kind == "replay" else "Live forecast frozen before its windows opened."),
                "forecast_id": fid,
                "issued_at": _iso(f["issued_at"]),
                "model_id": f["model_id"],
                "model_version": f["model_version"],
                "category": f["category"],
                "data_versions": json.loads(f["data_versions"]),
                "feature_cutoffs": json.loads(f["feature_cutoffs"]),
                "columns": ["expected", "p_any", "relative_risk", "lo90", "hi90"],
                "horizons": hz,
                "score": score,
                "sha256": f["sha256"],
            }
            _write(self.out / "forecasts" / f"{fid}.json", doc)
            items.append({"forecast_id": fid, "issued_at": _iso(f["issued_at"]), "t0": _iso(f["t0"]), "model_id": f["model_id"], "category": f["category"],
                          "status": status, "provenance_kind": kind})
            if kind == "actual":
                latest.setdefault(f["model_id"], {})[f["category"]] = fid
        # fall back to latest replay forecast when no live forecast exists for a model/category
        for it in items:
            if it["provenance_kind"] == "replay" and it["category"] not in latest.get(it["model_id"], {}):
                latest.setdefault(it["model_id"], {})
        rp_latest = {}
        for it in items:
            if it["provenance_kind"] == "replay":
                rp_latest.setdefault(it["model_id"], {})[it["category"]] = it["forecast_id"]
        for m, cats in rp_latest.items():
            for c, fid in cats.items():
                latest.setdefault(m, {}).setdefault(c, fid)
        return {"provenance": _prov("actual", "Index of frozen forecasts (live and historical replay)"), "latest": latest, "items": items}

    # -------------------------------------------------------------- history
    def history(self, min_count: int = 3) -> None:
        d = cube_dir(self.cfg)
        for cat in self.cfg["categories"]:
            cube = Cube.load(d, f"{cat}_report", mmap=True)
            end = pd.Timestamp(cube.meta.get("data_end", cube.meta["end"]))
            e = cube.hour_of(end)
            periods = {}
            for name, days in (("last_7d", 7), ("last_30d", 30), ("last_365d", 365)):
                s = max(e - 24 * days, 0)
                tot = np.asarray(cube.counts[s:e]).sum(0).ravel()
                periods[name] = {
                    "start": _iso(cube.origin + pd.Timedelta(hours=s)),
                    "end": _iso(end),
                    "total": int(tot[self.act_flat].sum()),
                    # every active cell is listed; counts below min_count (including zero) are
                    # all reported as null so suppressed cells cannot be told apart
                    "cells": {self.cell_id[int(f)]: (int(tot[f]) if tot[f] >= min_count else None) for f in self.act_flat},
                }
            # citywide hour-of-week profile over the last 365 days (local time)
            s = max(e - 24 * 365, 0)
            series = np.asarray(cube.counts[s:e]).reshape(e - s, -1)[:, self.act_flat].sum(1)
            ts = pd.date_range(cube.origin + pd.Timedelta(hours=s), periods=e - s, freq="h", tz="UTC").tz_convert(TZ)
            how = ts.dayofweek * 24 + ts.hour
            prof = pd.Series(series).groupby(np.asarray(how)).mean().reindex(range(168), fill_value=0.0)
            doc = {
                "provenance": _prov("actual", "Aggregated counts of SPD-reported incidents per 500 m cell by report time; cells with fewer than min_count incidents are suppressed (null)."),
                "category": cat,
                "min_count": min_count,
                "data_end": _iso(end),
                "periods": periods,
                "hour_of_week_citywide": [round(float(x), 4) for x in prof.to_numpy()],
                "note": "Recent days are incomplete because SPD publishes finalized reports with a multi-day delay.",
            }
            _write(self.out / "history" / f"{cat}.json", doc)

    # ----------------------------------------------------------- evaluation
    def evaluation(self, backtests: dict) -> dict:
        rows, cal, abl, prec, monthly, lag = [], [], [], [], [], []
        test_window = None
        deep_rows = []
        for rep in backtests.values():
            cat = rep["category"]
            et = rep["event_time"]
            sp = rep["split"]
            tag = rep.get("tag", "")
            if tag.startswith("fold") or tag == "deep_smoke":
                continue  # rolling folds are summarised separately; smoke runs are not results
            if not tag:
                test_window = test_window or {"start": sp["test_start"], "end": sp["test_end"]}
            for h, hr in rep["horizons"].items():
                for m, r in hr["models"].items():
                    if tag.startswith("deep"):
                        deep_rows.append({"model_id": m, "category": cat, "event_time": et, "horizon_h": int(h), "test_stride_h": rep.get("test_stride_h"),
                                          "n_train_issue_times": rep.get("n_train_issue_times"), "capture_10": _r(r["capture_10"]),
                                          "capture_10_ci": [_r(x) for x in r.get("ci90", {}).get("capture_10", [None, None])],
                                          "poisson_deviance": _r(r["poisson_deviance"], 5), "brier": _r(r["brier"], 6),
                                          "calibration_in_large": _r(r["calibration_in_large"], 4),
                                          "delta_capture_10_vs_seasonal": r.get("delta_capture_10_vs_seasonal"),
                                          "params": {k: v for k, v in (r.get("params") or {}).items()}})
                        continue
                    if tag == "ablation" or (tag and m in ("seasonal", "hotspot")):
                        continue  # ablation-run reference models / re-scored baselines duplicate the main run
                    rows.append({
                        "model_id": m, "category": cat, "event_time": et, "horizon_h": int(h),
                        "capture_5": _r(r["capture_5"]), "capture_10": _r(r["capture_10"]), "capture_20": _r(r["capture_20"]),
                        "pai_10": _r(r["pai_10"], 3), "hit_top10": _r(r.get("hit_top10")), "hit_top25": _r(r.get("hit_top25")), "hit_top50": _r(r.get("hit_top50")),
                        "lift_vs_seasonal_10": _r(r.get("lift_capture_10_vs_seasonal", 1.0 if m == "seasonal" else None), 4),
                        "lift_vs_hotspot_10": _r(r.get("lift_capture_10_vs_hotspot", 1.0 if m == "hotspot" else None), 4),
                        "delta_capture_10_vs_seasonal": r.get("delta_capture_10_vs_seasonal"),
                        "poisson_deviance": _r(r["poisson_deviance"], 5), "nll": _r(r["nll"], 5), "mae": _r(r["mae"], 5), "rmse": _r(r["rmse"], 5),
                        "brier": _r(r["brier"], 6), "ece": _r(r["ece"], 6), "calibration_in_large": _r(r["calibration_in_large"], 4),
                        "observed_total": r["observed_total"], "n_issue_times": r["n_issue_times"], "alpha": _r(r.get("alpha"), 4),
                        "ci": {k: [_r(a), _r(b)] for k, (a, b) in r.get("ci90", {}).items()},
                        "ci_level": 0.9,
                        "event_windows": r.get("_event_windows"),
                    })
                    cal.append({"model_id": m, "category": cat, "event_time": et, "horizon_h": int(h), "bins": r.get("_bins", [])})
                    if m in ("lgbm", "seasonal", "hotspot"):
                        prec.append({"model_id": m, "category": cat, "event_time": et, "horizon_h": int(h),
                                     "by_precinct": [{"precinct": p, "capture_10": _r(d["capture_10_within"], 4), **{k: _r(v, 4) for k, v in d.items()}}
                                                     for p, d in r.get("_precinct", {}).items() if d.get("obs", 0) >= 50]})
                        mon = r.get("_monthly", [])
                        med = float(np.median([x["observed_total"] for x in mon])) if mon else 0.0
                        # drop partial edge months (UTC/local boundary) with < 25 % of a typical month's incidents
                        monthly.append({"model_id": m, "category": cat, "event_time": et, "horizon_h": int(h),
                                        "by_month": [x for x in mon if x["observed_total"] >= 0.25 * med]})
                for a, ar in hr.get("ablations", {}).items():
                    abl.append({"name": a, "model_id": "lgbm", "category": cat, "event_time": et, "horizon_h": int(h),
                                "capture_10": _r(ar["capture_10"]), "poisson_deviance": _r(ar["poisson_deviance"], 5), "brier": _r(ar["brier"], 6),
                                # sign: ablated minus full (negative = removing the group hurts capture)
                                "delta_vs_full": _r(-ar["delta_capture_10_full_minus_ablated"]["delta"], 5),
                                "delta_ci90": sorted(_r(-x, 5) for x in ar["delta_capture_10_full_minus_ablated"]["ci90"]),
                                "delta_deviance_full_minus_ablated": _r(ar["delta_deviance_full_minus_ablated"], 6)})
                for m, r in hr.get("provisional", {}).items():
                    rows.append({"model_id": m, "category": cat, "event_time": et, "horizon_h": int(h), "maturity": "provisional", "section": "backtest_provisional",
                                 "capture_10": _r(r["capture_10"]), "poisson_deviance": _r(r["poisson_deviance"], 5), "brier": _r(r["brier"], 6),
                                 "observed_total": r["observed_total"], "n_issue_times": r["n_issue_times"]})
            if rep.get("lag_sensitivity"):
                lag.append({"category": cat, "event_time": et, "results": rep["lag_sensitivity"]})
        rolling = []
        for d in sorted((self.cfg.artifacts_root / "backtests").glob("*_rolling")):
            rp = d / "report.json"
            if rp.exists():
                rr = json.loads(rp.read_text())
                rolling.append({"category": rr["category"], "event_time": rr["event_time"], "origins": rr["origins"], "fold_days": rr["fold_days"],
                                "pooled": [{"horizon_h": int(h), "model_id": m, "capture_10": _r(r["capture_10"]),
                                            "capture_10_ci": [_r(x) for x in r.get("ci90", {}).get("capture_10", [None, None])],
                                            "poisson_deviance": _r(r["poisson_deviance"], 5), "calibration_in_large": _r(r["calibration_in_large"], 4),
                                            "delta_capture_10_vs_seasonal": r.get("delta_capture_10_vs_seasonal")}
                                           for h, pr in rr["pooled"].items() for m, r in pr.items()],
                                "folds": rr["folds"]})
        backtest_rows = [r for r in rows if r.get("section") != "backtest_provisional"]
        prov_rows = [r for r in rows if r.get("section") == "backtest_provisional"]
        prospective = self._prospective_rows()
        return {
            "provenance": _prov("actual", "Backtest metrics computed on identical held-out test windows; prospective metrics from frozen forecasts."),
            "backtest": {"test_window": test_window, "rows": backtest_rows, "provisional_rows": prov_rows},
            "prospective": prospective,
            "ablations": abl,
            "calibration": cal,
            "stability": {"by_precinct": prec, "by_month": monthly},
            "lag_sensitivity": lag,
            "rolling": rolling,
            "deep_comparison": deep_rows,
        }

    def _prospective_rows(self) -> dict:
        s = self.store.latest_scores()
        fc = self.store.list_forecasts()
        if s.empty or fc.empty:
            return {"rows": [], "timeseries": []}
        s = s.merge(fc[["forecast_id", "model_id", "category", "issued_at", "t0", "provenance"]], on="forecast_id")
        ts, rows = [], []
        for _, r in s.iterrows():
            m = r["metrics"]
            ts.append({"issued_at": _iso(r["issued_at"]), "window_start": _iso(r["t0"]), "model_id": r["model_id"], "category": r["category"], "horizon_h": int(r["horizon_h"]),
                       "capture_10": _r(m["capture_10"]), "poisson_deviance": _r(m["poisson_deviance"], 5), "observed_total": m["observed_total"],
                       "maturity": r["maturity"], "source": "live" if r["provenance"] == "actual" else "replay"})
        df = pd.DataFrame(ts)
        for (mid, cat, h, mat, src), g in df.groupby(["model_id", "category", "horizon_h", "maturity", "source"]):
            sub = s[(s["model_id"] == mid) & (s["category"] == cat) & (s["horizon_h"] == h) & (s["maturity"] == mat)]
            obs = sum(x["observed_total"] for x in sub["metrics"])
            cap = {k: sum(x[f"capture_{k}"] * x["observed_total"] for x in sub["metrics"]) / max(obs, 1e-9) for k in (5, 10, 20)}
            rows.append({"model_id": mid, "category": cat, "horizon_h": int(h), "maturity": mat, "source": src, "n_forecasts": int(len(g)),
                         "observed_total": obs, "capture_5": _r(cap[5]), "capture_10": _r(cap[10]), "capture_20": _r(cap[20]),
                         "poisson_deviance": _r(float(np.mean([x["poisson_deviance"] for x in sub["metrics"]])), 5),
                         "brier": _r(float(np.mean([x["brier"] for x in sub["metrics"]])), 6)})
        return {"rows": rows, "timeseries": sorted(ts, key=lambda x: x["issued_at"])}

    # --------------------------------------------------------------- health
    def health(self) -> dict:
        from .ingest.socrata import latest_snapshot

        now = pd.Timestamp.now("UTC")
        sources = []
        stale_h = {"spd_crime": 72, "spd_calls": 168, "fire_911": 3, "weather": 48, "events": 24 * 14, "gtfs": 24 * 30, "boundaries": 24 * 365, "news": 48}
        for src_dir in sorted(self.cfg.raw_dir.glob("*")):
            snap = latest_snapshot(self.cfg.raw_dir, src_dir.name)
            if snap is None:
                sources.append({"name": src_dir.name, "status": "missing"})
                continue
            man = json.loads((snap / "manifest.json").read_text())
            fetched = pd.Timestamp(man.get("fetched_at_utc") or man.get("completed_at_utc"))
            latest_ev = None
            proc = {"spd_crime": ("spd_offenses.parquet", "report_ts_utc"), "spd_calls": ("spd_calls.parquet", "ts_utc"), "fire_911": ("fire_911.parquet", "ts_utc"),
                    "weather": ("weather_hourly.parquet", "ts_utc")}.get(src_dir.name)
            if proc and (self.cfg.processed_dir / proc[0]).exists():
                latest_ev = pd.read_parquet(self.cfg.processed_dir / proc[0], columns=[proc[1]])[proc[1]].max()
            lag = (now - pd.Timestamp(latest_ev)) / pd.Timedelta(hours=1) if latest_ev is not None else None
            age = (now - fetched) / pd.Timedelta(hours=1)
            status = "ok" if age <= stale_h.get(src_dir.name, 72) else "stale"
            sources.append({"name": src_dir.name, "snapshot_id": snap.name, "fetched_at": _iso(fetched),
                            "publisher_updated_at": (man.get("dataset_metadata") or {}).get("rowsUpdatedAt_iso"),
                            "latest_event_ts": _iso(latest_ev) if latest_ev is not None else None, "lag_hours": _r(lag, 1),
                            "snapshot_age_hours": _r(age, 1), "status": status})
        reg_p = production_dir(self.cfg) / "registry.json"
        reg = json.loads(reg_p.read_text()) if reg_p.exists() else {}
        fc = self.store.list_forecasts()
        models = []
        seen = set()
        for cat, hs in reg.items():
            if cat.startswith("_"):
                continue
            for ms in hs.values():
                for m, info in ms.items():
                    if m in seen:
                        continue
                    seen.add(m)
                    live = fc[(fc["model_id"] == m) & (fc["provenance"] == "actual")] if not fc.empty else fc
                    last = live["issued_at"].max() if len(live) else None
                    st = "ok" if last is not None and (now - pd.Timestamp(last)) < pd.Timedelta(hours=3) else ("degraded" if last is not None else "missing")
                    models.append({"model_id": m, "version": info["version"], "trained_at": info["trained_at"], "last_forecast_at": _iso(last) if last else None,
                                   "status": st, "notes": "no live forecast in the last 3 h" if st != "ok" else ""})
        jobs = self.store.jobs()
        jl = []
        if not jobs.empty:
            for name, g in jobs.groupby("job"):
                last = g.sort_values("started_at").iloc[-1]
                jl.append({"name": name, "last_run": _iso(last["started_at"]), "last_status": "ok" if last["status"] == "ok" else last["status"],
                           "message": public_message(last["message"])})
        return {"provenance": _prov("actual", "Data freshness and model health"), "sources": sources, "models": models, "jobs": jl}


_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:/[\w.@~+-]+){2,}/?")


def public_message(msg: str | None, limit: int = 300) -> str:
    """Job messages are published in health.json: strip filesystem paths (and any
    traceback detail they carry) and cap the length."""
    if not msg:
        return ""
    return _PATH_RE.sub("<path>", str(msg))[:limit]


def _family(m: str) -> str:
    if m in ("seasonal", "hotspot"):
        return "baseline"
    if m in ("poisson_glm", "negbin_glm"):
        return "glm"
    if m in ("lgbm", "lgbm_resid", "catboost"):
        return "gbm"
    return "deep"


def latest_backtests(cfg: Config) -> dict:
    out = {}
    root = cfg.artifacts_root / "backtests"
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        latest = d / "LATEST"
        if latest.exists():
            p = d / latest.read_text().strip() / "report.json"
            if p.exists():
                out[d.name] = json.loads(p.read_text())
    return out


def export_all(cfg: Config, out: Path | None = None, max_live: int = 400, max_replay: int = 400) -> Path:
    """Write every dashboard document. ``max_live`` / ``max_replay`` keep only the
    most recent forecasts of each kind (used for the committed site snapshot)."""
    out = out or Path(os.environ.get("SEACRIME_EXPORT_DIR", REPO_ROOT / "web" / "public" / "data"))
    out.mkdir(parents=True, exist_ok=True)
    ex = Exporter(cfg, out)
    bts = latest_backtests(cfg)
    _write(out / "meta.json", ex.meta(bts))
    _write(out / "grid.geojson", ex.grid_geojson())
    _write(out / "forecasts" / "index.json", ex.forecasts(max_live=max_live, max_replay=max_replay))
    ex.history()
    _write(out / "evaluation" / "summary.json", ex.evaluation(bts))
    _write(out / "health.json", ex.health())
    log.info("exported dashboard data to %s", out)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--max-live", type=int, default=400, help="most recent live forecasts to export")
    ap.add_argument("--max-replay", type=int, default=400, help="most recent historical-replay forecasts to export")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    export_all(load_config(), Path(args.out) if args.out else None, max_live=args.max_live, max_replay=args.max_replay)


if __name__ == "__main__":
    main()
