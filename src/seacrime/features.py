"""Leakage-safe feature construction for (issue time t, horizon h, cell).

Availability rules (enforced here and tested in tests/test_leakage.py):

* Crime features use report-time cubes and only hours ``< t - L_crime``
  (SPD publishes finalized reports once a day with multi-day delay).
* SPD CAD call features use only hours ``< t - L_calls``.
* Fire 911 features use only hours ``< t - L_fire`` (real-time feed).
* Same-hour-of-week history uses past weeks whose window ends before the
  crime availability cutoff.
* Calendar, static geography, scheduled events (schedules are published weeks
  in advance) and weather are known/forecastable at t. Historical backtests
  use observed weather as a stand-in for a weather forecast — flagged
  ``weather_mode='observed_proxy'`` — or past-only weather (``'past_only'``).
* Features never depend on the target cube when the target uses offense time:
  features are always built from report-time (availability-consistent) cubes.

Two classic baselines are produced as feature columns as well:
``base_seasonal`` (empirical-Bayes per-cell hour-of-week rate) and
``base_hotspot`` (Gaussian KDE of trailing-year counts).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .dataset import Cube, window_sum
from .grid import Grid, gaussian_smooth, neighbor_sum
from .timeutil import TZ

HOURS_YEAR = 8760


@dataclass
class ContextData:
    """Optional contextual sources aligned to the cube's hourly index."""

    weather: pd.DataFrame | None = None  # index: hour idx; cols temp_c, precip_mm, wind_ms
    events: pd.DataFrame | None = None  # cols start_h, end_h, lat, lon, category, attendance
    transit_how: np.ndarray | None = None  # (168, R, C) scheduled departures per hour-of-week
    static: dict[str, np.ndarray] = field(default_factory=dict)  # name -> (R, C)
    holidays: set = field(default_factory=set)  # local dates


@dataclass
class FeatureConfig:
    crime_lag_h: int = 72
    calls_lag_h: int = 96
    fire_lag_h: int = 1
    seasonal_kappa: float = 26.0
    hotspot_sigma: float = 1.0
    hotspot_window_h: int = HOURS_YEAR
    weather_mode: str = "observed_proxy"  # observed_proxy | past_only | none
    groups: tuple[str, ...] = ("crime", "neighbors", "seasonal", "other_cats", "calls", "fire", "calendar", "static", "weather", "events", "transit")


