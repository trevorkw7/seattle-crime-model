"""Unit tests: grid, time handling, metrics, predictive distributions, models."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from seacrime.evaluation.metrics import _capture_one, calibration_bins, per_issue_stats, summarize
from seacrime.grid import Grid, neighbor_sum
from seacrime.models.base import fit_dispersion, nb_interval, nb_logpmf, p_any
from seacrime.timeutil import ambiguous_mask, local_to_utc, nonexistent_mask


# ----------------------------------------------------------------------- grid
def test_grid_locate_roundtrip():
    g = Grid.from_bbox((-122.44, 47.495, -122.235, 47.735), 500)
    lon, lat = g.centroids_lonlat()
    flat = g.locate(lon, lat)
    np.testing.assert_array_equal(flat, np.arange(g.n_cells))
    assert g.locate(np.array([0.0, np.nan]), np.array([0.0, 47.6])).tolist() == [-1, -1]
    assert 1000 < g.n_cells < 3000


def test_cell_size_is_configurable():
    g250 = Grid.from_bbox((-122.44, 47.495, -122.235, 47.735), 250)
    g500 = Grid.from_bbox((-122.44, 47.495, -122.235, 47.735), 500)
    assert abs(g250.n_cells / g500.n_cells - 4) < 0.3


def test_neighbor_sum_matches_bruteforce():
    rng = np.random.default_rng(1)
    a = rng.integers(0, 5, size=(3, 7, 6))
    for r in (1, 2):
        got = neighbor_sum(a, r)
        exp = np.zeros_like(a)
        for i in range(7):
            for j in range(6):
                blk = a[:, max(i - r, 0) : i + r + 1, max(j - r, 0) : j + r + 1].sum((1, 2))
                exp[:, i, j] = blk - a[:, i, j]
        np.testing.assert_array_equal(got, exp)


# ----------------------------------------------------------------------- time
def test_local_to_utc_dst():
    s = pd.Series(["2024-07-01T12:00:00", "2024-01-15T12:00:00", "2024-11-03T01:30:00", "2024-03-10T02:30:00"])
    u = local_to_utc(s)
    assert u.iloc[0] == pd.Timestamp("2024-07-01T19:00:00Z")
    assert u.iloc[1] == pd.Timestamp("2024-01-15T20:00:00Z")
    assert u.iloc[2] == pd.Timestamp("2024-11-03T08:30:00Z")  # first (PDT) occurrence
    assert u.iloc[3] == pd.Timestamp("2024-03-10T10:00:00Z")  # shifted forward to 03:00 PDT
    assert ambiguous_mask(s).tolist() == [False, False, True, False]
    assert nonexistent_mask(s).tolist() == [False, False, False, True]


# -------------------------------------------------------------------- metrics
def test_capture_fractional_ties():
    mu = np.array([1.0, 1.0, 1.0, 1.0])
    y = np.array([4.0, 0.0, 0.0, 0.0])
    # top-1 among four tied cells captures 1/4 of 4 in expectation
    assert _capture_one(mu, y, 1) == pytest.approx(1.0)
    mu2 = np.array([3.0, 1.0, 1.0, 0.0])
    y2 = np.array([1.0, 2.0, 0.0, 5.0])
    assert _capture_one(mu2, y2, 2) == pytest.approx(1.0 + 2.0 * 0.5)


def test_summary_perfect_and_uninformative():
    n_cells, n_t = 100, 50
    rng = np.random.default_rng(0)
    lam = np.r_[np.full(5, 3.0), np.full(95, 0.01)]
    y = rng.poisson(np.tile(lam, n_t)).astype(float)
    t = np.repeat(np.arange(n_t), n_cells)
    good = summarize(per_issue_stats(t, y, np.tile(lam, n_t), p_any(np.tile(lam, n_t), 0), 0.0, n_cells), n_cells)
    flat = np.full_like(y, lam.mean())
    bad = summarize(per_issue_stats(t, y, flat, p_any(flat, 0), 0.0, n_cells), n_cells)
    assert good["capture_5"] > 0.9
    assert bad["capture_5"] == pytest.approx(0.05, abs=0.02)  # random tie-breaking ≈ area share
    assert good["poisson_deviance"] < bad["poisson_deviance"]
    assert good["brier"] < bad["brier"]
    assert good["pai_5"] == pytest.approx(good["capture_5"] / 0.05)


def test_calibration_bins_well_calibrated():
    rng = np.random.default_rng(3)
    p = rng.uniform(0, 0.5, 200_000)
    y = (rng.uniform(size=p.size) < p).astype(float)
    bins, ece = calibration_bins(p, y)
    assert ece < 0.01
    assert sum(b["n"] for b in bins) == p.size


# ----------------------------------------------------------- distributions
def test_dispersion_estimator_recovers_alpha():
    rng = np.random.default_rng(0)
    mu = rng.uniform(0.2, 3.0, 400_000)
    alpha = 0.7
    lam = rng.gamma(1 / alpha, alpha * mu)
    y = rng.poisson(lam)
    assert fit_dispersion(y, mu) == pytest.approx(alpha, rel=0.08)
    assert fit_dispersion(rng.poisson(mu), mu) < 0.02


def test_p_any_and_intervals():
    mu = np.array([0.01, 0.5, 2.0])
    np.testing.assert_allclose(p_any(mu, 0.0), 1 - np.exp(-mu))
    assert np.all(p_any(mu, 1.0) < p_any(mu, 0.0))  # overdispersion -> more zeros
    lo, hi = nb_interval(np.array([5.0]), 0.5)
    assert lo[0] <= 5 <= hi[0]
    ll = nb_logpmf(np.array([0, 1, 2]), np.array([1.0, 1.0, 1.0]), 1e-12)
    np.testing.assert_allclose(np.exp(ll), [np.exp(-1), np.exp(-1), np.exp(-1) / 2], rtol=1e-6)


# --------------------------------------------------------------------- models
def _synthetic(n=60_000, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=n)
    base = np.exp(-2 + 0.3 * rng.normal(size=n))
    mu = base * np.exp(0.8 * x)
    X = pd.DataFrame({"p_c_28d": rng.poisson(5, n).astype(float), "x_signal": x, "hour": rng.integers(0, 24, n), "base_seasonal": base, "base_hotspot": base, "_t": 0, "_cell": 0})
    return X, rng.poisson(mu).astype(float), mu


def test_lgbm_learns_signal_beyond_seasonal():
    from seacrime.models.gbm import LGBMModel

    X, y, mu = _synthetic()
    m = LGBMModel(num_boost_round=200, early_stopping=20)
    m.fit(X.iloc[:40_000], y[:40_000], X.iloc[40_000:50_000], y[40_000:50_000])
    pred = m.predict_mu(X.iloc[50_000:])
    corr = np.corrcoef(np.log(pred), np.log(mu[50_000:]))[0, 1]
    assert corr > 0.8
    d = m.predict_dist(X.iloc[50_000:51_000])
    assert set(d.columns) == {"expected", "p_any", "lo90", "hi90"}
    assert (d["lo90"] <= d["hi90"]).all()


def test_poisson_glm_fits():
    from seacrime.models.glm import PoissonGLM

    X, y, mu = _synthetic(30_000)
    m = PoissonGLM().fit(X.iloc[:20_000], y[:20_000], X.iloc[20_000:], y[20_000:])
    pred = m.predict_mu(X.iloc[20_000:])
    assert abs(pred.sum() / y[20_000:].sum() - 1) < 0.1


def test_lgbm_with_seasonal_offset_starts_from_baseline():
    from seacrime.models.gbm import LGBMModel

    X, y, mu = _synthetic()
    m = LGBMModel(model_id="lgbm_resid", offset_col="base_seasonal", num_boost_round=200, early_stopping=20)
    m.fit(X.iloc[:40_000], y[:40_000], X.iloc[40_000:50_000], y[40_000:50_000])
    pred = m.predict_mu(X.iloc[50_000:])
    assert np.corrcoef(np.log(pred), np.log(mu[50_000:]))[0, 1] > 0.8
