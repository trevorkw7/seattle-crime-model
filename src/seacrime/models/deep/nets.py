"""Compact spatiotemporal neural architectures (PyTorch).

All networks predict a *log-rate residual* r over the seasonal baseline:

    log mu(cell, window) = log base_seasonal(cell, window) + r(cell, window)

with the residual head initialised to zero, so training starts from the strong
seasonal baseline and only learns deviations supported by data.

Inputs (per sample = one issue time, full lattice R x C):
  feat  (F, R, C)   per-cell engineered features (leakage-safe, standardised)
  seq   (D, K, R, C) daily history ending at each source's availability cutoff
  tokens (N, 7)     recent space-time events (age, x, y, log-count, source one-hot x3)
                    used by the event transformer and the neural point process
"""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F


def _zero_head(m: nn.Module) -> nn.Module:
    last = [x for x in m.modules() if isinstance(x, (nn.Conv2d, nn.Linear))][-1]
    nn.init.zeros_(last.weight)
    nn.init.zeros_(last.bias)
    return m


class TCNNet(nn.Module):
    """Dilated causal temporal convolutions per cell (shared weights) over the
    daily history, fused with per-cell features and a 3x3 spatial mixer."""

    def __init__(self, n_feat: int, n_seq: int, hidden: int = 32, levels: int = 3):
        super().__init__()
        layers, c = [], n_seq
        for i in range(levels):
            d = 2**i
            layers += [nn.Conv1d(c, hidden, kernel_size=3, dilation=d, padding=2 * d), nn.GELU()]
            c = hidden
        self.tcn = nn.ModuleList(layers)
        self.fuse = nn.Sequential(
            nn.Conv2d(hidden + n_feat, hidden, 1), nn.GELU(), nn.Conv2d(hidden, hidden, 3, padding=1), nn.GELU()
        )
        self.head = _zero_head(nn.Conv2d(hidden, 1, 1))

    def forward(self, feat, seq, tokens=None):
        B, D, K, R, C = seq.shape
        x = seq.permute(0, 3, 4, 2, 1).reshape(B * R * C, K, D)
        for layer in self.tcn:
            x = layer(x)
            if isinstance(layer, nn.Conv1d):
                x = x[..., :D]  # causal crop
        h = x[..., -1].reshape(B, R, C, -1).permute(0, 3, 1, 2)
        return self.head(self.fuse(torch.cat([h, feat], 1))).squeeze(1)


class ConvLSTMCell(nn.Module):
    def __init__(self, cin: int, hidden: int):
        super().__init__()
        self.hidden = hidden
        self.conv = nn.Conv2d(cin + hidden, 4 * hidden, 3, padding=1)

    def forward(self, x, state):
        h, c = state
        i, f, o, g = self.conv(torch.cat([x, h], 1)).chunk(4, 1)
        c = torch.sigmoid(f) * c + torch.sigmoid(i) * torch.tanh(g)
        h = torch.sigmoid(o) * torch.tanh(c)
        return h, c


class ConvLSTMNet(nn.Module):
    def __init__(self, n_feat: int, n_seq: int, hidden: int = 16):
        super().__init__()
        self.cell = ConvLSTMCell(n_seq, hidden)
        self.fuse = nn.Sequential(nn.Conv2d(hidden + n_feat, 32, 3, padding=1), nn.GELU())
        self.head = _zero_head(nn.Conv2d(32, 1, 1))

    def forward(self, feat, seq, tokens=None):
        B, D, K, R, C = seq.shape
        h = seq.new_zeros(B, self.cell.hidden, R, C)
        c = torch.zeros_like(h)
        for d in range(D):
            h, c = self.cell(seq[:, d], (h, c))
        return self.head(self.fuse(torch.cat([h, feat], 1))).squeeze(1)


class STGNNNet(nn.Module):
    """Spatiotemporal graph network: a GRU over daily history per node with
    graph convolutions (row-normalised queen adjacency over active cells)
    applied to the GRU input and the readout (GConvGRU-style, simplified)."""

    def __init__(self, n_feat: int, n_seq: int, adj: torch.Tensor, hidden: int = 32, gc_layers: int = 2):
        super().__init__()
        self.register_buffer("adj", adj)  # (N, N) sparse-ish dense, row-normalised incl. self loop
        self.inp = nn.Linear(2 * n_seq, hidden)
        self.gru = nn.GRU(hidden, hidden, batch_first=True)
        self.gcs = nn.ModuleList([nn.Linear(2 * hidden + (n_feat if i == 0 else 0), hidden) for i in range(gc_layers)])
        self.head = _zero_head(nn.Linear(hidden, 1))

    def forward(self, feat_nodes, seq_nodes, tokens=None):
        # feat_nodes (B, N, F); seq_nodes (B, D, K, N)
        B, D, K, N = seq_nodes.shape
        x = seq_nodes.permute(0, 3, 1, 2)  # B N D K
        nb = torch.einsum("nm,bmdk->bndk", self.adj, x)
        z = self.inp(torch.cat([x, nb], -1)).reshape(B * N, D, -1)
        _, hT = self.gru(z)
        h = hT[-1].reshape(B, N, -1)
        for i, gc in enumerate(self.gcs):
            agg = torch.einsum("nm,bmh->bnh", self.adj, h)
            parts = [h, agg] + ([feat_nodes] if i == 0 else [])
            h = F.gelu(gc(torch.cat(parts, -1)))
        return self.head(h).squeeze(-1)  # (B, N)


