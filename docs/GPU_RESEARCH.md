# Neural model research (GPU)

No GPU was available in the development environment (4 vCPU, 16 GB RAM). All
five architectures are implemented, unit-smoke-tested on CPU, and a reduced
CPU comparison was executed (`configs/gpu/deep_cpu.yaml`; results in
[reports/backtest_results.md](../reports/backtest_results.md) under the
`deep_cpu` run). Full-scale runs need a GPU and are fully scripted.

## Architectures (`src/seacrime/models/deep/nets.py`)

All predict a log-rate *residual* over the seasonal baseline (head initialised
to zero), use the same leakage-safe inputs as the classical models and are
trained with the Poisson likelihood of windowed counts:

| id | idea | inputs |
|---|---|---|
| `tcn` | dilated causal 1-D convolutions over each cell's daily history (shared weights) + 3×3 spatial mixer | daily history (28–84 d) of target, other categories, CAD calls, Fire/EMS; engineered features |
| `convlstm` | ConvLSTM over the daily lattice frames | same |
| `stgnn` | GRU over node histories with graph convolutions on the queen-adjacency graph of active cells (GConvGRU-style) | same, as graph signals |
| `transformer` | self-attention over the recent *event sequence* (age, location, count, source) + cross-attention from each cell's query (Fourier location features + engineered features) | last N available events |
| `hawkes` | discretised spatio-temporal self-exciting point process: background = seasonal × exp(MLP(features)); triggering kernel = exp(MLP(age, distance, source)) with Gaussian spatial decay | last N available events |

The event-based models only see events older than each source's availability
cutoff (72 h for crime reports), which by construction limits the classic
"near-repeat" self-excitation signal; the lag-sensitivity experiment quantifies
how much fresher data would help.

## Jobs

| config | purpose | approx. cost |
|---|---|---|
| `configs/gpu/cpu_smoke.yaml` | 1-epoch smoke test of every architecture (CI/CPU) | minutes on CPU |
| `configs/gpu/deep_cpu.yaml` | executed reduced comparison on CPU (h = 6, 24; 2,000 training issue times; 8 epochs; 3 h test stride) | ~1–2 h CPU |
| `configs/gpu/deep_property.yaml` | full comparison (6,000 training issue times, 40 epochs, 56-day history, hourly test windows) | ~1–3 GPU-hours on an L4/T4 |
| `configs/gpu/deep_all_categories.yaml` | all categories and horizons | ~6–10 GPU-hours |

## Launching on a GPU machine

```bash
git clone https://github.com/trevorkw7/seattle-crime-model && cd seattle-crime-model
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[catboost]" torch            # CUDA wheels
export PYTHONPATH=src
python -m seacrime.ingest.run spd_crime spd_calls fire_911 weather events gtfs boundaries
python -m seacrime.ingest.process && python -m seacrime.build
python scripts/train_deep.py --job configs/gpu/deep_property.yaml        # logs device; falls back to CPU if no CUDA
python scripts/make_report.py
```

On GCP, an `g2-standard-8` (1× L4) spot VM or a Vertex AI custom job with the
repository's Docker image (add `torch` with CUDA) runs the job; results are
written to `artifacts/backtests/property_report_deep/<run_id>/report.json` with the
seasonal and LightGBM references re-scored on identical windows.
