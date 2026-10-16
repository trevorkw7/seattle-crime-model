"""Integration tests: incident reconciliation, frozen-forecast store, scoring
maturity, weather-forecast availability and the read-only API."""

from __future__ import annotations

import json
import os
import stat

import numpy as np
import pandas as pd
import pytest

from seacrime.incidents import clean_spd, incidents, reconcile
from seacrime.prospective.store import ForecastStore

BBOX = (-122.44, 47.495, -122.235, 47.735)


def _raw(rows):
    cols = ["report_number", "offense_id", "report_date_time", "offense_date", "latitude", "longitude", "offense_category",
            "nibrs_crime_against_category", "nibrs_offense_code", "nibrs_offense_code_description", "offense_sub_category",
            "precinct", "sector", "beat", "neighborhood", "block_address", "shooting_type_group"]
    return pd.DataFrame([dict(zip(cols, r, strict=True)) for r in rows]).astype("string")


ROW = ["2024-1", "11", "2024-05-01T10:00:00", "2024-05-01T08:00:00", "47.61", "-122.33", "PROPERTY CRIME", "PROPERTY", "23H", "All Other Larceny", "x", "West", "M", "M1", "DOWNTOWN", "1XX BLOCK", "-"]


def test_clean_spd_location_status_and_duplicates():
    rows = [
        ROW,
        ROW[:1] + ["12"] + ROW[2:4] + ["REDACTED", "REDACTED"] + ROW[6:],
        ROW[:1] + ["13"] + ROW[2:4] + ["-1.0", "-1.0"] + ROW[6:],
        ROW[:1] + ["14"] + ROW[2:4] + ["89.99", "-122.33"] + ROW[6:],
        ROW[:6] + ["VIOLENT CRIME"] + ROW[7:],  # duplicate key, different content
    ]
    df = clean_spd(_raw(rows), BBOX)
    assert df.attrs["n_duplicate_keys_dropped"] == 1
    assert dict(df.set_index("offense_id")["loc_status"].astype(str)) == {"11": "ok", "12": "redacted", "13": "unknown", "14": "out_of_area"}
    assert df["report_ts_utc"].iloc[0] == pd.Timestamp("2024-05-01T17:00:00Z")


def test_reconcile_tracks_new_changed_removed(tmp_path):
    a = clean_spd(_raw([ROW, ROW[:1] + ["12"] + ROW[2:]]), BBOX)
    s1 = reconcile(tmp_path, a, "s1", pd.Timestamp("2024-05-02T00:00:00Z"))
    assert s1["new"] == 2 and s1["initial_backfill"]
    changed = ROW[:6] + ["ALL OTHER"] + ROW[7:]
    b = clean_spd(_raw([changed, ROW[:1] + ["99"] + ROW[2:]]), BBOX)
    s2 = reconcile(tmp_path, b, "s2", pd.Timestamp("2024-05-03T00:00:00Z"))
    assert (s2["new"], s2["changed"], s2["removed"]) == (1, 1, 1)
    st = pd.read_parquet(tmp_path / "spd_offenses.parquet").set_index("offense_id")
    assert not st.loc["12", "present_in_latest"]  # retained but flagged, never dropped
    assert st.loc["99", "first_seen_at_utc"] == pd.Timestamp("2024-05-03T00:00:00Z")
    assert not st.loc["99", "backfill"]
    assert st.loc["11", "n_changes"] == 1
    log = pd.read_parquet(tmp_path / "spd_offense_changes.parquet")
    assert set(log["field"]) == {"offense_category"}


def test_incidents_one_per_report():
    rows = [ROW, ROW[:1] + ["12"] + ROW[2:], ["2024-2"] + ROW[1:]]
    df = clean_spd(_raw(rows), BBOX)
    df["present_in_latest"] = True
    df["first_seen_at_utc"] = pd.Timestamp("2024-05-02T00:00:00Z")
    df["backfill"] = True
    inc = incidents(df, {"offense_category": ["PROPERTY CRIME"]})
    assert sorted(inc["incident_id"]) == ["2024-1", "2024-2"]
    assert inc.set_index("incident_id").loc["2024-1", "n_offenses"] == 2