def fourier_xy(xy: torch.Tensor, n: int = 8) -> torch.Tensor:
    freqs = 2.0 ** torch.arange(n, device=xy.device, dtype=xy.dtype) * torch.pi
    a = xy[..., None] * freqs
    return torch.cat([a.sin(), a.cos()], -1).flatten(-2)


class EventTransformer(nn.Module):
    """Compact transformer over the recent space-time event sequence.

    Event tokens (available history only) are encoded with self-attention;
    every grid cell then cross-attends to the encoded events with a query built
    from its location (Fourier features) and engineered features."""

    def __init__(self, n_feat: int, d: int = 64, heads: int = 4, layers: int = 2, tok_dim: int = 7):
        super().__init__()
        self.tok = nn.Sequential(nn.Linear(tok_dim + 32, d), nn.GELU(), nn.Linear(d, d))
        enc = nn.TransformerEncoderLayer(d, heads, dim_feedforward=2 * d, dropout=0.0, batch_first=True)
        self.encoder = nn.TransformerEncoder(enc, layers)
        self.q = nn.Sequential(nn.Linear(n_feat + 32, d), nn.GELU(), nn.Linear(d, d))
        self.cross = nn.MultiheadAttention(d, heads, batch_first=True)
        self.head = _zero_head(nn.Sequential(nn.Linear(2 * d, d), nn.GELU(), nn.Linear(d, 1)))

    def forward(self, feat_nodes, cell_xy, tokens, tok_mask):
        # feat_nodes (B, N, F); cell_xy (N, 2) in [0,1]; tokens (B, T, 6); tok_mask (B, T) True = pad
        tx = self.tok(torch.cat([tokens, fourier_xy(tokens[..., 1:3])], -1))
        tx = self.encoder(tx, src_key_padding_mask=tok_mask)
        q = self.q(torch.cat([feat_nodes, fourier_xy(cell_xy).expand(feat_nodes.shape[0], -1, -1)], -1))
        all_pad = tok_mask.all(1)
        safe_mask = tok_mask.clone()
        safe_mask[all_pad, 0] = False  # avoid NaN when a sample has no events
        a, _ = self.cross(q, tx, tx, key_padding_mask=safe_mask)
        a = a * (~all_pad)[:, None, None]
        return self.head(torch.cat([q, a], -1)).squeeze(-1)


class NeuralHawkes(nn.Module):
    """Discretised spatio-temporal self-exciting point process with neural
    background modulation and a neural triggering kernel:

        lambda(cell, window) = base * exp(g(features))
                               + h * sum_events exp(k(age, dist, src) - d^2/(2*0.5km^2)) * count

    Fitted with the exact Poisson likelihood of windowed counts (intensity
    assumed piecewise constant within the window)."""

    def __init__(self, n_feat: int, hidden: int = 32, n_src: int = 3):
        super().__init__()
        self.g = _zero_head(nn.Sequential(nn.Linear(n_feat, hidden), nn.GELU(), nn.Linear(hidden, 1)))
        self.k = nn.Sequential(nn.Linear(2 + n_src, hidden), nn.GELU(), nn.Linear(hidden, 1))
        nn.init.zeros_(self.k[-1].weight)
        nn.init.constant_(self.k[-1].bias, -9.0)  # start with negligible excitation

    def forward(self, feat_nodes, cell_xy_km, tokens, tok_mask, log_base, horizon_h: float):
        # tokens: age_h/168, x_km, y_km (raw km here), log1p count, src one-hot(3)
        B, N, _ = feat_nodes.shape
        bg = torch.exp(log_base + self.g(feat_nodes).squeeze(-1))
        age = tokens[..., 0:1]  # (B, T, 1) scaled age
        src = tokens[..., 4:]
        d = torch.cdist(cell_xy_km.expand(B, -1, -1), tokens[..., 1:3])  # (B, N, T) km
        inp = torch.cat([age.unsqueeze(1).expand(-1, N, -1, -1), d.unsqueeze(-1) / 2.0, src.unsqueeze(1).expand(-1, N, -1, -1)], -1)
        # log-kernel = learned MLP minus a Gaussian spatial decay (0.5 km scale) so
        # that, at initialisation, excitation is negligible and strictly local
        logk = self.k(inp).squeeze(-1) - 0.5 * (d / 0.5) ** 2
        kern = torch.exp(logk.clamp(max=5.0))  # (B, N, T)
        cnt = torch.expm1(tokens[..., 3]).clamp(min=0)
        kern = kern * (~tok_mask).unsqueeze(1) * cnt.unsqueeze(1)
        return bg + horizon_h * kern.sum(-1)  # returns mu directly (B, N)
