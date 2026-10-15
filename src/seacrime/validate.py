"""Data validation report generated from the actual downloaded snapshots.

    python -m seacrime.validate      # writes reports/data_validation.{md,json}
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from .config import REPO_ROOT, Config, load_config
from .ingest.socrata import latest_snapshot, read_snapshot
from .timeutil import ambiguous_mask, nonexistent_mask

log = logging.getLogger(__name__)
TZ = "America/Los_Angeles"


def _manifest(cfg: Config, source: str) -> dict | None:
    s = latest_snapshot(cfg.raw_dir, source)
    return json.loads((s / "manifest.json").read_text()) if s else None


def _gaps(ts: pd.Series, freq: str = "D", start=None) -> dict:
    """Empty periods (local calendar) between first and last record."""
    loc = pd.to_datetime(ts, utc=True).dt.tz_convert(TZ).dt.tz_localize(None)
    if start is not None:
        loc = loc[loc >= pd.Timestamp(start)]
    c = loc.dt.floor(freq).value_counts().sort_index()
    full = pd.date_range(c.index.min(), c.index.max(), freq=freq)
    c = c.reindex(full, fill_value=0)
    zero = c[c == 0]
    runs, cur = [], None
    for d in zero.index:
        if cur and d - cur[1] == pd.Timedelta(1, freq):
            cur[1] = d
        else:
            if cur:
                runs.append(cur)
            cur = [d, d]
    if cur:
        runs.append(cur)
    longest = sorted(runs, key=lambda r: r[1] - r[0], reverse=True)[:5]
    med = c.rolling(28, min_periods=7).median()
    low = c[(c < 0.3 * med) & (c.index < c.index.max() - pd.Timedelta(days=14))]
    return {
        "periods": int(len(c)),
        "empty_periods": int(len(zero)),
        "longest_empty_runs": [[str(a.date()), str(b.date())] for a, b in longest],
        "anomalously_low_periods(<30% of 28-period median)": [str(d.date()) for d in low.index[:20]],
        "n_anomalously_low": int(len(low)),
    }


def validate_spd_crime(cfg: Config) -> dict:
    snap = latest_snapshot(cfg.raw_dir, "spd_crime")
    raw = read_snapshot(snap)
    man = _manifest(cfg, "spd_crime")
    store = pd.read_parquet(cfg.processed_dir / "spd_offenses.parquet")
    r: dict = {
        "snapshot_id": man["snapshot_id"],
        "fetched_at_utc": man["fetched_at_utc"],
        "publisher_rows_updated_at": man["dataset_metadata"]["rowsUpdatedAt_iso"],
        "rows": int(len(raw)),
    }
    # identifiers & duplicates
    key = raw["report_number"] + "|" + raw["offense_id"]
    r["distinct_report_numbers"] = int(raw["report_number"].nunique())
    r["offense_id_duplicates"] = int(raw["offense_id"].duplicated().sum())
    r["key_duplicates(report_number,offense_id)"] = int(key.duplicated().sum())
    r["exact_duplicate_rows"] = int(raw.duplicated().sum())
    r["offenses_per_report"] = raw.groupby("report_number").size().describe()[["mean", "50%", "max"]].round(2).to_dict()
    # missingness per column
    miss = {}
    for c in raw.columns:
        s = raw[c].astype("string")
        miss[c] = {
            "null": int(s.isna().sum()),
            "dash_placeholder": int((s == "-").sum()),
            "REDACTED": int((s.str.upper() == "REDACTED").sum()),
        }
    r["missing_by_column"] = miss
    # time
    rt = pd.to_datetime(raw["report_date_time"], errors="coerce", format="mixed")
    ot = pd.to_datetime(raw["offense_date"], errors="coerce", format="mixed")
    r["report_time_range_local"] = [str(rt.min()), str(rt.max())]
    r["report_year_counts"] = {int(k): int(v) for k, v in rt.dt.year.value_counts().sort_index().items() if k >= 2005}
    r["records_before_2008"] = int((rt < "2008-01-01").sum())
    r["timezone"] = "Timestamps are floating local time (America/Los_Angeles), no offset; converted to UTC."
    r["dst_ambiguous_report_times"] = int(ambiguous_mask(raw["report_date_time"]).sum())
    r["dst_nonexistent_report_times"] = int(nonexistent_mask(raw["report_date_time"]).sum())
    since = rt >= "2019-06-01"
    r["report_time_seconds_precision_share_since_2020"] = float((rt[rt >= "2020-01-01"].dt.second != 0).mean())
    r["offense_time_exact_midnight_share_since_2019_06"] = float(((ot.dt.hour == 0) & (ot.dt.minute == 0) & (ot.dt.second == 0))[since].mean())
    lag_h = (rt - ot).dt.total_seconds() / 3600
    r["report_minus_offense_hours_quantiles_since_2019_06"] = {str(q): round(float(lag_h[since].quantile(q)), 1) for q in (0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99)}
    r["report_before_offense_share"] = float((lag_h[since] < 0).mean())
    # location
    st = store
    r["location_status_counts"] = st["loc_status"].value_counts().to_dict()
    yr = st["report_ts_utc"].dt.year
    tab = pd.crosstab(yr[yr >= 2015], st.loc[yr >= 2015, "loc_status"], normalize="index").round(4)
    r["location_status_share_by_year"] = {int(k): v for k, v in tab.T.to_dict().items()}
    cat_tab = pd.crosstab(st["offense_category"], st["loc_status"], normalize="index").round(4)
    r["location_status_share_by_category"] = cat_tab.T.to_dict()
    ok = st[st["loc_status"] == "ok"]
    coords = ok["lat"].round(6).astype(str) + "," + ok["lon"].round(6).astype(str)
    r["distinct_coordinates"] = int(coords.nunique())
    r["records_per_distinct_coordinate_median"] = float(coords.value_counts().median())
    r["records_per_distinct_coordinate_p99"] = float(coords.value_counts().quantile(0.99))
    r["coordinate_precision_note"] = (
        "SPD blurs offense locations to the hundred block (block_address e.g. '24XX BLOCK OF 4TH AVE S'); coordinates are block "
        "centroids, so positional uncertainty is roughly +/-50-150 m along the street. Some incidents with unknown location are "
        "coded as (-1, -1) and others as REDACTED (sensitive offense types)."
    )
    # temporal gaps (report time, since 2019-06)
    r["daily_gaps_report_time_since_2019_06"] = _gaps(st["report_ts_utc"], "D", start="2019-06-01")
    # reconciliation
    logp = cfg.processed_dir / "spd_reconcile_log.jsonl"
    r["reconciliation_log"] = [json.loads(x) for x in logp.read_text().splitlines()] if logp.exists() else []
    r["publication_completeness"] = completeness_curve(st, pd.Timestamp(man["fetched_at_utc"]))
    return r


def completeness_curve(store: pd.DataFrame, fetched_at: pd.Timestamp) -> dict:
    """Estimate how complete recent report-days are in a single snapshot by
    comparing each recent day's count with the same weekday's mean over weeks
    5-12 before the snapshot (assumed mature). Single-snapshot estimate; the
    prospective engine measures true publication delays across snapshots."""
    s = store[store["present_in_latest"]]
    loc_day = s["report_ts_utc"].dt.tz_convert(TZ).dt.floor("D").dt.tz_localize(None)
    c = s.groupby(loc_day)["report_number"].nunique()
    today = fetched_at.tz_convert(TZ).floor("D").tz_localize(None)
    days = pd.date_range(today - pd.Timedelta(days=84), today, freq="D")
    c = c.reindex(days, fill_value=0)
    ref = c[(c.index <= today - pd.Timedelta(days=35)) & (c.index > today - pd.Timedelta(days=84))]
    ref_dow = ref.groupby(ref.index.dayofweek).mean()
    out = []
    for age in range(0, 35):
        d = today - pd.Timedelta(days=age)
        exp = ref_dow.get(d.dayofweek, np.nan)
        out.append({"age_days": age, "date": str(d.date()), "reports": int(c.get(d, 0)), "expected": round(float(exp), 1), "ratio": round(float(c.get(d, 0) / exp), 3) if exp else None})
    df = pd.DataFrame(out)
    bands = {}
    for lo, hi in ((0, 1), (1, 3), (3, 7), (7, 14), (14, 21), (21, 35)):
        m = (df["age_days"] >= lo) & (df["age_days"] < hi)
        bands[f"{lo}-{hi}d"] = round(float(df.loc[m, "reports"].sum() / max(df.loc[m, "expected"].sum(), 1e-9)), 3)
    return {"method": completeness_curve.__doc__.strip().replace("\n", " "), "by_age_band_ratio": bands, "daily": out}


def validate_simple(cfg: Config, name: str, ts_col: str = "ts_utc", key: str | None = None) -> dict:
    p = cfg.processed_dir / f"{name}.parquet"
    if not p.exists():
        return {"missing": True}
    df = pd.read_parquet(p)
    man = _manifest(cfg, name)
    r = {"rows": int(len(df)), "columns": {c: str(t) for c, t in df.dtypes.items()}}
    if man:
        r.update({"snapshot_id": man["snapshot_id"], "fetched_at_utc": man["fetched_at_utc"], "raw_rows": man["row_count"],
                  "publisher_rows_updated_at": man["dataset_metadata"]["rowsUpdatedAt_iso"]})
    if ts_col in df:
        ts = pd.to_datetime(df[ts_col], utc=True)
        r["time_range_utc"] = [str(ts.min()), str(ts.max())]
        if man:
            r["latest_record_age_hours_at_fetch"] = round(float((pd.Timestamp(man["fetched_at_utc"]) - ts.max()) / pd.Timedelta(hours=1)), 2)
        r["daily_gaps"] = _gaps(ts, "D")
    if key and key in df:
        r["duplicate_keys"] = int(df[key].duplicated().sum())
    if "lat" in df:
        r["located_share"] = float(df["lat"].notna().mean())
    r["null_share"] = {c: round(float(df[c].isna().mean()), 4) for c in df.columns}
    return r


def validate_context(cfg: Config) -> dict:
    out = {}
    p = cfg.processed_dir
    if (p / "weather_hourly.parquet").exists():
        w = pd.read_parquet(p / "weather_hourly.parquet")
        ts = pd.to_datetime(w["ts_utc"], utc=True)
        full = pd.date_range(ts.min().floor("h"), ts.max().floor("h"), freq="h")
        out["weather_hourly"] = {"rows": int(len(w)), "range": [str(ts.min()), str(ts.max())],
                                 "missing_hours": int(len(full.difference(pd.DatetimeIndex(ts.dt.floor("h"))))),
                                 "null_share": {c: round(float(w[c].isna().mean()), 4) for c in w.columns}}
    if (p / "events.parquet").exists():
        e = pd.read_parquet(p / "events.parquet")
        out["events"] = {"rows": int(len(e)), "by_category": e["category"].value_counts().to_dict(),
                         "range": [str(pd.to_datetime(e["start_utc"], utc=True).min()), str(pd.to_datetime(e["start_utc"], utc=True).max())]}
    for name in ("transit_stops", "transit_stop_how", "census_tracts", "zoning", "streets", "city_limits", "spd_beats", "news_items", "weather_forecast_hourly"):
        f = p / f"{name}.parquet"
        if f.exists():
            out[name] = {"rows": int(len(pd.read_parquet(f, columns=None)))}
    return out


def render_md(rep: dict) -> str:
    c = rep["spd_crime"]
    L = []
    a = L.append
    a("# Data validation report\n")
    a(f"Generated: {rep['generated_at']} from the snapshots listed below. All numbers are computed by `python -m seacrime.validate` on the actual downloads.\n")
    a("## SPD Crime Data (tazs-3rd5)\n")
    a(f"* Snapshot `{c['snapshot_id']}` fetched {c['fetched_at_utc']}; publisher `rowsUpdatedAt` {c['publisher_rows_updated_at']}.")
    a(f"* Rows (offenses): **{c['rows']:,}**; distinct report numbers: {c['distinct_report_numbers']:,}; offenses per report (mean/median/max): {c['offenses_per_report']}.")
    a(f"* Report-time range (local): {c['report_time_range_local'][0]} → {c['report_time_range_local'][1]}; {c['records_before_2008']} records dated before 2008 (legacy/erroneous dates).")
    a(f"* Identifiers: `offense_id` duplicates {c['offense_id_duplicates']}; duplicate `(report_number, offense_id)` keys {c['key_duplicates(report_number,offense_id)']} (same key, differing content; resolved deterministically), exact duplicate rows {c['exact_duplicate_rows']}.")
    a(f"* Time zones: {c['timezone']} DST-ambiguous report times (fall-back hour): {c['dst_ambiguous_report_times']:,} (assigned to the first, PDT occurrence); nonexistent spring-forward times: {c['dst_nonexistent_report_times']:,} (shifted forward).")
    a(f"* Timestamp precision: share of report times with non-zero seconds since 2020: {c['report_time_seconds_precision_share_since_2020']:.3f} (Mark43 RMS records to the second). Offense start times at exactly 00:00:00 since 2019-06: {c['offense_time_exact_midnight_share_since_2019_06']:.3f} (date-only entries) — the reason the primary target uses report time.")
    a(f"* Report − offense delay (hours) quantiles since 2019-06: {c['report_minus_offense_hours_quantiles_since_2019_06']}; report before offense: {c['report_before_offense_share']:.4f}.")
    a(f"* Location status (all years): {c['location_status_counts']}.")
    a("* Location status share by offense category:\n")
    a("| category | ok | redacted | unknown (-1,-1) | out_of_area |\n|---|---|---|---|---|")
    for cat, v in c["location_status_share_by_category"].items():
        a(f"| {cat} | {v.get('ok', 0):.3f} | {v.get('redacted', 0):.3f} | {v.get('unknown', 0):.3f} | {v.get('out_of_area', 0):.4f} |")
    a("\n* Location status share by report year:\n")
    a("| year | ok | redacted | unknown | out_of_area |\n|---|---|---|---|---|")
    for y, v in c["location_status_share_by_year"].items():
        a(f"| {y} | {v.get('ok', 0):.3f} | {v.get('redacted', 0):.3f} | {v.get('unknown', 0):.3f} | {v.get('out_of_area', 0):.4f} |")
    a(f"\n* Coordinate uncertainty: {c['distinct_coordinates']:,} distinct coordinates; median {c['records_per_distinct_coordinate_median']:.0f} records per coordinate (p99 {c['records_per_distinct_coordinate_p99']:.0f}). {c['coordinate_precision_note']}")
    g = c["daily_gaps_report_time_since_2019_06"]
    a(f"* Temporal gaps (report day, since 2019-06): {g['empty_periods']} empty days of {g['periods']}; longest empty runs {g['longest_empty_runs']}; {g['n_anomalously_low']} anomalously low days (<30% of rolling median), e.g. {g['anomalously_low_periods(<30% of 28-period median)'][:8]}.")
    pc = c["publication_completeness"]
    a(f"* **Publication completeness (single-snapshot estimate)** — ratio of reports present vs. same-weekday expectation, by age of report day: {pc['by_age_band_ratio']}. Method: {pc['method']}")
    a("  The most recent day is heavily incomplete; daily counts are also volatile (batch approval), so feature availability is set conservatively to 72 h and scoring maturity to 30 days.")
    a(f"* Reconciliation: {c['reconciliation_log']}\n")
    for name, title in (("spd_calls", "SPD Call Data (33kz-ixgy)"), ("fire_911", "Seattle Real Time Fire 911 Calls (kzjm-xkqj)")):
        v = rep[name]
        if v.get("missing"):
            continue
        a(f"## {title}\n")
        a(f"* Snapshot `{v.get('snapshot_id')}` fetched {v.get('fetched_at_utc')}; raw rows {v.get('raw_rows'):,} → processed rows {v['rows']:,}.")
        a(f"* Time range (UTC): {v['time_range_utc']}; latest record age at fetch: **{v.get('latest_record_age_hours_at_fetch')} h** (observed publication delay).")
        a(f"* Located share: {v.get('located_share', float('nan')):.3f}; duplicate keys after processing: {v.get('duplicate_keys')}.")
        dg = v["daily_gaps"]
        a(f"* Daily gaps: {dg['empty_periods']} empty days of {dg['periods']}; longest {dg['longest_empty_runs'][:3]}; anomalously low days: {dg['n_anomalously_low']}.\n")
    a("## Contextual sources\n")
    a("```json\n" + json.dumps(rep["context"], indent=1, default=str)[:4000] + "\n```\n")
    a("## Leakage controls derived from this report\n")
    a("* Feature availability lags: crime 72 h after report time, SPD calls 96 h, Fire 911 1 h (see configs/default.yaml).")
    a("* Backfilled rows have unknown true publication times (`backfill=True` in the store); live operation records `first_seen_at_utc` per snapshot.")
    a("* Scores for windows younger than 30 days are labelled provisional and recomputed on every new snapshot.")
    return "\n".join(L) + "\n"


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    rep = {
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
        "spd_crime": validate_spd_crime(cfg),
        "spd_calls": validate_simple(cfg, "spd_calls", key="event_id"),
        "fire_911": validate_simple(cfg, "fire_911", key=None),
        "context": validate_context(cfg),
    }
    out = REPO_ROOT / "reports"
    out.mkdir(exist_ok=True)
    (out / "data_validation.json").write_text(json.dumps(rep, indent=1, default=str))
    (out / "data_validation.md").write_text(render_md(rep))
    print((out / "data_validation.md").read_text()[:6000])


if __name__ == "__main__":
    main()
