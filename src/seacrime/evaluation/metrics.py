"""Forecast evaluation metrics on (issue time x cell) predictions.

All metrics are computed from per-issue-time aggregates so that block
bootstrap confidence intervals (resampling whole days) are cheap.

Capture@k%: share of observed incidents falling in the k% of (equal-area) cells
with the highest predicted expected count, pooled over issue times. Ties at the
cutoff are handled fractionally (the expectation under random tie-breaking), so
coarse baselines are not flattered or penalised by arbitrary ordering.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.special import gammaln

from ..models.base import nb_logpmf

AREA_FRACS = (0.05, 0.10, 0.20)


def _capture_one(mu: np.ndarray, y: np.ndarray, k: int) -> float:
    """Expected count captured by the top-k cells with fractional ties."""
    if k <= 0:
        return 0.0
    order = np.argsort(-mu, kind="stable")
    mu_s, y_s = mu[order], y[order]
    thr = mu_s[k - 1]
    above = mu_s > thr
    tie = mu_s == thr
    n_above = int(above.sum())
    n_tie = int(tie.sum())
    need = k - n_above
    cap = y_s[above].sum()
    if n_tie > 0 and need > 0:
        cap += y_s[tie].sum() * (need / n_tie)
    return float(cap)


def per_issue_stats(
    t: np.ndarray,
    y: np.ndarray,
    mu: np.ndarray,
    pany: np.ndarray,
    alpha: float,
    n_cells: int,
    fixed_k_cells: tuple[int, ...] = (10, 25, 50),
) -> pd.DataFrame:
    """Aggregate per issue time. Rows must be grouped by issue time with exactly
    ``n_cells`` cells each (the layout produced by FeatureBuilder)."""
    n_t = len(y) // n_cells
    Y = y.reshape(n_t, n_cells)
    M = np.maximum(mu.reshape(n_t, n_cells), 1e-9)
    P = np.clip(pany.reshape(n_t, n_cells), 1e-9, 1 - 1e-9)
    out = {"t": t.reshape(n_t, n_cells)[:, 0], "total": Y.sum(1), "pred_total": M.sum(1)}
    ks = {f"cap_{int(f * 100)}": int(round(f * n_cells)) for f in AREA_FRACS}
    ks.update({f"capk_{k}": k for k in fixed_k_cells})
    for name, k in ks.items():
        out[name] = np.array([_capture_one(M[i], Y[i], k) for i in range(n_t)])
    # Poisson deviance (unit) and log-likelihoods
    with np.errstate(divide="ignore", invalid="ignore"):
        ylogy = np.where(Y > 0, Y * np.log(Y / M), 0.0)
    out["dev"] = (2 * (ylogy - (Y - M))).sum(1)
    out["nll_pois"] = -(Y * np.log(M) - M - gammaln(Y + 1)).sum(1)
    out["nll_nb"] = -nb_logpmf(Y, M, alpha).sum(1)
    out["abs_err"] = np.abs(Y - M).sum(1)
    out["sq_err"] = ((Y - M) ** 2).sum(1)
    B = (Y >= 1).astype(float)
    out["brier"] = ((P - B) ** 2).sum(1)
    out["n_pos"] = B.sum(1)
    return pd.DataFrame(out)


def summarize(stats: pd.DataFrame, n_cells: int) -> dict:
    tot = stats["total"].sum()
    n = len(stats) * n_cells
    r = {
        "n_issue_times": int(len(stats)),
        "n_cell_windows": int(n),
        "observed_total": float(tot),
        "predicted_total": float(stats["pred_total"].sum()),
        "calibration_in_large": float(stats["pred_total"].sum() / max(tot, 1e-9)),
    }
    for f in AREA_FRACS:
        k = int(f * 100)
        cap = stats[f"cap_{k}"].sum() / max(tot, 1e-9)
        r[f"capture_{k}"] = float(cap)
        r[f"pai_{k}"] = float(cap / f)
    for c in [c for c in stats.columns if c.startswith("capk_")]:
        r[c.replace("capk_", "hit_top")] = float(stats[c].sum() / max(tot, 1e-9))
    r["poisson_deviance"] = float(stats["dev"].sum() / n)
    r["nll"] = float(stats["nll_nb"].sum() / n)
    r["nll_poisson"] = float(stats["nll_pois"].sum() / n)
    r["mae"] = float(stats["abs_err"].sum() / n)
    r["rmse"] = float(np.sqrt(stats["sq_err"].sum() / n))
    r["brier"] = float(stats["brier"].sum() / n)
    r["base_rate_any"] = float(stats["n_pos"].sum() / n)
    return r


def bootstrap_ci(stats: pd.DataFrame, n_cells: int, origin: pd.Timestamp, keys=("capture_10", "poisson_deviance", "brier"), B: int = 200, seed: int = 0) -> dict:
    """Day-block bootstrap 90% intervals for selected summary metrics."""
    day = ((origin + pd.to_timedelta(stats["t"], unit="h")).dt.floor("D")).to_numpy()
    days, inv = np.unique(day, return_inverse=True)
    groups = [np.flatnonzero(inv == i) for i in range(len(days))]
    rng = np.random.default_rng(seed)
    draws = {k: [] for k in keys}
    for _ in range(B):
        pick = rng.integers(0, len(days), len(days))
        idx = np.concatenate([groups[i] for i in pick])
        s = summarize(stats.iloc[idx], n_cells)
        for k in keys:
            draws[k].append(s[k])
    return {k: [float(np.percentile(v, 5)), float(np.percentile(v, 95))] for k, v in draws.items()}


def paired_bootstrap_delta(a: pd.DataFrame, b: pd.DataFrame, n_cells: int, origin: pd.Timestamp, key: str = "capture_10", B: int = 200, seed: int = 0) -> dict:
    """Paired day-block bootstrap of metric(a) - metric(b) on identical windows."""
    assert np.array_equal(a["t"].to_numpy(), b["t"].to_numpy())
    day = ((origin + pd.to_timedelta(a["t"], unit="h")).dt.floor("D")).to_numpy()
    days, inv = np.unique(day, return_inverse=True)
    groups = [np.flatnonzero(inv == i) for i in range(len(days))]
    rng = np.random.default_rng(seed)
    d = []
    for _ in range(B):
        pick = rng.integers(0, len(days), len(days))
        idx = np.concatenate([groups[i] for i in pick])
        d.append(summarize(a.iloc[idx], n_cells)[key] - summarize(b.iloc[idx], n_cells)[key])
    point = summarize(a, n_cells)[key] - summarize(b, n_cells)[key]
    return {"delta": float(point), "ci90": [float(np.percentile(d, 5)), float(np.percentile(d, 95))], "p_le_0": float(np.mean(np.array(d) <= 0))}


def calibration_bins(pany: np.ndarray, y: np.ndarray, n_bins: int = 12) -> tuple[list[dict], float]:
    """Reliability diagram on P(N>=1) with quantile bins; returns bins and ECE."""
    b = (y >= 1).astype(float)
    qs = np.unique(np.quantile(pany, np.linspace(0, 1, n_bins + 1)))
    idx = np.clip(np.searchsorted(qs, pany, side="right") - 1, 0, len(qs) - 2)
    bins, ece = [], 0.0
    for i in range(len(qs) - 1):
        m = idx == i
        if not m.any():
            continue
        pm, om = float(pany[m].mean()), float(b[m].mean())
        bins.append({"p_mean": pm, "obs_rate": om, "n": int(m.sum())})
        ece += m.mean() * abs(pm - om)
    return bins, float(ece)
