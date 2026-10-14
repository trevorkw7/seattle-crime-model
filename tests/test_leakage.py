"""Future-data leakage and chronology tests on synthetic cubes."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from seacrime.dataset import Cube
from seacrime.evaluation.backtest import check_chronology, issue_times
from seacrime.features import ContextData, FeatureBuilder, FeatureConfig
from seacrime.grid import Grid

ORIGIN = pd.Timestamp("2020-01-01T00:00:00Z")


def _cube(name, counts):
    return Cube(name=name, origin=ORIGIN, counts=counts, n_unlocated=np.zeros(counts.shape[0], np.int32), meta={})


@pytest.fixture()
def env():
    rng = np.random.default_rng(0)
    grid = Grid(cell_size_m=500, x0=0, y0=0, n_rows=6, n_cols=5)
    T = 24 * 7 * 60  # 60 weeks
    prop = rng.poisson(0.05, size=(T, 6, 5)).astype(np.int16)
    viol = rng.poisson(0.01, size=(T, 6, 5)).astype(np.int16)
    calls = rng.poisson(0.2, size=(T, 6, 5)).astype(np.int16)
    fire = rng.poisson(0.1, size=(T, 6, 5)).astype(np.int16)
    active = np.ones((6, 5), bool)
    active[0, 0] = False
    return grid, active, prop, viol, calls, fire


def _builder(env, prop, fire=None, calls=None, fcfg=None):
    grid, active, _, viol, calls0, fire0 = env
    crime = {"property": _cube("property_report", prop), "violent": _cube("violent_report", viol)}
    return FeatureBuilder(
        grid,
        active,
        crime["property"],
        crime,
        "property",
        calls=_cube("calls", calls if calls is not None else calls0),
        fire=_cube("fire", fire if fire is not None else fire0),
        context=ContextData(),
        fcfg=fcfg or FeatureConfig(groups=("crime", "neighbors", "seasonal", "other_cats", "calls", "fire", "calendar", "static")),
    )


@pytest.mark.parametrize("h", [1, 3, 6, 24])
def test_features_ignore_unavailable_crime_data(env, h):
    """Perturbing crime counts at or after t - lag must not change any feature."""
    grid, active, prop, viol, calls, fire = env
    fcfg = FeatureConfig(crime_lag_h=72, calls_lag_h=96, fire_lag_h=1)
    t = np.array([24 * 7 * 55 + 5, 24 * 7 * 56 + 13])
    X1, y1 = _builder(env, prop, fcfg=fcfg).build(t, h)
    prop2 = prop.copy()
    cut = t.max() - fcfg.crime_lag_h  # unavailable to every issue time in t
    prop2[cut:] += 7  # massive change in not-yet-available data (incl. the target window)
    X2, y2 = _builder(env, prop2, fcfg=fcfg).build(t, h)
    feat = [c for c in X1.columns if not c.startswith("_")]
    pd.testing.assert_frame_equal(X1[feat], X2[feat])
    assert not np.array_equal(y1, y2)  # sanity: the target did change


def test_fire_and_calls_respect_their_lags(env):
    grid, active, prop, viol, calls, fire = env
    fcfg = FeatureConfig(crime_lag_h=72, calls_lag_h=96, fire_lag_h=1)
    t = np.array([24 * 7 * 55])
    X1, _ = _builder(env, prop, fcfg=fcfg).build(t, 6)
    fire2 = fire.copy()
    fire2[t[0] - 1 :] += 5
    calls2 = calls.copy()
    calls2[t[0] - 96 :] += 5
    X2, _ = _builder(env, prop, fire=fire2, calls=calls2, fcfg=fcfg).build(t, 6)
    feat = [c for c in X1.columns if not c.startswith("_")]
    pd.testing.assert_frame_equal(X1[feat], X2[feat])
    # but data just before the cutoff *is* used
    fire3 = fire.copy()
    fire3[t[0] - 2] += 5
    X3, _ = _builder(env, prop, fire=fire3, fcfg=fcfg).build(t, 6)
    assert not X1["fire_c_3h"].equals(X3["fire_c_3h"])


def test_target_alignment(env):
    grid, active, prop, *_ = env
    fb = _builder(env, prop)
    t = np.array([5000])
    _, y = fb.build(t, 6)
    expect = prop[5000:5006].sum(0).ravel()[np.flatnonzero(active.ravel())]
    np.testing.assert_array_equal(y, expect)


def test_offense_time_target_does_not_feed_features(env):
    """With an offense-time target the features still come from report-time cubes."""
    grid, active, prop, viol, calls, fire = env
    off = np.roll(prop, -10, axis=0)
    crime = {"property": _cube("property_report", prop), "violent": _cube("violent_report", viol)}
    fb = FeatureBuilder(grid, active, _cube("property_offense", off), crime, "property", context=ContextData(),
                        fcfg=FeatureConfig(groups=("crime",)))
    off2 = off.copy()
    off2[:] += 3
    fb2 = FeatureBuilder(grid, active, _cube("property_offense", off2), crime, "property", context=ContextData(),
                         fcfg=FeatureConfig(groups=("crime",)))
    t = np.array([6000])
    X1, _ = fb.build(t, 24)
    X2, _ = fb2.build(t, 24)
    feat = [c for c in X1.columns if not c.startswith("_")]
    pd.testing.assert_frame_equal(X1[feat], X2[feat])


def test_issue_times_and_chronology():
    o = ORIGIN
    tr = issue_times(o, o, o + pd.Timedelta(days=10), 24)
    assert tr.max() + 24 <= 240
    va = issue_times(o, o + pd.Timedelta(days=10), o + pd.Timedelta(days=17), 24)
    te = issue_times(o, o + pd.Timedelta(days=20), o + pd.Timedelta(days=30), 24)
    check_chronology(tr, va, te, 24, 72)
    with pytest.raises(AssertionError):
        check_chronology(tr, va, issue_times(o, o + pd.Timedelta(days=17), o + pd.Timedelta(days=30), 24), 24, 72)


def test_events_respect_schedule_publication_and_exante_attendance(env, tmp_path):
    """An event is only visible once its schedule is known, and post-event
    (announced) attendance is never used."""
    from seacrime.context import load_events

    grid, active, prop, *_ = env
    origin = ORIGIN
    lon, lat = 0.0, 0.0
    ev = pd.DataFrame(
        {
            "event_id": ["a", "b"],
            "category": ["mlb", "mlb"],
            "lat": [47.6, 47.6],
            "lon": [-122.33, -122.33],
            "start_utc": [origin + pd.Timedelta(hours=9000), origin + pd.Timedelta(hours=100)],
            "end_utc": [origin + pd.Timedelta(hours=9003), origin + pd.Timedelta(hours=103)],
            "schedule_known_at_utc": [origin + pd.Timedelta(hours=8990), origin],
            "attendance": [99999.0, 30000.0],
            "attendance_kind": ["announced_post_event", "announced_post_event"],
            "status": ["scheduled", "final"],
        }
    )
    ev.to_parquet(tmp_path / "events.parquet")
    e = load_events(tmp_path, origin, typical_before="2020-02-01")
    assert e.loc[0, "attendance"] == 30000.0  # typical from earlier games, not the announced 99999
    del lon, lat
    g2 = Grid.from_bbox((-122.44, 47.495, -122.235, 47.735), 500)
    act = np.zeros((g2.n_rows, g2.n_cols), bool)
    act[g2.locate(np.array([-122.33]), np.array([47.6]))[0] // g2.n_cols, g2.locate(np.array([-122.33]), np.array([47.6]))[0] % g2.n_cols] = True
    T = 9100
    z = np.zeros((T, g2.n_rows, g2.n_cols), np.int16)
    cube = _cube("property_report", z)
    fb = FeatureBuilder(g2, act, cube, {"property": cube}, "property", context=ContextData(events=e), fcfg=FeatureConfig(groups=("events",)))
    X_before, _ = fb.build(np.array([8985]), 24)  # schedule not yet public
    X_after, _ = fb.build(np.array([8995]), 24)
    assert X_before["ev_major_near_1km"].sum() == 0
    assert X_after["ev_major_near_1km"].sum() == 1


def test_deep_model_checkpoint_excludes_runtime_arrays(env):
    """Saved neural models must contain weights/config only (not cube arrays)."""
    import pickle

    torch = pytest.importorskip("torch")
    del torch
    from seacrime.models.deep.wrapper import DeepGridModel

    grid, active, prop, *_ = env
    fb = _builder(env, prop)
    t = np.arange(24 * 7 * 54, 24 * 7 * 54 + 40, 3)
    X, y = fb.build(t, 6)
    m = DeepGridModel(arch="tcn", epochs=1, samples_per_epoch=8, batch_size=4, hist_days=7).attach(fb, 6)
    m.fit(X, y, X, y)
    blob = pickle.dumps(m)
    assert len(blob) < 2_000_000
    m2 = pickle.loads(blob).attach(fb, 6)
    np.testing.assert_allclose(m2.predict_mu(X), m.predict_mu(X), rtol=1e-5)
