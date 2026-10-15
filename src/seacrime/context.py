"""Load contextual sources (weather, events, transit, static geography) and
align them to the cube's hourly index and the grid lattice.

Every loader degrades gracefully: a missing file yields ``None`` and the
corresponding feature group is simply absent (recorded in model metadata).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .features import ContextData
from .grid import Grid

log = logging.getLogger(__name__)


def us_holidays(start: str = "2017-01-01", end: str = "2028-12-31") -> set:
    from pandas.tseries.holiday import USFederalHolidayCalendar

    return set(USFederalHolidayCalendar().holidays(start, end).date)


def load_weather(processed: Path, origin: pd.Timestamp) -> pd.DataFrame | None:
    p = processed / "weather_hourly.parquet"
    if not p.exists():
        return None
    w = pd.read_parquet(p)
    ts = pd.to_datetime(w["ts_utc"], utc=True).dt.floor("h")
    w = w.assign(h=((ts - origin) // pd.Timedelta(hours=1)).astype(np.int64))
    cols = [c for c in ("temp_c", "precip_mm", "wind_ms") if c in w]
    w = w.groupby("h")[cols].mean(numeric_only=True)
    return w[w.index >= 0]


def load_weather_forecasts(processed: Path, origin: pd.Timestamp, horizons: list[int], source: str = "iem_mos:GFS") -> dict | None:
    """For every issue hour t, values from the latest MOS run *issued* at or
    before t, evaluated at the window midpoint t + h/2 (linear interpolation in
    valid time; 6-hour precipitation fields take the period covering the
    midpoint). Cached to parquet."""
    p = processed / "weather_forecast_hourly.parquet"
    if not p.exists():
        return None
    cache = processed / f"weather_fc_by_issue_{source.replace(':', '_')}.parquet"
    if cache.exists() and cache.stat().st_mtime >= p.stat().st_mtime:
        tab = pd.read_parquet(cache)
    else:
        old_tab = pd.read_parquet(cache) if cache.exists() else None
        f = pd.read_parquet(p)
        f = f[f["source"] == source].copy()
        if old_tab is not None and len(old_tab):
            # incremental: recompute only issue hours near the end of the cached table
            t_from = int(old_tab["t"].max()) - 96
            iss = ((pd.to_datetime(f["issued_at_utc"], utc=True) - origin) / pd.Timedelta(hours=1)).apply(np.ceil)
            f = f[iss >= t_from - 24 * 4]
            old_tab = old_tab[old_tab["t"] < t_from]
        f["iss_h"] = ((pd.to_datetime(f["issued_at_utc"], utc=True) - origin) / pd.Timedelta(hours=1)).apply(np.ceil).astype(np.int64)
        f["val_h"] = ((pd.to_datetime(f["valid_ts_utc"], utc=True) - origin) // pd.Timedelta(hours=1)).astype(np.int64)
        f = f.sort_values(["iss_h", "val_h"])
        runs = {k: g for k, g in f.groupby("iss_h")}
        run_keys = np.array(sorted(runs))
        t_all = np.arange(max(run_keys.min(), 0), run_keys.max() + 24 * 3)
        rows = []
        for h in horizons:
            ridx = np.searchsorted(run_keys, t_all, side="right") - 1
            for t, ri in zip(t_all, ridx, strict=True):
                if ri < 0:
                    continue
                g = runs[run_keys[ri]]
                mid = t + h / 2.0
                vh = g["val_h"].to_numpy(dtype=float)
                if mid > vh.max() + 3:  # run too old to cover the window
                    continue
                row = {"h": h, "t": t, "run_age_h": float(t - run_keys[ri])}
                for c in ("temp_c", "wind_ms", "rel_humidity"):
                    v = g[c].to_numpy(dtype=float)
                    ok = np.isfinite(v)
                    row[c] = float(np.interp(mid, vh[ok], v[ok])) if ok.sum() >= 2 else np.nan
                for c in ("pop_6h_pct", "qpf_6h_cat"):
                    v = g[c].to_numpy(dtype=float)
                    ok = np.isfinite(v)
                    if ok.any():
                        # 6-h fields are valid for the 6 h ending at valid time
                        cand = vh[ok]
                        j = np.searchsorted(cand, mid, side="left")
                        row[c] = float(v[ok][min(j, ok.sum() - 1)])
                    else:
                        row[c] = np.nan
                rows.append(row)
        tab = pd.DataFrame(rows)
        if old_tab is not None and len(old_tab):
            tab = pd.concat([old_tab, tab[tab["t"] >= t_from]], ignore_index=True)
        tab.to_parquet(cache, index=False)
    return {h: g.drop(columns=["h"]).set_index("t") for h, g in tab.groupby("h") if h in horizons}


SPORTS = {"mlb", "nfl", "mls", "nhl", "wnba", "ncaaf", "nwsl", "soccer_intl"}
MAJOR_ATTENDANCE = 10_000


def load_events(processed: Path, origin: pd.Timestamp, typical_before: str = "2025-01-01") -> pd.DataFrame | None:
    """Scheduled events with leakage-safe attributes.

    * ``known_h``: hour index from which the event may be used
      (``schedule_known_at_utc``; conservative estimate of schedule publication).
    * ``attendance``: only *ex-ante* values. Sports attendance is announced after
      the game, so it is replaced by the category median over games before
      ``typical_before``; permit attendance is the applicant's advance estimate.
    """
    p = processed / "events.parquet"
    if not p.exists():
        return None
    e = pd.read_parquet(p)
    if "status" in e:
        e = e[~e["status"].astype(str).str.lower().isin({"cancelled", "canceled", "postponed"})]
    e = e.dropna(subset=["lat", "lon", "start_utc"])
    e = e[e["category"] != "holiday"].copy()
    start = pd.to_datetime(e["start_utc"], utc=True)
    end = pd.to_datetime(e["end_utc"], utc=True).fillna(start + pd.Timedelta(hours=3))
    if "schedule_known_at_utc" in e:
        known = pd.to_datetime(e["schedule_known_at_utc"], utc=True).fillna(start - pd.Timedelta(days=1))
    else:
        known = start - pd.Timedelta(days=1)
    att = pd.to_numeric(e["attendance"], errors="coerce") if "attendance" in e else pd.Series(np.nan, index=e.index)
    kind = e["attendance_kind"].astype(str) if "attendance_kind" in e else pd.Series("", index=e.index)
    ex_ante = att.where(kind == "applicant_estimate")
    hist = (start < pd.Timestamp(typical_before, tz="UTC")) & (kind == "announced_post_event")
    typical = att[hist].groupby(e.loc[hist, "category"]).median()
    att_safe = ex_ante.fillna(e["category"].map(typical)).fillna(att[hist].median())  # new categories (e.g. 2026 World Cup)
    out = pd.DataFrame(
        {
            "start_h": ((start - origin) // pd.Timedelta(hours=1)).astype(np.int64),
            "end_h": ((end - origin) // pd.Timedelta(hours=1)).astype(np.int64) + 1,
            "known_h": ((known - origin) // pd.Timedelta(hours=1)).astype(np.int64),
            "lat": e["lat"].astype(float),
            "lon": e["lon"].astype(float),
            "category": e["category"].astype(str),
            "attendance": att_safe.astype(float),
        }
    )
    out["major"] = out["category"].isin(SPORTS) | (out["attendance"] >= MAJOR_ATTENDANCE)
    return out.reset_index(drop=True)


def load_transit_how(processed: Path, grid: Grid) -> np.ndarray | None:
    stops_p = processed / "transit_stops.parquet"
    how_p = processed / "transit_stop_how.parquet"
    if not (stops_p.exists() and how_p.exists()):
        return None
    stops = pd.read_parquet(stops_p)
    how = pd.read_parquet(how_p)
    stops = stops.drop_duplicates("stop_id")
    stops["cell"] = grid.locate(stops["lon"].to_numpy(), stops["lat"].to_numpy())
    m = how.merge(stops[["stop_id", "cell"]], on="stop_id", how="inner")
    m = m[m["cell"] >= 0]
    arr = np.zeros((168, grid.n_cells), dtype=np.float32)
    np.add.at(arr, (m["hour_of_week"].to_numpy(dtype=int), m["cell"].to_numpy(dtype=int)), m["scheduled_departures"].to_numpy(dtype=float))
    return arr.reshape(168, grid.n_rows, grid.n_cols)


def load_static(processed: Path, grid: Grid) -> dict[str, np.ndarray]:
    """Per-cell static attributes from geographic layers (if present)."""
    out: dict[str, np.ndarray] = {}
    xs, ys = grid.centroids_xy()
    # distance to downtown (Westlake Center) is always available
    from pyproj import Transformer

    dx, dy = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform(-122.3376, 47.6119)
    out["dist_downtown_km"] = (np.sqrt((xs - dx) ** 2 + (ys - dy) ** 2) / 1000).reshape(grid.n_rows, grid.n_cols)
    try:
        import geopandas as gpd
        from shapely.geometry import box

        cells = gpd.GeoDataFrame(
            {"cell": np.arange(grid.n_cells)},
            geometry=[box(x - grid.cell_size_m / 2, y - grid.cell_size_m / 2, x + grid.cell_size_m / 2, y + grid.cell_size_m / 2) for x, y in zip(xs, ys, strict=True)],
            crs=grid.crs,
        )
        cell_area = grid.cell_size_m**2
        tracts_p = processed / "census_tracts.parquet"
        if tracts_p.exists():
            tr = gpd.read_parquet(tracts_p).to_crs(grid.crs)
            if "population" in tr:
                tr = tr[["population", "geometry"]].copy()
                tr["dens"] = tr["population"].astype(float) / tr.geometry.area
                ov = gpd.overlay(cells, tr, how="intersection", keep_geom_type=True)
                ov["pop"] = ov["dens"] * ov.geometry.area
                pop = ov.groupby("cell")["pop"].sum().reindex(range(grid.n_cells), fill_value=0.0)
                out["population"] = pop.to_numpy().reshape(grid.n_rows, grid.n_cols)
        zon_p = processed / "zoning.parquet"
        if zon_p.exists():
            z = gpd.read_parquet(zon_p).to_crs(grid.crs)
            catcol = next((c for c in ("zone_category", "category", "zone_class") if c in z), None)
            if catcol:
                ov = gpd.overlay(cells, z[[catcol, "geometry"]], how="intersection", keep_geom_type=True)
                ov["a"] = ov.geometry.area / cell_area
                piv = ov.pivot_table(index="cell", columns=catcol, values="a", aggfunc="sum").reindex(range(grid.n_cells)).fillna(0.0)
                for c in piv.columns:
                    key = "zone_" + "".join(ch if ch.isalnum() else "_" for ch in str(c).lower())[:24]
                    out[key] = piv[c].to_numpy().reshape(grid.n_rows, grid.n_cols)
        streets_p = processed / "streets.parquet"
        if streets_p.exists():
            s = gpd.read_parquet(streets_p).to_crs(grid.crs)
            ov = gpd.overlay(s[["geometry"]], cells, how="intersection", keep_geom_type=True)
            ln = ov.groupby("cell").geometry.apply(lambda g: g.length.sum()).reindex(range(grid.n_cells), fill_value=0.0)
            out["street_km"] = (ln.to_numpy() / 1000).reshape(grid.n_rows, grid.n_cols)
    except Exception as e:  # geometry layers are optional
        log.warning("static geography features skipped: %s", e)
    return out


def load_context(cfg: Config, grid: Grid, origin: pd.Timestamp, cache: bool = True) -> ContextData:
    processed = cfg.processed_dir
    static_cache = processed / f"static_{int(grid.cell_size_m)}m.npz"
    if cache and static_cache.exists() and static_cache.stat().st_mtime > max(
        [p.stat().st_mtime for p in processed.glob("*.parquet") if p.stem in {"census_tracts", "zoning", "streets"}] or [0]
    ):
        static = dict(np.load(static_cache))
    else:
        static = load_static(processed, grid)
        np.savez(static_cache, **static)
    ctx = ContextData(
        weather=load_weather(processed, origin),
        weather_fc=load_weather_forecasts(processed, origin, cfg.horizons),
        events=load_events(processed, origin),
        transit_how=load_transit_how(processed, grid),
        static=static,
        holidays=us_holidays(),
    )
    log.info(
        "context: weather=%s events=%s transit=%s static=%s",
        None if ctx.weather is None else len(ctx.weather),
        None if ctx.events is None else len(ctx.events),
        ctx.transit_how is not None,
        list(static),
    )
    return ctx
