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
    parts = {}
    for c in cols:
        v = X[c].to_numpy(dtype=float)
        if c in CYCLIC:
            parts[f"{c}_sin"] = np.sin(2 * np.pi * v / CYCLIC[c])
            parts[f"{c}_cos"] = np.cos(2 * np.pi * v / CYCLIC[c])
        elif c in ("holiday", "doy_sin", "doy_cos"):
            parts[c] = v
        else:
            parts[c] = np.log1p(np.maximum(np.nan_to_num(v, nan=0.0), 0.0)) if v.min(initial=0) >= 0 else np.nan_to_num(v)
    parts["log_seasonal"] = np.log(np.maximum(X["base_seasonal"].to_numpy(dtype=float), 1e-4))
    D = pd.DataFrame(parts)
    if stats is None:
        stats = {"mean": D.mean().to_dict(), "std": D.std().replace(0, 1).fillna(1).to_dict()}
    D = (D - pd.Series(stats["mean"])) / pd.Series(stats["std"])
    return D.fillna(0.0).to_numpy(dtype=np.float64), stats, list(D.columns)


@dataclass
class PoissonGLM(CountModel):
    model_id: str = "poisson_glm"
    family: str = "glm"
    l2: float = 1e-4
    stats: dict = field(default_factory=dict)
    coef_: np.ndarray | None = None
    intercept_: float = 0.0
    design_cols: list[str] = field(default_factory=list)

    def _select(self, X: pd.DataFrame) -> list[str]:
        return [c for c in X.columns if not c.startswith(("_", "base_")) and c not in SKIP]

    def _fit(self, X, y, X_val, y_val) -> None:
        from sklearn.linear_model import PoissonRegressor

        self.features = self._select(X)
        D, self.stats, self.design_cols = _design(X, self.features, None)
        m = PoissonRegressor(alpha=self.l2, max_iter=300)
        m.fit(D, y)
        self.coef_, self.intercept_ = m.coef_, float(m.intercept_)
        self.params = {"l2": self.l2, "n_features": len(self.design_cols)}

    def predict_mu(self, X: pd.DataFrame) -> np.ndarray:
        D, _, _ = _design(X, self.features, self.stats)
        eta = np.clip(D @ self.coef_ + self.intercept_, -20, 8)
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
        D, self.stats, self.design_cols = _design(X, self.features, None)
        rng = np.random.default_rng(self.seed)
        idx = rng.choice(len(y), size=min(self.max_rows, len(y)), replace=False)
        Dd = sm.add_constant(D[idx], has_constant="add")
        res = sm.NegativeBinomial(y[idx], Dd, loglike_method="nb2").fit(method="lbfgs", maxiter=200, disp=0)
        params = np.asarray(res.params)
        self.intercept_ = float(params[0])
        self.coef_ = params[1:-1]
        self.params = {"n_features": len(self.design_cols), "mle_alpha": float(params[-1]), "rows": int(len(idx))}
