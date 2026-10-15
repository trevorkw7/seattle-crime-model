"""Configurable square spatial grid over Seattle (default 500 m, UTM 10N).

Cells are addressed by (row, col) with row 0 at the south edge and col 0 at the
west edge; string ids are ``"r{row}_c{col}"``. The full rectangular lattice is
used for neighbourhood computations; an *active* mask selects the cells that are
inside the city / have historical support and are actually forecast.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pyproj import Transformer


@dataclass(frozen=True)
class Grid:
    cell_size_m: float
    x0: float  # projected easting of the lattice's west edge
    y0: float  # projected northing of the lattice's south edge
    n_rows: int
    n_cols: int
    crs: str = "EPSG:32610"

    # ------------------------------------------------------------------ build
    @classmethod
    def from_bbox(cls, bbox: tuple[float, float, float, float], cell_size_m: float, crs: str = "EPSG:32610") -> Grid:
        lon0, lat0, lon1, lat1 = bbox
        fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        xs, ys = fwd.transform([lon0, lon1, lon0, lon1], [lat0, lat0, lat1, lat1])
        xmin = np.floor(min(xs) / cell_size_m) * cell_size_m
        ymin = np.floor(min(ys) / cell_size_m) * cell_size_m
        xmax = np.ceil(max(xs) / cell_size_m) * cell_size_m
        ymax = np.ceil(max(ys) / cell_size_m) * cell_size_m
        return cls(
            cell_size_m=float(cell_size_m),
            x0=float(xmin),
            y0=float(ymin),
            n_rows=int(round((ymax - ymin) / cell_size_m)),
            n_cols=int(round((xmax - xmin) / cell_size_m)),
            crs=crs,
        )

    @classmethod
    def from_config(cls, cfg) -> Grid:
        g = cfg["grid"]
        return cls.from_bbox(tuple(g["bbox"]), g["cell_size_m"], g["crs"])

    # ---------------------------------------------------------------- mapping
    @property
    def n_cells(self) -> int:
        return self.n_rows * self.n_cols

    def project(self, lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        fwd = Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)
        return fwd.transform(np.asarray(lon, dtype=float), np.asarray(lat, dtype=float))

    def locate(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        """Return flat cell index (row * n_cols + col) or -1 if outside / invalid."""
        lon = np.asarray(lon, dtype=float)
        lat = np.asarray(lat, dtype=float)
        ok = np.isfinite(lon) & np.isfinite(lat)
        out = np.full(lon.shape, -1, dtype=np.int64)
        if not ok.any():
            return out
        x, y = self.project(lon[ok], lat[ok])
        col = np.floor((x - self.x0) / self.cell_size_m).astype(np.int64)
        row = np.floor((y - self.y0) / self.cell_size_m).astype(np.int64)
        inside = (row >= 0) & (row < self.n_rows) & (col >= 0) & (col < self.n_cols)
        idx = np.where(inside, row * self.n_cols + col, -1)
        out[ok] = idx
        return out

    def cell_id(self, flat: int) -> str:
        r, c = divmod(int(flat), self.n_cols)
        return f"r{r}_c{c}"

    def cell_ids(self, flat: np.ndarray) -> list[str]:
        return [self.cell_id(f) for f in flat]

    def centroids_xy(self) -> tuple[np.ndarray, np.ndarray]:
        rows, cols = np.divmod(np.arange(self.n_cells), self.n_cols)
        return self.x0 + (cols + 0.5) * self.cell_size_m, self.y0 + (rows + 0.5) * self.cell_size_m

    def centroids_lonlat(self) -> tuple[np.ndarray, np.ndarray]:
        inv = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
        return inv.transform(*self.centroids_xy())

    def cell_polygon_lonlat(self, flat: int) -> list[list[float]]:
        inv = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
        r, c = divmod(int(flat), self.n_cols)
        x0 = self.x0 + c * self.cell_size_m
        y0 = self.y0 + r * self.cell_size_m
        s = self.cell_size_m
        xs = [x0, x0 + s, x0 + s, x0, x0]
        ys = [y0, y0, y0 + s, y0 + s, y0]
        lons, lats = inv.transform(xs, ys)
        return [[round(a, 6), round(b, 6)] for a, b in zip(lons, lats, strict=True)]

    def to_geojson(self, active: np.ndarray, props: dict[int, dict] | None = None) -> dict:
        feats = []
        for flat in np.flatnonzero(active):
            r, c = divmod(int(flat), self.n_cols)
            p = {"cell_id": self.cell_id(flat), "row": r, "col": c}
            if props and flat in props:
                p.update(props[flat])
            feats.append(
                {"type": "Feature", "properties": p, "geometry": {"type": "Polygon", "coordinates": [self.cell_polygon_lonlat(flat)]}}
            )
        return {"type": "FeatureCollection", "features": feats}

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.__dict__, indent=2))

    @classmethod
    def load(cls, path: Path) -> Grid:
        return cls(**json.loads(path.read_text()))


def neighbor_sum(a: np.ndarray, radius: int = 1, include_self: bool = False) -> np.ndarray:
    """Sum over the (2r+1)x(2r+1) Moore neighbourhood of the last two axes.

    ``a`` has shape (..., n_rows, n_cols). Zero padding at the lattice edge.
    """
    p = np.pad(a, [(0, 0)] * (a.ndim - 2) + [(radius, radius), (radius, radius)])
    # 2D prefix sums for O(1) box sums
    s = p.cumsum(axis=-1).cumsum(axis=-2)
    s = np.pad(s, [(0, 0)] * (a.ndim - 2) + [(1, 0), (1, 0)])
    k = 2 * radius + 1
    nr, nc = a.shape[-2], a.shape[-1]
    box = s[..., k : k + nr, k : k + nc] - s[..., 0:nr, k : k + nc] - s[..., k : k + nr, 0:nc] + s[..., 0:nr, 0:nc]
    return box if include_self else box - a


def gaussian_smooth(a: np.ndarray, sigma_cells: float) -> np.ndarray:
    """Separable Gaussian smoothing over the last two axes (kernel-density hotspot)."""
    if sigma_cells <= 0:
        return a.astype(float)
    from scipy.ndimage import gaussian_filter

    sig = [0] * (a.ndim - 2) + [sigma_cells, sigma_cells]
    return gaussian_filter(a.astype(float), sigma=sig, mode="constant")