# ---------------------------------------------------------------- forecast store
def _meta(fid, issued, t0):
    return {"forecast_id": fid, "issued_at": issued, "t0": t0, "model_id": "lgbm", "model_version": "v1", "category": "property",
            "horizons": [1, 6], "data_versions": {"spd_crime": "s1"}, "feature_cutoffs": {}, "provenance": "actual"}


def test_store_freeze_rules_and_integrity(tmp_path):
    st = ForecastStore(tmp_path)
    preds = pd.DataFrame({"cell": [1, 2], "horizon_h": [1, 1], "expected": [0.1, 0.2], "p_any": [0.09, 0.18], "lo90": [0, 0], "hi90": [1, 1], "relative_risk": [1, 1]})
    with pytest.raises(ValueError):
        st.freeze(_meta("f0", "2024-05-01T10:30:00+00:00", "2024-05-01T10:00:00+00:00"), preds)  # window opened before freeze
    st.freeze(_meta("f1", "2024-05-01T10:05:00+00:00", "2024-05-01T11:00:00+00:00"), preds)
    with pytest.raises(FileExistsError):
        st.freeze(_meta("f1", "2024-05-01T10:05:00+00:00", "2024-05-01T11:00:00+00:00"), preds)
    p = tmp_path / "forecasts" / "f1.parquet"
    assert not os.stat(p).st_mode & stat.S_IWUSR  # read-only
    pd.testing.assert_frame_equal(st.load_predictions("f1"), preds)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    preds.assign(expected=[9.0, 9.0]).to_parquet(p, index=False)  # tamper
    with pytest.raises(RuntimeError):
        st.load_predictions("f1")


def test_store_scores_keep_history(tmp_path):
    st = ForecastStore(tmp_path)
    st.add_score("f1", 6, "snapA", "provisional", {"capture_10": 0.3})
    st.add_score("f1", 6, "snapB", "mature", {"capture_10": 0.35})
    assert len(st.scores()) == 2
    latest = st.latest_scores()
    assert latest.iloc[0]["maturity"] == "mature"


# ------------------------------------------------------------ weather forecasts
def test_weather_forecast_uses_only_issued_runs(tmp_path):
    from seacrime.context import load_weather_forecasts

    origin = pd.Timestamp("2024-01-01T00:00:00Z")
    rows = []
    for run_h, temp in ((0, 10.0), (6, 20.0)):
        issued = origin + pd.Timedelta(hours=run_h + 4)
        for lead in range(0, 48, 3):
            rows.append({"source": "iem_mos:GFS", "issued_at_utc": issued, "valid_ts_utc": origin + pd.Timedelta(hours=run_h + lead),
                         "temp_c": temp, "wind_ms": 1.0, "rel_humidity": 50.0, "pop_6h_pct": 10.0, "qpf_6h_cat": 0.0})
    pd.DataFrame(rows).to_parquet(tmp_path / "weather_forecast_hourly.parquet")
    fc = load_weather_forecasts(tmp_path, origin, [6])[6]
    assert fc.loc[9, "temp_c"] == 10.0  # second run issued at h=10 is not yet available at t=9
    assert fc.loc[10, "temp_c"] == 20.0