class FeatureBuilder:
    def __init__(
        self,
        grid: Grid,
        active: np.ndarray,  # (R, C) bool
        target: Cube,  # cube defining y (report or offense time)
        crime: dict[str, Cube],  # report-time cubes by category (must include target category)
        target_cat: str,
        calls: Cube | None = None,
        fire: Cube | None = None,
        context: ContextData | None = None,
        fcfg: FeatureConfig | None = None,
    ):
        self.grid = grid
        self.active = active.astype(bool)
        self.active_flat = np.flatnonzero(self.active.ravel())
        self.target = target
        self.crime = crime
        self.cat = target_cat
        self.calls = calls
        self.fire = fire
        self.ctx = context or ContextData()
        self.f = fcfg or FeatureConfig()
        self.origin = target.origin
        for c in list(crime.values()) + [x for x in (calls, fire) if x is not None]:
            if c.origin != self.origin:
                raise ValueError(f"cube {c.name} origin {c.origin} != {self.origin}")
        self.S = {k: v.prefix() for k, v in crime.items()}
        self.S_target = target.prefix() if target is not crime.get(target_cat) else self.S[target_cat]
        self.S_calls = calls.prefix() if calls is not None else None
        self.S_fire = fire.prefix() if fire is not None else None
        self.T = target.n_hours
        xs, ys = grid.centroids_xy()
        self._x = xs.reshape(grid.n_rows, grid.n_cols)
        self._y = ys.reshape(grid.n_rows, grid.n_cols)

    # ------------------------------------------------------------------ utils
    def _flat(self, a: np.ndarray) -> np.ndarray:
        """(n, R, C) -> (n * n_active,) restricted to active cells."""
        n = a.shape[0]
        return a.reshape(n, -1)[:, self.active_flat].ravel()

    def _bcast(self, v: np.ndarray) -> np.ndarray:
        """(n,) -> (n * n_active,)."""
        return np.repeat(v, len(self.active_flat))

    def _city(self, a: np.ndarray) -> np.ndarray:
        return a.reshape(a.shape[0], -1)[:, self.active_flat].sum(axis=1)

    def max_issue_hour(self) -> int:
        return self.T  # features need only past data; targets need t + h <= T

    def target_values(self, t: np.ndarray, h: int) -> np.ndarray:
        y = window_sum(self.S_target, t, t + h)
        return self._flat(y).astype(np.float32)

    # --------------------------------------------------------------- features
    def build(self, t: np.ndarray, h: int, with_target: bool = True) -> tuple[pd.DataFrame, np.ndarray | None]:
        t = np.asarray(t, dtype=np.int64)
        g = set(self.f.groups)
        L = self.f.crime_lag_h
        a = t - L  # crime availability cutoff (exclusive) per issue time
        tt = t
        aa = a
        S = self.S[self.cat]
        cols: dict[str, np.ndarray] = {}
        n_act = len(self.active_flat)

        def win(Sx, w, cutoff=aa):
            return window_sum(Sx, cutoff - w, cutoff)

        # --- core crime history of the target category (always on: baselines need it)
        c365 = win(S, HOURS_YEAR)
        c28 = win(S, 672)
        if "crime" in g:
            cols["p_c_1d"] = self._flat(win(S, 24))
            cols["p_c_7d"] = self._flat(win(S, 168))
            cols["p_c_28d"] = self._flat(c28)
            cols["p_c_91d"] = self._flat(win(S, 2184))
            cols["p_c_365d"] = self._flat(c365)
            cols["p_trend_28_365"] = self._flat((c28 + 0.5) / (c365 * 28 / 365 + 0.5))
        if "neighbors" in g:
            w7 = win(S, 168)
            cols["p_nb1_7d"] = self._flat(neighbor_sum(w7, 1))
            cols["p_nb1_28d"] = self._flat(neighbor_sum(c28, 1))
            cols["p_nb1_365d"] = self._flat(neighbor_sum(c365, 1))
            nb2 = neighbor_sum(c365, 2, include_self=True) - neighbor_sum(c365, 1, include_self=True)
            cols["p_nb2_365d"] = self._flat(nb2)

        # --- same-hour-of-week history (seasonal), past weeks fully available
        how_sum = np.zeros((len(t), self.grid.n_rows, self.grid.n_cols), dtype=np.float64)
        how4 = np.zeros_like(how_sum)
        n_weeks = 0
        for k in range(1, 53):
            s0 = tt - 168 * k
            e0 = s0 + h
            if np.any(e0 > aa):
                continue  # would use unavailable data (only for very long horizons)
            w = window_sum(S, s0, e0)
            how_sum += w
            if k <= 4:
                how4 += w
            n_weeks += 1
        how52 = how_sum / max(n_weeks, 1)
        city_how52 = self._city(how52)
        city365 = self._city(c365).astype(float)
        city28 = self._city(c28).astype(float)
        # separable expectation: cell share x citywide same-window rate
        share = (c365 + 0.01) / (city365[:, None, None] + 0.01 * n_act)
        sep = share * city_how52[:, None, None]
        kappa = self.f.seasonal_kappa
        seasonal = (n_weeks * how52 + kappa * sep) / (n_weeks + kappa)
        cols["base_seasonal"] = self._flat(seasonal)
        # hotspot KDE: smoothed trailing-window spatial density x recent citywide rate
        hw = c365 if self.f.hotspot_window_h == HOURS_YEAR else win(S, self.f.hotspot_window_h)
        dens = gaussian_smooth(hw.astype(float), self.f.hotspot_sigma) * self.active[None]
        dens = dens / np.maximum(dens.reshape(len(t), -1).sum(1), 1e-9)[:, None, None]
        cols["base_hotspot"] = self._flat(dens * (city28 / 672.0 * h)[:, None, None])
        if "seasonal" in g:
            cols["p_how_4w"] = self._flat(how4 / 4.0)
            cols["p_how_52w"] = self._flat(how52)
            cols["p_how_52w_nb1"] = self._flat(neighbor_sum(how52, 1))
            cols["city_how_52w"] = self._bcast(city_how52)
            cols["city_p_7d"] = self._bcast(self._city(win(S, 168)).astype(float))
            cols["city_p_28d"] = self._bcast(city28)
            cols["city_trend"] = self._bcast((city28 * 365 / 28 + 1) / (city365 + 1))

        # --- other crime categories (report time, same availability)
        if "other_cats" in g:
            for name, Sx in self.S.items():
                if name == self.cat:
                    continue
                cols[f"{name}_c_28d"] = self._flat(win(Sx, 672))
                cols[f"{name}_c_365d"] = self._flat(win(Sx, HOURS_YEAR))
                cols[f"{name}_nb1_365d"] = self._flat(neighbor_sum(win(Sx, HOURS_YEAR), 1))

        # --- SPD CAD calls for service (delayed publication)
        if "calls" in g and self.S_calls is not None:
            ca = tt - self.f.calls_lag_h
            c7 = win(self.S_calls, 168, ca)
            c28c = win(self.S_calls, 672, ca)
            cols["calls_c_7d"] = self._flat(c7)
            cols["calls_c_28d"] = self._flat(c28c)
            cols["calls_nb1_28d"] = self._flat(neighbor_sum(c28c, 1))
            cols["city_calls_7d"] = self._bcast(self._city(c7).astype(float))

        # --- Fire/EMS 911 (near-real-time)
        if "fire" in g and self.S_fire is not None:
            fa = tt - self.f.fire_lag_h
            f3 = win(self.S_fire, 3, fa)
            f24 = win(self.S_fire, 24, fa)
            cols["fire_c_3h"] = self._flat(f3)
            cols["fire_c_24h"] = self._flat(f24)
            cols["fire_c_7d"] = self._flat(win(self.S_fire, 168, fa))
            cols["fire_nb1_24h"] = self._flat(neighbor_sum(f24, 1))
            cols["city_fire_3h"] = self._bcast(self._city(f3).astype(float))
            cols["city_fire_24h"] = self._bcast(self._city(f24).astype(float))

        # --- calendar (local time of window start)
        ts = self.origin + pd.to_timedelta(t, unit="h")
        loc = pd.DatetimeIndex(ts).tz_convert(TZ)
        if "calendar" in g:
            cols["hour"] = self._bcast(loc.hour.to_numpy())
            cols["dow"] = self._bcast(loc.dayofweek.to_numpy())
            cols["how"] = self._bcast((loc.dayofweek * 24 + loc.hour).to_numpy())
            cols["month"] = self._bcast(loc.month.to_numpy())
            doy = loc.dayofyear.to_numpy()
            cols["doy_sin"] = self._bcast(np.sin(2 * np.pi * doy / 365.25))
            cols["doy_cos"] = self._bcast(np.cos(2 * np.pi * doy / 365.25))
            hol = np.array([d in self.ctx.holidays for d in loc.date])
            cols["holiday"] = self._bcast(hol.astype(np.int8))

        # --- static geography
        if "static" in g:
            cols["x_km"] = self._flat(np.broadcast_to(self._x / 1000, (len(t),) + self._x.shape))
            cols["y_km"] = self._flat(np.broadcast_to(self._y / 1000, (len(t),) + self._y.shape))
            for name, arr in self.ctx.static.items():
                cols[f"st_{name}"] = self._flat(np.broadcast_to(arr, (len(t),) + arr.shape))

        # --- transit scheduled service in the window (hour-of-week profile)
        if "transit" in g and self.ctx.transit_how is not None:
            how0 = (loc.dayofweek * 24 + loc.hour).to_numpy()
            acc = np.zeros((len(t),) + self.ctx.transit_how.shape[1:], dtype=np.float32)
            for j in range(h):
                acc += self.ctx.transit_how[(how0 + j) % 168]
            cols["transit_deps"] = self._flat(acc)

        # --- weather
        if "weather" in g and self.ctx.weather is not None and self.f.weather_mode != "none":
            wx = self.ctx.weather
            if self.f.weather_mode == "observed_proxy":
                # window aggregates of observed weather (stand-in for a forecast)
                tmp = _window_agg(wx, t, h)
                for k, v in tmp.items():
                    cols[f"wx_{k}"] = self._bcast(v)
            past = _window_agg(wx, t - 24, 24, suffix="_prev24h")
            for k, v in past.items():
                cols[f"wx_{k}"] = self._bcast(v)

        # --- scheduled events (sports etc.)
        if "events" in g and self.ctx.events is not None and len(self.ctx.events):
            ev_cols = self._event_features(t, h)
            cols.update(ev_cols)

        X = pd.DataFrame({k: np.asarray(v, dtype=np.float32) for k, v in cols.items()})
        X["_t"] = self._bcast(t).astype(np.int64)
        X["_cell"] = np.tile(self.active_flat, len(t))
        y = self.target_values(t, h) if with_target else None
        return X, y

    def _event_features(self, t: np.ndarray, h: int) -> dict[str, np.ndarray]:
        ev = self.ctx.events
        xs, ys = self._x.ravel()[self.active_flat], self._y.ravel()[self.active_flat]
        ex, ey = self.grid.project(ev["lon"].to_numpy(), ev["lat"].to_numpy())
        # distance (km) from each active cell to each event venue
        d = np.sqrt((xs[:, None] - ex[None]) ** 2 + (ys[:, None] - ey[None]) ** 2) / 1000.0
        s = ev["start_h"].to_numpy() - 2  # arrival window
        e = ev["end_h"].to_numpy() + 2  # dispersal window
        att = np.log1p(ev["attendance"].fillna(20000).to_numpy(dtype=float))
        n = len(t)
        near1 = np.zeros((n, len(xs)), np.float32)
        near3 = np.zeros((n, len(xs)), np.float32)
        att3 = np.zeros((n, len(xs)), np.float32)
        city = np.zeros(n, np.float32)
        for i, ti in enumerate(t):
            act = np.flatnonzero((s < ti + h) & (e > ti))
            if len(act) == 0:
                continue
            city[i] = len(act)
            dd = d[:, act]
            near1[i] = (dd <= 1.0).sum(1)
            near3[i] = (dd <= 3.0).sum(1)
            att3[i] = ((dd <= 3.0) * att[act][None]).sum(1)
        return {
            "ev_near_1km": near1.ravel(),
            "ev_near_3km": near3.ravel(),
            "ev_logatt_3km": att3.ravel(),
            "ev_city": np.repeat(city, len(xs)),
        }


