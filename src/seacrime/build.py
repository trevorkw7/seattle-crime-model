"""Build space-time cubes and the active-cell mask from processed tables.

    python -m seacrime.build
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from .config import Config, load_config
from .dataset import Cube, build_cube
from .grid import Grid
from .incidents import incidents, load_store

log = logging.getLogger(__name__)

CUBE_ORIGIN = pd.Timestamp("2018-01-01T00:00:00Z")
MIN_INCIDENTS_ACTIVE = 5  # cells with fewer located incidents since 2019-06 are not forecast


def cube_dir(cfg: Config):
    return cfg.processed_dir / f"cubes_{int(cfg['grid']['cell_size_m'])}m"


def data_end(cfg: Config) -> pd.Timestamp:
    """End of the cube = fetch time of the latest SPD snapshot (floored to hour)."""
    log_p = cfg.processed_dir / "spd_reconcile_log.jsonl"
    store = load_store(cfg.processed_dir)
    return pd.Timestamp(store["last_seen_at_utc"].max()).floor("h") if log_p.exists() else pd.Timestamp.now("UTC").floor("h")


def build_all(cfg: Config, extend_to: pd.Timestamp | None = None) -> dict:
    """Build all cubes. ``extend_to`` pads the time axis with empty future hours
    (needed for live forecasting); ``data_end`` in each cube's metadata always
    records the end of *observed* data, which backtests use."""
    grid = Grid.from_config(cfg)
    out = cube_dir(cfg)
    out.mkdir(parents=True, exist_ok=True)
    grid.save(out / "grid.json")
    store = load_store(cfg.processed_dir)
    d_end = data_end(cfg)
    end = max(d_end, pd.Timestamp(extend_to).ceil("h")) if extend_to is not None else d_end
    summary: dict = {"origin": CUBE_ORIGIN.isoformat(), "end": end.isoformat(), "data_end": d_end.isoformat(), "grid": grid.__dict__, "cubes": {}}

    cats = cfg["categories"]
    for cat, cdef in cats.items():
        inc = incidents(store, cdef, event_time="report")
        cube = build_cube(f"{cat}_report", inc["ts_utc"], inc["lon"], inc["lat"], grid, CUBE_ORIGIN, end, meta={"category": cat, "event_time": "report", "data_end": d_end.isoformat()})
        cube.save(out)
        summary["cubes"][cube.name] = cube.meta
        if cat == "property":
            inc_o = incidents(store, cdef, event_time="offense")
            cube_o = build_cube(f"{cat}_offense", inc_o["ts_utc"], inc_o["lon"], inc_o["lat"], grid, CUBE_ORIGIN, end, meta={"category": cat, "event_time": "offense", "data_end": d_end.isoformat()})
            cube_o.save(out)
            summary["cubes"][cube_o.name] = cube_o.meta
        log.info("cube %s: %s", cat, cube.meta["n_located"])

    calls_p = cfg.processed_dir / "spd_calls.parquet"
    if calls_p.exists():
        calls = pd.read_parquet(calls_p, columns=["ts_utc", "lat", "lon"])
        c = build_cube("calls", calls["ts_utc"], calls["lon"], calls["lat"], grid, CUBE_ORIGIN, end, meta={"source": "spd_calls", "data_end": d_end.isoformat()})
        c.save(out)
        summary["cubes"]["calls"] = c.meta
    fire_p = cfg.processed_dir / "fire_911.parquet"
    if fire_p.exists():
        fire = pd.read_parquet(fire_p, columns=["ts_utc", "lat", "lon"])
        f = build_cube("fire", fire["ts_utc"], fire["lon"], fire["lat"], grid, CUBE_ORIGIN, end, meta={"source": "fire_911", "data_end": d_end.isoformat()})
        f.save(out)
        summary["cubes"]["fire"] = f.meta

    # active cells: historical support since the 2019 RMS change (all located offenses)
    allc = Cube.load(out, "all_located_report")
    h0 = allc.hour_of(pd.Timestamp("2019-06-01T00:00:00Z"))
    support = np.asarray(allc.counts[h0:]).sum(0)
    active = support >= MIN_INCIDENTS_ACTIVE
    city_p = cfg.processed_dir / "city_limits.parquet"
    if city_p.exists():
        active &= _in_city_mask(grid, city_p) | (support >= 50)
    np.save(out / "active.npy", active)
    excluded = support[~active].sum() / max(support.sum(), 1)
    summary["active_cells"] = int(active.sum())
    summary["lattice_cells"] = int(grid.n_cells)
    summary["share_incidents_outside_active"] = float(excluded)

    # cell attributes (dominant precinct / neighbourhood) for display & stability analysis
    inc_all = incidents(store, cats["all_located"], event_time="report")
    inc_all = inc_all[inc_all["ts_utc"] >= pd.Timestamp("2023-01-01T00:00:00Z")]
    cell = grid.locate(inc_all["lon"].to_numpy(), inc_all["lat"].to_numpy())
    attrs = pd.DataFrame({"cell": cell, "precinct": inc_all["precinct"].to_numpy(), "neighborhood": inc_all["neighborhood"].to_numpy()})
    attrs = attrs[attrs["cell"] >= 0]
    dom = (
        attrs.groupby("cell")
        .agg(precinct=("precinct", lambda s: s.mode().iat[0] if len(s.mode()) else None), neighborhood=("neighborhood", lambda s: s.mode().iat[0] if len(s.mode()) else None))
        .reset_index()
    )
    dom.to_parquet(out / "cell_attrs.parquet", index=False)
    (out / "build_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    log.info("active cells %d / %d; incidents outside active %.4f", active.sum(), grid.n_cells, excluded)
    return summary


def _in_city_mask(grid: Grid, city_p) -> np.ndarray:
    import geopandas as gpd
    from shapely.geometry import box

    city = gpd.read_parquet(city_p).to_crs(grid.crs).union_all()
    m = np.zeros(grid.n_cells, dtype=bool)
    xs, ys = grid.centroids_xy()
    s = grid.cell_size_m / 2
    for i, (x, y) in enumerate(zip(xs, ys, strict=True)):
        m[i] = city.intersects(box(x - s, y - s, x + s, y + s))
    return m.reshape(grid.n_rows, grid.n_cols)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(json.dumps(build_all(load_config()), indent=2, default=str)[:3000])


if __name__ == "__main__":
    main()
