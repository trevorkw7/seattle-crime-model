"""Configuration loading and canonical paths."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "default.yaml"


@dataclass
class Config:
    raw: dict[str, Any]
    root: Path = field(default=REPO_ROOT)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def _path(self, key: str) -> Path:
        p = Path(os.environ.get(f"SEACRIME_{key.upper()}", self.raw[key]))
        return p if p.is_absolute() else self.root / p

    @property
    def data_root(self) -> Path:
        return self._path("data_root")

    @property
    def artifacts_root(self) -> Path:
        return self._path("artifacts_root")

    @property
    def raw_dir(self) -> Path:
        return self.data_root / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_root / "processed"

    @property
    def tz(self) -> str:
        return self.raw["timezone"]

    @property
    def horizons(self) -> list[int]:
        return list(self.raw["forecast"]["horizons_h"])


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    path = Path(path or os.environ.get("SEACRIME_CONFIG", DEFAULT_CONFIG))
    with open(path) as f:
        raw = yaml.safe_load(f)
    if overrides:
        raw = _deep_update(raw, overrides)
    return Config(raw=raw)


def _deep_update(base: dict, upd: dict) -> dict:
    out = dict(base)
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_update(out[k], v)
        else:
            out[k] = v
    return out
