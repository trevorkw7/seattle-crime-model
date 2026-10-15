"""Time handling. Internally everything is tz-aware UTC; calendar features use
Seattle local time (America/Los_Angeles).

Seattle open-data timestamps are *floating local* strings without offset. During
the autumn DST fall-back one local hour occurs twice and is ambiguous: we assign
it to the first (daylight) occurrence and count such rows in the validation
report. Spring-forward nonexistent local times (02:00-02:59) are shifted forward.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TZ = "America/Los_Angeles"


def local_to_utc(s: pd.Series, tz: str = TZ) -> pd.Series:
    """Parse floating-local timestamps (strings or naive datetimes) to UTC."""
    t = pd.to_datetime(s, errors="coerce", format="mixed")
    if getattr(t.dt, "tz", None) is not None:
        return t.dt.tz_convert("UTC")
    # ambiguous=True -> treat repeated fall-back hour as DST (first occurrence)
    loc = t.dt.tz_localize(tz, ambiguous=np.ones(len(t), dtype=bool), nonexistent="shift_forward")
    return loc.dt.tz_convert("UTC")


def ambiguous_mask(s: pd.Series, tz: str = TZ) -> pd.Series:
    """True where a floating local timestamp falls in a DST fall-back repeated hour."""
    t = pd.to_datetime(s, errors="coerce", format="mixed")
    a = t.dt.tz_localize(tz, ambiguous=np.ones(len(t), dtype=bool), nonexistent="NaT")
    b = t.dt.tz_localize(tz, ambiguous=np.zeros(len(t), dtype=bool), nonexistent="NaT")
    return (a != b) & a.notna()


def nonexistent_mask(s: pd.Series, tz: str = TZ) -> pd.Series:
    t = pd.to_datetime(s, errors="coerce", format="mixed")
    a = t.dt.tz_localize(tz, ambiguous=np.ones(len(t), dtype=bool), nonexistent="NaT")
    return a.isna() & t.notna()


def hour_index(ts_utc: pd.Series | pd.DatetimeIndex, origin: pd.Timestamp) -> np.ndarray:
    """Integer hours since ``origin`` (UTC, hour-aligned)."""
    ts = pd.DatetimeIndex(ts_utc)
    return ((ts - origin) // pd.Timedelta(hours=1)).to_numpy()


def hour_of_week_local(ts_utc: pd.DatetimeIndex, tz: str = TZ) -> np.ndarray:
    """0..167, Monday 00:00 local = 0."""
    loc = pd.DatetimeIndex(ts_utc).tz_convert(tz)
    return (loc.dayofweek * 24 + loc.hour).to_numpy()


def floor_hour_utc(ts: pd.Timestamp | str) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.floor("h")
