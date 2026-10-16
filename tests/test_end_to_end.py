"""End-to-end prospective lifecycle on synthetic data (no network):

snapshot -> reconcile -> cubes -> production training -> freeze forecasts ->
new snapshot with incidents in the window -> provisional score -> export.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from seacrime.config import load_config
from seacrime.incidents import clean_spd, reconcile

BBOX = [-122.36, 47.59, -122.31, 47.63]


def _raw(n: int, start: str, end: str, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = pd.to_datetime(rng.uniform(pd.Timestamp(start).value, pd.Timestamp(end).value, n).astype("int64"))
    hot = rng.random(n) < 0.6  # a persistent hotspot near the centre
    lat = np.where(hot, 47.61 + rng.normal(0, 0.002, n), rng.uniform(BBOX[1], BBOX[3], n))
    lon = np.where(hot, -122.335 + rng.normal(0, 0.002, n), rng.uniform(BBOX[0], BBOX[2], n))
    return pd.DataFrame(
        {
            "report_number": [f"R{seed}-{i}" for i in range(n)],
            "offense_id": [f"O{seed}-{i}" for i in range(n)],
            "report_date_time": t.strftime("%Y-%m-%dT%H:%M:%S"),
            "offense_date": (t - pd.Timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
            "latitude": lat.round(6).astype(str),
            "longitude": lon.round(6).astype(str),
            "offense_category": np.where(rng.random(n) < 0.8, "PROPERTY CRIME", "VIOLENT CRIME"),
            "nibrs_crime_against_category": "PROPERTY",
            "nibrs_offense_code": "23H",
            "nibrs_offense_code_description": "All Other Larceny",
            "offense_sub_category": "x",
            "precinct": "West",
            "sector": "M",
            "beat": "M1",
            "neighborhood": "DOWNTOWN",
            "block_address": "1XX BLOCK",
            "shooting_type_group": "-",
        }
    ).astype("string")


def test_prospective_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("SEACRIME_OFFLINE", "1")
    monkeypatch.setenv("SEACRIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SEACRIME_ARTIFACTS_ROOT", str(tmp_path / "artifacts"))
    cfg = load_config(overrides={
        "grid": {"bbox": BBOX},
        "categories": {"property": {"offense_category": ["PROPERTY CRIME"]}, "all_located": {"offense_category": None}},
        "training": {"start": "2023-06-01", "val_start": "2024-07-01", "test_start": "2024-10-01", "test_end": None},
    })
    from seacrime.build import build_all
    from seacrime.export import export_all
    from seacrime.prospective.forecast import issue_forecasts, prospective_root
    from seacrime.prospective.score import score_all
    from seacrime.prospective.store import ForecastStore
    from seacrime.train import train_production

    # snapshot 1: two years of history, fetched 2025-03-01 12:00 UTC
    snap1 = clean_spd(_raw(30_000, "2023-01-01", "2025-03-01 03:00", 1), tuple(BBOX))
    reconcile(cfg.processed_dir, snap1, "s1", pd.Timestamp("2025-03-01T12:00:00Z"))
    now = pd.Timestamp("2025-03-01T12:20:00Z")
    build_all(cfg, extend_to=now + pd.Timedelta(hours=30))
    reg = train_production(cfg, ["property"], [1, 24], ["seasonal", "lgbm"], n_train=150, n_val=60, val_days=30)
    assert set(reg["property"]["24"]) == {"seasonal", "lgbm"}

    ids = issue_forecasts(cfg, ["property"], now=now)
    assert len(ids) == 2
    store = ForecastStore(prospective_root(cfg))
    fc = store.list_forecasts()
    assert (pd.to_datetime(fc["t0"]) > pd.to_datetime(fc["issued_at"])).all()  # frozen before windows open
    preds = store.load_predictions(ids[0])
    assert {"expected", "p_any", "lo90", "hi90", "relative_risk", "cell", "horizon_h"} <= set(preds.columns)
    assert preds["expected"].gt(0).all() and preds["p_any"].between(0, 1).all()
    # the forecast's feature cutoff never exceeds what was available
    cut = json.loads(fc.iloc[0]["feature_cutoffs"])
    assert pd.Timestamp(cut["spd_crime"]) <= pd.Timestamp("2025-03-01T13:00:00Z") - pd.Timedelta(hours=72)

    # nothing scorable yet: the windows lie after the snapshot
    assert score_all(cfg) == 0

    # snapshot 2 (two days later) includes incidents inside the forecast windows
    new = _raw(200, "2025-03-01 05:00", "2025-03-02 16:00", 2)  # local time = 13:00 UTC Mar 1 onwards
    snap2 = clean_spd(pd.concat([_raw(30_000, "2023-01-01", "2025-03-01 03:00", 1), new]), tuple(BBOX))
    s = reconcile(cfg.processed_dir, snap2, "s2", pd.Timestamp("2025-03-03T12:00:00Z"))
    assert s["new"] == 200
    assert score_all(cfg) == 4  # 2 models x 2 horizons
    assert score_all(cfg) == 0  # hourly re-runs against the same snapshot do no work
    sc = store.latest_scores()
    assert set(sc["maturity"]) == {"provisional"}  # < 30 days old: never final
    assert all(m["observed_total"] > 0 for m in sc["metrics"])

    out = export_all(cfg, tmp_path / "export")
    idx = json.loads((out / "forecasts" / "index.json").read_text())
    assert {it["status"] for it in idx["items"]} == {"scored_provisional"}
    doc = json.loads((out / "forecasts" / f"{ids[0]}.json").read_text())
    assert doc["provenance"]["kind"] == "actual" and doc["score"]["maturity"] == "provisional"
    hist = json.loads((out / "history" / "property.json").read_text())
    vals = list(hist["periods"]["last_7d"]["cells"].values())
    assert all(v is None or v >= 3 for v in vals)  # small counts suppressed
    for name in ("meta.json", "grid.geojson", "health.json", "evaluation/summary.json"):
        assert (out / name).exists()

    # scheduled live job: no backtest artifacts locally, so backtest sections come from a
    # previous export while forecasts and scores are fresh
    base = tmp_path / "base"
    (base / "evaluation").mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"models": [{"id": "tcn", "label": "TCN", "backtest_only": True}, {"id": "lgbm", "backtest_only": True}]}))
    (base / "evaluation" / "summary.json").write_text(json.dumps({"backtest": {"rows": [{"model_id": "lgbm"}]}, "ablations": [1],
                                                                  "prospective": {"rows": [], "timeseries": []}}))
    live = export_all(cfg, tmp_path / "live", max_live=1, max_replay=0, static_base=base)
    ev = json.loads((live / "evaluation" / "summary.json").read_text())
    assert ev["backtest"] == {"rows": [{"model_id": "lgbm"}]} and ev["ablations"] == [1]
    assert ev["prospective"]["rows"]  # live scores, not the base's empty section
    models = {m["id"]: m for m in json.loads((live / "meta.json").read_text())["models"]}
    assert "tcn" in models and not models["lgbm"].get("backtest_only")  # production entry wins
    assert len(json.loads((live / "forecasts" / "index.json").read_text())["items"]) == 1
