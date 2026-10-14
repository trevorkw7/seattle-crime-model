"""CountModel wrapper that trains the neural architectures on whole-grid samples.

Samples are built on the fly from the same leakage-safe FeatureBuilder used by
the classical models (so every network sees exactly the information available
at issue time), plus daily history sequences and recent event tokens taken
strictly before each source's availability cutoff.

GPU is used automatically when available (``torch.cuda.is_available()``);
everything also runs (slowly) on CPU. See configs/gpu/*.yaml and
scripts/train_deep.py for reproducible GPU jobs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import torch

from ...dataset import window_sum
from ..base import CountModel
from ..glm import _design

log = logging.getLogger(__name__)

SKIP = {"how", "x_km", "y_km"}


@dataclass
class DeepGridModel(CountModel):
    model_id: str = "deep"
    family: str = "deep"
    arch: str = "tcn"
    hist_days: int = 28
    token_hours: int = 168
    max_tokens: int = 384
    epochs: int = 12
    samples_per_epoch: int = 768
    batch_size: int = 16
    lr: float = 2e-3
    weight_decay: float = 1e-4
    patience: int = 3
    seed: int = 0
    device: str | None = None
    feat_cols: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    state: dict | None = None
    horizon: int | None = None
    history: list = field(default_factory=list)

    def __post_init__(self):
        self.model_id = self.arch
        self._fb = None
        self._net = None

    # ------------------------------------------------------------ plumbing
    def __getstate__(self):
        # Persist only configuration, normalisation stats and weights. Everything
        # set by attach() (feature builder, prefix-sum/count arrays, geometry) is
        # runtime state and can be gigabytes; it is re-created by attach().
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        if self._net is not None:
            d["state"] = {k: v.detach().cpu() for k, v in self._net.state_dict().items()}
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self._fb = None
        self._net = None

    def attach(self, fb, horizon: int) -> DeepGridModel:
        self._fb = fb
        self.horizon = horizon
        g = fb.grid
        self._R, self._C = g.n_rows, g.n_cols
        self._act = fb.active_flat
        xs, ys = g.centroids_xy()
        xy = np.stack([xs[self._act], ys[self._act]], 1)
        self._xy_km = torch.tensor((xy - xy.min(0)) / 1000.0, dtype=torch.float32)
        self._xy01 = torch.tensor((xy - xy.min(0)) / np.ptp(xy, 0), dtype=torch.float32)
        rows, cols = np.divmod(self._act, g.n_cols)
        self._rc = (rows, cols)
        # sequence channels and their availability lags
        f = fb.f
        self._seq_src = [(fb.S[fb.cat], f.crime_lag_h)]
        for k, S in fb.S.items():
            if k != fb.cat:
                self._seq_src.append((S, f.crime_lag_h))
        if fb.S_calls is not None:
            self._seq_src.append((fb.S_calls, f.calls_lag_h))
        if fb.S_fire is not None:
            self._seq_src.append((fb.S_fire, f.fire_lag_h))
        self._tok_src = [(fb.crime[fb.cat].counts, f.crime_lag_h, 0)]
        if "all_located" in fb.crime and fb.cat != "all_located":
            self._tok_src.append((fb.crime["all_located"].counts, f.crime_lag_h, 1))
        if fb.fire is not None:
            self._tok_src.append((fb.fire.counts, f.fire_lag_h, 2))
        return self

    def _dev(self) -> torch.device:
        return torch.device(self.device or ("cuda" if torch.cuda.is_available() else "cpu"))

    def _adj(self) -> torch.Tensor:
        r, c = self._rc
        N = len(r)
        pos = {(a, b): i for i, (a, b) in enumerate(zip(r, c, strict=True))}
        A = np.eye(N, dtype=np.float32)
        for i, (a, b) in enumerate(zip(r, c, strict=True)):
            for da in (-1, 0, 1):
                for db in (-1, 0, 1):
                    j = pos.get((a + da, b + db))
                    if j is not None:
                        A[i, j] = 1.0
        return torch.tensor(A / A.sum(1, keepdims=True))

    def _make_net(self, n_feat: int):
        from . import nets

        n_seq = len(self._seq_src)
        if self.arch == "tcn":
            return nets.TCNNet(n_feat, n_seq)
        if self.arch == "convlstm":
            return nets.ConvLSTMNet(n_feat, n_seq)
        if self.arch == "stgnn":
            return nets.STGNNNet(n_feat, n_seq, self._adj())
        if self.arch == "transformer":
            return nets.EventTransformer(n_feat)
        if self.arch == "hawkes":
            return nets.NeuralHawkes(n_feat)
        raise ValueError(self.arch)

    # ------------------------------------------------------------- batches
    def _batch(self, t: np.ndarray, with_y: bool = True):
        fb, h = self._fb, self.horizon
        X, y = fb.build(t, h, with_target=with_y)
        B, N = len(t), len(self._act)
        D, _, _ = _design(X, self.feat_cols, self.stats)
        feat_nodes = torch.tensor(D.reshape(B, N, -1), dtype=torch.float32)
        log_base = torch.tensor(np.log(np.maximum(X["base_seasonal"].to_numpy(), 1e-4)).reshape(B, N), dtype=torch.float32)
        out = {"feat_nodes": feat_nodes, "log_base": log_base}
        if self.arch in ("tcn", "convlstm", "stgnn"):
            seq = np.zeros((B, self.hist_days, len(self._seq_src), self._R, self._C), dtype=np.float32)
            for k, (S, lag) in enumerate(self._seq_src):
                cut = t - lag
                for d in range(self.hist_days):
                    seq[:, self.hist_days - 1 - d, k] = window_sum(S, cut - 24 * (d + 1), cut - 24 * d)
            seq = np.log1p(seq)
            if self.arch == "stgnn":
                out["seq_nodes"] = torch.tensor(seq.reshape(B, self.hist_days, -1, self._R * self._C)[..., self._act])
            else:
                out["seq"] = torch.tensor(seq)
                lat = np.zeros((B, feat_nodes.shape[-1], self._R * self._C), dtype=np.float32)
                lat[:, :, self._act] = D.reshape(B, N, -1).transpose(0, 2, 1)
                out["feat"] = torch.tensor(lat.reshape(B, -1, self._R, self._C))
        if self.arch in ("transformer", "hawkes"):
            out["tokens"], out["tok_mask"] = self._tokens(t)
        if with_y:
            out["y"] = torch.tensor(y.reshape(B, N), dtype=torch.float32)
        return out

    def _tokens(self, t: np.ndarray):
        g = self._fb.grid
        B = len(t)
        toks = np.zeros((B, self.max_tokens, 7), dtype=np.float32)
        mask = np.ones((B, self.max_tokens), dtype=bool)
        x0 = float(self._xy_km[:, 0].min())
        xs, ys = g.centroids_xy()
        xs_km = (xs - xs[self._act].min()) / 1000.0
        ys_km = (ys - ys[self._act].min()) / 1000.0
        span = max(float(self._xy_km.max()), 1.0)
        for b, tb in enumerate(t):
            rows = []
            for counts, lag, src in self._tok_src:
                cut = int(tb - lag)
                win = self.token_hours if src != 2 else 24
                lo = max(cut - win, 0)
                blk = np.asarray(counts[lo:cut]).reshape(cut - lo, -1)
                hh, cc = np.nonzero(blk)
                if len(hh) == 0:
                    continue
                age = (tb - (lo + hh)) / 168.0
                cnt = blk[hh, cc]
                onehot = np.zeros((len(hh), 3), np.float32)
                onehot[:, src] = 1
                rows.append(np.column_stack([age, xs_km[cc], ys_km[cc], np.log1p(cnt), onehot]))
            if not rows:
                continue
            r = np.concatenate(rows)
            r = r[np.argsort(r[:, 0])][: self.max_tokens]  # most recent first
            if self.arch == "transformer":
                r[:, 1:3] = r[:, 1:3] / span
            toks[b, : len(r)] = r
            mask[b, : len(r)] = False
        del x0
        return torch.tensor(toks), torch.tensor(mask)

    def _forward(self, bt: dict, dev) -> torch.Tensor:
        net = self._net
        if self.arch in ("tcn", "convlstm"):
            r = net(bt["feat"].to(dev), bt["seq"].to(dev))
            r = r.reshape(r.shape[0], -1)[:, torch.as_tensor(self._act, device=dev)]
            return torch.exp(bt["log_base"].to(dev) + r)
        if self.arch == "stgnn":
            r = net(bt["feat_nodes"].to(dev), bt["seq_nodes"].to(dev))
            return torch.exp(bt["log_base"].to(dev) + r)
        if self.arch == "transformer":
            r = net(bt["feat_nodes"].to(dev), self._xy01.to(dev), bt["tokens"].to(dev), bt["tok_mask"].to(dev))
            return torch.exp(bt["log_base"].to(dev) + r)
        if self.arch == "hawkes":
            return net(bt["feat_nodes"].to(dev), self._xy_km.to(dev), bt["tokens"].to(dev), bt["tok_mask"].to(dev), bt["log_base"].to(dev), float(self.horizon))
        raise ValueError(self.arch)

    # -------------------------------------------------------------- fit/pred
    def _fit(self, X, y, X_val, y_val) -> None:
        if self._fb is None:
            raise RuntimeError("call attach(feature_builder, horizon) before fit")
        torch.manual_seed(self.seed)
        rng = np.random.default_rng(self.seed)
        dev = self._dev()
        t_tr = np.unique(X["_t"].to_numpy())
        t_va = np.unique(X_val["_t"].to_numpy()) if X_val is not None else t_tr[-200:]
        self.feat_cols = [c for c in X.columns if not c.startswith(("_", "base_")) and c not in SKIP]
        _, self.stats, _ = _design(X.sample(min(len(X), 400_000), random_state=0), self.feat_cols, None)
        n_feat = len(_design(X.iloc[:10], self.feat_cols, self.stats)[2])
        self._net = self._make_net(n_feat).to(dev)
        opt = torch.optim.AdamW(self._net.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        val_batches = [self._batch(t_va[i : i + self.batch_size]) for i in range(0, min(len(t_va), 256), self.batch_size)]
        best, best_state, bad = np.inf, None, 0
        for ep in range(self.epochs):
            t_ep = time.time()
            self._net.train()
            pick = rng.choice(t_tr, size=min(self.samples_per_epoch, len(t_tr)), replace=False)
            tr_loss = []
            for i in range(0, len(pick), self.batch_size):
                bt = self._batch(np.sort(pick[i : i + self.batch_size]))
                mu = self._forward(bt, dev)
                yy = bt["y"].to(dev)
                loss = (mu - yy * torch.log(mu + 1e-8)).mean()
                opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self._net.parameters(), 1.0)
                opt.step()
                tr_loss.append(float(loss))
            vl = self._val_loss(val_batches, dev)
            self.history.append({"epoch": ep, "train_loss": float(np.mean(tr_loss)), "val_loss": vl, "sec": time.time() - t_ep})
            log.info("%s h=%s epoch %d train %.5f val %.5f (%.0fs)", self.arch, self.horizon, ep, np.mean(tr_loss), vl, time.time() - t_ep)
            if vl < best - 1e-6:
                best, bad = vl, 0
                best_state = {k: v.detach().clone() for k, v in self._net.state_dict().items()}
            else:
                bad += 1
                if bad >= self.patience:
                    break
        if best_state is not None:
            self._net.load_state_dict(best_state)
        self.features = self.feat_cols
        self.params = {"arch": self.arch, "epochs_run": len(self.history), "best_val_loss": best, "device": str(dev),
                       "n_params": int(sum(p.numel() for p in self._net.parameters()))}

    @torch.no_grad()
    def _val_loss(self, batches, dev) -> float:
        self._net.eval()
        tot, n = 0.0, 0
        for bt in batches:
            mu = self._forward(bt, dev)
            yy = bt["y"].to(dev)
            tot += float((mu - yy * torch.log(mu + 1e-8)).sum())
            n += yy.numel()
        return tot / max(n, 1)

    @torch.no_grad()
    def predict_mu(self, X: pd.DataFrame) -> np.ndarray:
        if self._net is None:
            self._restore()
        dev = self._dev()
        self._net.eval()
        t = pd.unique(X["_t"].to_numpy())
        N = len(self._act)
        assert len(X) == len(t) * N, "rows must be issue-major over all active cells"
        out = []
        for i in range(0, len(t), self.batch_size):
            bt = self._batch(t[i : i + self.batch_size], with_y=False)
            out.append(self._forward(bt, dev).cpu().numpy().ravel())
        return np.maximum(np.concatenate(out), 1e-6)

    def _restore(self) -> None:
        if self.state is None or self._fb is None:
            raise RuntimeError("model has no weights or no attached feature builder")
        n_feat = len(_design(self._fb.build(np.array([self._fb.T - 1000]), self.horizon, with_target=False)[0], self.feat_cols, self.stats)[2])
        self._net = self._make_net(n_feat).to(self._dev())
        self._net.load_state_dict(self.state)