def _window_agg(wx: pd.DataFrame, t: np.ndarray, h: int, suffix: str = "") -> dict[str, np.ndarray]:
    """Mean temperature/wind and total precipitation over [t, t+h) from an hourly
    frame indexed by hour index. Missing hours are ignored (NaN if all missing)."""
    out = {}
    idx = wx.index.to_numpy()
    lo, hi = idx.min(), idx.max()
    for col, how in (("temp_c", "mean"), ("precip_mm", "sum"), ("wind_ms", "mean")):
        if col not in wx:
            continue
        full = np.full(hi - lo + 1, np.nan)
        full[idx - lo] = wx[col].to_numpy(dtype=float)
        valid = np.isfinite(full)
        cs = np.concatenate([[0.0], np.cumsum(np.where(valid, full, 0.0))])
        cn = np.concatenate([[0], np.cumsum(valid)])
        a = np.clip(t - lo, 0, len(full))
        b = np.clip(t + h - lo, 0, len(full))
        tot = cs[b] - cs[a]
        n = cn[b] - cn[a]
        with np.errstate(invalid="ignore", divide="ignore"):
            v = tot / n if how == "mean" else np.where(n > 0, tot * h / np.maximum(n, 1), np.nan)
        out[col + suffix] = v
    return out


def feature_columns(X: pd.DataFrame) -> list[str]:
    return [c for c in X.columns if not c.startswith("_") and not c.startswith("base_")]
