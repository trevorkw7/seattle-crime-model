"""Common count-model interface and predictive distributions.

Every model predicts the expected count ``mu`` for a (cell, window). The
predictive distribution is negative binomial with a dispersion ``alpha``
estimated on held-out validation data (Var = mu + alpha * mu^2; alpha=0 is
Poisson). From it we derive P(N >= 1) and 90% predictive intervals.
"""

from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats


def fit_dispersion(y: np.ndarray, mu: np.ndarray) -> float:
    """Moment estimator for NB2 dispersion; 0 means Poisson."""
    mu = np.maximum(mu, 1e-9)
    num = np.sum((y - mu) ** 2 - y)
    den = np.sum(mu**2)
    return float(max(num / den, 0.0)) if den > 0 else 0.0


def p_any(mu: np.ndarray, alpha: float) -> np.ndarray:
    mu = np.maximum(mu, 0.0)
    if alpha <= 1e-8:
        return -np.expm1(-mu)
    return 1.0 - np.power(1.0 + alpha * mu, -1.0 / alpha)


def nb_interval(mu: np.ndarray, alpha: float, level: float = 0.9) -> tuple[np.ndarray, np.ndarray]:
    lo_q, hi_q = (1 - level) / 2, 1 - (1 - level) / 2
    mu = np.maximum(mu, 1e-9)
    if alpha <= 1e-8:
        return stats.poisson.ppf(lo_q, mu), stats.poisson.ppf(hi_q, mu)
    n = 1.0 / alpha
    p = n / (n + mu)
    return stats.nbinom.ppf(lo_q, n, p), stats.nbinom.ppf(hi_q, n, p)


def nb_logpmf(y: np.ndarray, mu: np.ndarray, alpha: float) -> np.ndarray:
    mu = np.maximum(mu, 1e-9)
    if alpha <= 1e-8:
        return stats.poisson.logpmf(y, mu)
    n = 1.0 / alpha
    return stats.nbinom.logpmf(y, n, n / (n + mu))


@dataclass
class CountModel:
    model_id: str = "base"
    family: str = "baseline"
    alpha: float = 0.0
    features: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    trained_at: str | None = None
    train_info: dict = field(default_factory=dict)

    # subclasses implement _fit / _predict
    def fit(self, X: pd.DataFrame, y: np.ndarray, X_val: pd.DataFrame | None = None, y_val: np.ndarray | None = None):
        self._fit(X, y, X_val, y_val)
        self.trained_at = datetime.now(UTC).isoformat()
        if X_val is not None and y_val is not None:
            self.alpha = fit_dispersion(y_val, self.predict_mu(X_val))
        return self

    def _fit(self, X, y, X_val, y_val) -> None:  # pragma: no cover - interface
        pass

    def predict_mu(self, X: pd.DataFrame) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError

    def predict_dist(self, X: pd.DataFrame) -> pd.DataFrame:
        mu = self.predict_mu(X)
        lo, hi = nb_interval(mu, self.alpha)
        return pd.DataFrame({"expected": mu, "p_any": p_any(mu, self.alpha), "lo90": lo, "hi90": hi})

    @property
    def version(self) -> str:
        h = hashlib.sha1(json.dumps({"p": self.params, "f": self.features, "t": self.trained_at}, sort_keys=True, default=str).encode())
        stamp = (self.trained_at or "untrained")[:16].replace(":", "").replace("-", "")
        return f"{self.model_id}-{stamp}-{h.hexdigest()[:6]}"

    def save(self, d: Path) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "model.pkl", "wb") as f:
            pickle.dump(self, f)
        meta = {
            "model_id": self.model_id,
            "family": self.family,
            "version": self.version,
            "alpha": self.alpha,
            "features": self.features,
            "params": self.params,
            "trained_at": self.trained_at,
            "train_info": self.train_info,
        }
        (d / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
        return d

    @staticmethod
    def load(d: Path) -> CountModel:
        with open(d / "model.pkl", "rb") as f:
            return pickle.load(f)


@dataclass
class ColumnModel(CountModel):
    """A baseline whose prediction is a precomputed feature column
    (``base_seasonal`` or ``base_hotspot``), optionally rescaled."""

    column: str = "base_seasonal"
    scale: float = 1.0

    def _fit(self, X, y, X_val, y_val) -> None:
        self.features = [self.column]
        self.params = {"column": self.column}

    def predict_mu(self, X: pd.DataFrame) -> np.ndarray:
        return np.maximum(X[self.column].to_numpy(dtype=float) * self.scale, 1e-6)
