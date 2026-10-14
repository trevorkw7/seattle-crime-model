"""Gradient-boosted count models (LightGBM, CatBoost) with Poisson loss."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..features import feature_columns
from .base import CountModel

CATEGORICAL = ["hour", "dow", "how", "month"]


@dataclass
class LGBMModel(CountModel):
    model_id: str = "lgbm"
    family: str = "gbm"
    booster: object = None
    lgb_params: dict = field(
        default_factory=lambda: {
            "objective": "poisson",
            "learning_rate": 0.05,
            "num_leaves": 63,
            "min_data_in_leaf": 200,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "lambda_l2": 1.0,
            "verbose": -1,
            "num_threads": 4,
            "seed": 0,
        }
    )
    num_boost_round: int = 2000
    early_stopping: int = 100
    exclude: tuple[str, ...] = ()

    def _fit(self, X, y, X_val, y_val) -> None:
        import lightgbm as lgb

        self.features = [c for c in feature_columns(X) if c not in self.exclude] + ["base_seasonal", "base_hotspot"]
        dtr = lgb.Dataset(X[self.features], label=y, free_raw_data=True)
        valid = []
        callbacks = []
        if X_val is not None:
            valid = [lgb.Dataset(X_val[self.features], label=y_val, reference=dtr)]
            callbacks = [lgb.early_stopping(self.early_stopping, verbose=False), lgb.log_evaluation(0)]
        self.booster = lgb.train(self.lgb_params, dtr, num_boost_round=self.num_boost_round, valid_sets=valid, callbacks=callbacks)
        self.params = {**self.lgb_params, "best_iteration": int(self.booster.best_iteration or self.num_boost_round)}

    def predict_mu(self, X: pd.DataFrame) -> np.ndarray:
        it = self.booster.best_iteration or None
        return np.maximum(self.booster.predict(X[self.features], num_iteration=it), 1e-6)

    def feature_importance(self) -> pd.Series:
        return pd.Series(self.booster.feature_importance("gain"), index=self.features).sort_values(ascending=False)


@dataclass
class CatBoostModel(CountModel):
    model_id: str = "catboost"
    family: str = "gbm"
    cb: object = None
    iterations: int = 800
    learning_rate: float = 0.1
    depth: int = 7
    exclude: tuple[str, ...] = ()

    def _fit(self, X, y, X_val, y_val) -> None:
        from catboost import CatBoostRegressor

        self.features = [c for c in feature_columns(X) if c not in self.exclude] + ["base_seasonal", "base_hotspot"]
        self.cb = CatBoostRegressor(
            loss_function="Poisson",
            iterations=self.iterations,
            learning_rate=self.learning_rate,
            depth=self.depth,
            thread_count=4,
            random_seed=0,
            verbose=0,
            od_type="Iter" if X_val is not None else None,
            od_wait=100 if X_val is not None else None,
        )
        eval_set = (X_val[self.features], y_val) if X_val is not None else None
        self.cb.fit(X[self.features], y, eval_set=eval_set, use_best_model=X_val is not None)
        self.params = {"iterations": self.iterations, "lr": self.learning_rate, "depth": self.depth, "best_iteration": int(self.cb.get_best_iteration() or self.iterations)}

    def predict_mu(self, X: pd.DataFrame) -> np.ndarray:
        # CatBoost Poisson predicts log-mean with prediction_type="RawFormulaVal"
        return np.maximum(np.exp(self.cb.predict(X[self.features], prediction_type="RawFormulaVal")), 1e-6)
