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


def load_events(processed: Path, origin: pd.Timestamp, categories: tuple[str, ...] | None = None) -> pd.DataFrame | None:
    p = processed / "events.parquet"
    if not p.exists():
        return None
    e = pd.read_parquet(p)
    if "status" in e:
        e = e[~e["status"].astype(str).str.lower().isin({"cancelled", "canceled", "postponed"})]
    e = e.dropna(subset=["lat", "lon", "start_utc"])
    if categories:
        e = e[e["category"].isin(categories)]
    e = e[e["category"] != "holiday"]
    start = pd.to_datetime(e["start_utc"], utc=True)
    end = pd.to_datetime(e["end_utc"], utc=True).fillna(start + pd.Timedelta(hours=3))
    out = pd.DataFrame(
        {
            "start_h": ((start - origin) // pd.Timedelta(hours=1)).astype(np.int64),
            "end_h": ((end - origin) // pd.Timedelta(hours=1)).astype(np.int64) + 1,
            "lat": e["lat"].astype(float),
            "lon": e["lon"].astype(float),
            "category": e["category"].astype(str),
            "attendance": pd.to_numeric(e.get("attendance"), errors="coerce") if "attendance" in e else np.nan,
        }
    )
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
