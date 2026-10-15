"""Space-time count cubes: hourly counts on the full grid lattice.

A cube has shape (T, n_rows, n_cols) where hour index 0 is ``origin`` (UTC).
Prefix sums along time give O(1) window counts, which makes leakage-safe
feature construction for every issue time fully vectorised.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .grid import Grid


@dataclass
class Cube:
    name: str
    origin: pd.Timestamp  # tz-aware UTC, hour aligned
    counts: np.ndarray  # (T, R, C) int16/int32
    n_unlocated: np.ndarray  # (T,) events in the window that could not be gridded
    meta: dict
    _S: np.ndarray | None = field(default=None, repr=False, compare=False)

    @property
    def n_hours(self) -> int:
        return self.counts.shape[0]

    def time_index(self) -> pd.DatetimeIndex:
        return pd.date_range(self.origin, periods=self.n_hours, freq="h", tz="UTC")

    def hour_of(self, ts: pd.Timestamp) -> int:
        return int((pd.Timestamp(ts).tz_convert("UTC").floor("h") - self.origin) // pd.Timedelta(hours=1))

    def prefix(self) -> np.ndarray:
        """S with S[i] = sum of counts[:i]; shape (T+1, R, C), int32 (cached)."""
        if self._S is None:
            s = np.zeros((self.n_hours + 1,) + self.counts.shape[1:], dtype=np.int32)
            np.cumsum(self.counts, axis=0, out=s[1:])
            self._S = s
        return self._S

    def save(self, d: Path) -> None:
        """Atomic write (temp file + rename) so processes that memory-map the
        previous version keep a consistent view."""
        d.mkdir(parents=True, exist_ok=True)
        for suffix, arr in ((".npy", self.counts), (".unlocated.npy", self.n_unlocated)):
            tmp = d / f".{self.name}{suffix}.tmp"
            with open(tmp, "wb") as f:
                np.save(f, arr)
            os.replace(tmp, d / f"{self.name}{suffix}")
        tmp = d / f".{self.name}.json.tmp"
        tmp.write_text(json.dumps({"origin": self.origin.isoformat(), **self.meta}, indent=2, default=str))
        os.replace(tmp, d / f"{self.name}.json")

    @classmethod
    def load(cls, d: Path, name: str, mmap: bool = False) -> Cube:
        meta = json.loads((d / f"{name}.json").read_text())
        origin = pd.Timestamp(meta.pop("origin"))
        counts = np.load(d / f"{name}.npy", mmap_mode="r" if mmap else None)
        unl = np.load(d / f"{name}.unlocated.npy")
        return cls(name=name, origin=origin, counts=counts, n_unlocated=unl, meta=meta)


def build_cube(
    name: str,
    ts_utc: pd.Series,
    lon: pd.Series,
    lat: pd.Series,
    grid: Grid,
    origin: pd.Timestamp,
    end: pd.Timestamp,
    dtype=np.int16,
    meta: dict | None = None,
) -> Cube:
    origin = pd.Timestamp(origin).tz_convert("UTC").floor("h")
    end = pd.Timestamp(end).tz_convert("UTC").ceil("h")
    T = int((end - origin) // pd.Timedelta(hours=1))
    ts = pd.DatetimeIndex(pd.to_datetime(ts_utc, utc=True))
    h = ((ts - origin) // pd.Timedelta(hours=1)).to_numpy()
    in_t = (h >= 0) & (h < T) & ~pd.isna(ts)
    cell = grid.locate(np.asarray(lon, float), np.asarray(lat, float))
    located = in_t & (cell >= 0)
    flat = h[located].astype(np.int64) * grid.n_cells + cell[located]
    counts = np.bincount(flat, minlength=T * grid.n_cells)
    if counts.max(initial=0) > np.iinfo(dtype).max:
        dtype = np.int32
    counts = counts.astype(dtype).reshape(T, grid.n_rows, grid.n_cols)
    unl = np.bincount(h[in_t & (cell < 0)].astype(np.int64), minlength=T).astype(np.int32)
    m = {
        "n_events_in_period": int(in_t.sum()),
        "n_located": int(located.sum()),
        "n_unlocated": int((in_t & (cell < 0)).sum()),
        "end": end.isoformat(),
        "grid": grid.__dict__,
    }
    m.update(meta or {})
    return Cube(name=name, origin=origin, counts=counts, n_unlocated=unl, meta=m)


def window_sum(S: np.ndarray, start: np.ndarray, end: np.ndarray) -> np.ndarray:
    """Counts in [start, end) for arrays of hour indices (clipped to the cube)."""
    T = S.shape[0] - 1
    a = np.clip(start, 0, T)
    b = np.clip(end, 0, T)
    return S[b] - S[a]