# ----------------------------------------------------------------------- API
def test_api_serves_contract_documents(tmp_path, monkeypatch):
    (tmp_path / "forecasts").mkdir()
    prov = {"kind": "actual", "generated_at": "2024-01-01T00:00:00Z", "note": ""}
    (tmp_path / "health.json").write_text(json.dumps({"provenance": prov, "sources": [{"name": "spd_crime", "status": "ok"}], "models": [], "jobs": []}))
    (tmp_path / "forecasts" / "index.json").write_text(json.dumps({"provenance": prov, "latest": {"lgbm": {"property": "fc1"}}, "items": []}))
    (tmp_path / "forecasts" / "fc1.json").write_text(json.dumps({
        "provenance": prov, "issued_at": "x", "model_version": "v", "columns": ["expected", "p_any", "relative_risk", "lo90", "hi90"],
        "horizons": {"6": {"window_start": "a", "window_end": "b", "cells": {"r1_c1": [0.1, 0.09, 1.2, 0, 1]}}}}))
    monkeypatch.setenv("SEACRIME_EXPORT_DIR", str(tmp_path))
    import importlib

    import seacrime.api.app as appmod

    importlib.reload(appmod)
    from fastapi.testclient import TestClient

    c = TestClient(appmod.app)
    assert c.get("/api/healthz").json()["status"] == "ok"
    r = c.get("/api/forecast", params={"model": "lgbm", "category": "property", "horizon": 6}).json()
    assert r["cells"][0] == {"cell_id": "r1_c1", "expected": 0.1, "p_any": 0.09, "relative_risk": 1.2, "lo90": 0, "hi90": 1}
    assert c.get("/data/health.json").status_code == 200
    assert c.get("/api/forecast", params={"horizon": 3}).status_code == 404
    np.testing.assert_equal(c.get("/data/../pyproject.toml").status_code in (404, 400), True)


def test_public_job_messages_have_no_paths():
    from seacrime.export import public_message

    msg = '{"out": "/home/user/seattle-crime-model/web/public/data"} File "/usr/lib/python3/x.py", line 3'
    out = public_message(msg)
    assert "/home" not in out and "/usr" not in out and "<path>" in out
    assert public_message(None) == ""
    assert len(public_message("x" * 1000)) == 300


# ------------------------------------------------------------ live job state
def _live_state(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    monkeypatch.setenv("SEACRIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SEACRIME_ARTIFACTS_ROOT", str(tmp_path / "artifacts"))
    spec = importlib.util.spec_from_file_location("live_state", Path(__file__).resolve().parents[1] / "scripts" / "live_state.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_live_state_prune_keeps_latest_snapshot(tmp_path, monkeypatch):
    ls = _live_state(monkeypatch, tmp_path)
    raw = tmp_path / "data" / "raw" / "spd_crime"
    for sid in ("20261001T000000Z", "20261002T000000Z", "20261003T000000Z"):
        (raw / sid).mkdir(parents=True)
        (raw / sid / "manifest.json").write_text("{}")
        (raw / sid / "part-0.csv").write_text("x" * 100)
    (raw / "partial").mkdir()  # no manifest: not a snapshot, left alone
    st = ls.status(7)
    assert st["bootstrap"] and st["retrain"] and not st["has_store"]
    assert ls.prune()["removed_snapshots"] == 2
    assert sorted(p.name for p in raw.iterdir()) == ["20261003T000000Z", "partial"]


def test_live_state_store_roundtrip(tmp_path, monkeypatch):
    ls = _live_state(monkeypatch, tmp_path)
    st = ForecastStore(tmp_path / "data" / "prospective")
    preds = pd.DataFrame({"cell": [1], "horizon_h": [1], "expected": [0.1], "p_any": [0.09], "lo90": [0], "hi90": [1], "relative_risk": [1.0]})
    st.freeze(_meta("f1", "2024-05-01T10:05:00+00:00", "2024-05-01T11:00:00+00:00"), preds)
    arc = tmp_path / "store.tar.gz"
    ls.pack_store(arc)
    with pytest.raises(SystemExit):
        ls.unpack_store(arc)  # never overwrites a live store
    import shutil

    shutil.rmtree(tmp_path / "data" / "prospective")
    ls.unpack_store(arc)
    pd.testing.assert_frame_equal(ForecastStore(tmp_path / "data" / "prospective").load_predictions("f1"), preds)
