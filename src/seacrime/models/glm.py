"""Poisson and negative-binomial count regressions (log link).

Count-history features are log1p-transformed and standardised; calendar
features are encoded cyclically. The seasonal baseline enters as a feature
(log scale), so the GLM learns how much to trust/adjust it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .base import CountModel

CYCLIC = {"hour": 24, "dow": 7, "month": 12}
SKIP = {"how", "x_km", "y_km"}


def _design(X: pd.DataFrame, cols: list[str], stats: dict | None):
    """Memory-lean standardised design matrix (float32).

    Which columns get ``log1p`` is decided once at fit time (non-negative
    columns in the training data) and stored in ``stats`` so that train and
    test are transformed identically.
    """
    if stats is None:
        log_cols = [c for c in cols if c not in CYCLIC and c not in ("holiday", "doy_sin", "doy_cos") and np.nanmin(X[c].to_numpy(dtype=float), initial=0.0) >= 0]
    else:
        log_cols = stats["log_cols"]
    log_set = set(log_cols)
    names: list[str] = []
    for c in cols:
        names += [f"{c}_sin", f"{c}_cos"] if c in CYCLIC else [c]
    names.append("log_seasonal")
    D = np.empty((len(X), len(names)), dtype=np.float32)
    j = 0
    for c in cols:
        v = X[c].to_numpy(dtype=np.float32)
        if c in CYCLIC:
            D[:, j] = np.sin(2 * np.pi * v / CYCLIC[c])
            D[:, j + 1] = np.cos(2 * np.pi * v / CYCLIC[c])
            j += 2
            continue
        if c in log_set:
            v = np.log1p(np.maximum(np.nan_to_num(v, nan=0.0), 0.0))
        D[:, j] = v
        j += 1
    D[:, j] = np.log(np.maximum(X["base_seasonal"].to_numpy(dtype=np.float32), 1e-4))
    if stats is None:
        mean = np.nanmean(D, axis=0, dtype=np.float64)
        std = np.nanstd(D, axis=0, dtype=np.float64)
        std[~np.isfinite(std) | (std == 0)] = 1.0
        mean[~np.isfinite(mean)] = 0.0
        stats = {"mean": dict(zip(names, mean.tolist(), strict=True)), "std": dict(zip(names, std.tolist(), strict=True)), "log_cols": log_cols}
    mu = np.array([stats["mean"].get(n, 0.0) for n in names], dtype=np.float32)
    sd = np.array([stats["std"].get(n, 1.0) for n in names], dtype=np.float32)
    D -= mu
    D /= sd
    np.nan_to_num(D, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    return D, stats, names


@dataclass
class PoissonGLM(CountModel):
    model_id: str = "poisson_glm"
    family: str = "glm"
    l2: float = 1e-4
    max_rows: int = 1_200_000  # GLMs are fitted on a random subsample of training rows
    stats: dict = field(default_factory=dict)
    coef_: np.ndarray | None = None
    intercept_: float = 0.0
    design_cols: list[str] = field(default_factory=list)

    def _select(self, X: pd.DataFrame) -> list[str]:
        return [c for c in X.columns if not c.startswith(("_", "base_")) and c not in SKIP]

    def _fit(self, X, y, X_val, y_val) -> None:
        from sklearn.linear_model import PoissonRegressor

        self.features = self._select(X)
        idx = np.random.default_rng(0).choice(len(y), size=min(self.max_rows, len(y)), replace=False)
        D, self.stats, self.design_cols = _design(X.iloc[idx], self.features, None)
        m = PoissonRegressor(alpha=self.l2, max_iter=300)
        m.fit(D.astype(np.float64), y[idx])
        self.coef_, self.intercept_ = m.coef_, float(m.intercept_)
        self.params = {"l2": self.l2, "n_features": len(self.design_cols)}

    def predict_mu(self, X: pd.DataFrame) -> np.ndarray:
        D, _, _ = _design(X, self.features, self.stats)
        eta = np.clip(D.astype(np.float64) @ self.coef_ + self.intercept_, -20, 8)
        return np.exp(eta)


@dataclass
class NegBinGLM(PoissonGLM):
    """NB2 regression fitted by maximum likelihood (statsmodels) on a subsample;
    the dispersion is re-estimated on validation data like other models."""

    model_id: str = "negbin_glm"
    max_rows: int = 600_000
    seed: int = 0

    def _fit(self, X, y, X_val, y_val) -> None:
        import statsmodels.api as sm

        self.features = self._select(X)
        rng = np.random.default_rng(self.seed)
        idx = rng.choice(len(y), size=min(self.max_rows, len(y)), replace=False)
        D, self.stats, self.design_cols = _design(X.iloc[idx], self.features, None)
        Dd = sm.add_constant(D.astype(np.float64), has_constant="add")
        res = sm.NegativeBinomial(y[idx], Dd, loglike_method="nb2").fit(method="lbfgs", maxiter=200, disp=0)
        params = np.asarray(res.params)
        self.intercept_ = float(params[0])
        self.coef_ = params[1:-1]
        self.params = {"n_features": len(self.design_cols), "mle_alpha": float(params[-1]), "rows": int(len(idx))}
