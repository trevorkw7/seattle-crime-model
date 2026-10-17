# Deployment

The dashboard is deployed by the repository owner on Vercel (Git integration,
Root Directory `web`). Hourly live forecasts run on GitHub Actions and are
published to GitHub Pages once Pages is enabled (below); both are free for a
public repository. No paid cloud resources have been provisioned; the Terraform
configuration below is an alternative that has not been applied.

## Live forecasts on GitHub Actions + Pages (free)

`.github/workflows/live-forecast.yml`, every hour (each run starts the next one at
hh:20 because GitHub's cron fires only on a best-effort basis; the `23 * * * *`
cron is a backstop, and pushes to `main` that touch the pipeline start a run):

1. **gate** — checks that GitHub Pages publishes from Actions; otherwise ends.
2. **cycle** — restores state from the Actions cache (processed data, production
   models, frozen-forecast store; the store falls back to its daily release-asset
   backup), downloads every source on a first run, retrains weekly, then runs
   `python -m seacrime.prospective.cycle --models lgbm seasonal --no-export`
   and exports with `python -m seacrime.export --out _site/data --max-live 150
   --max-replay 0 --static-base web/public/data` (backtest results are carried over
   from the committed snapshot because the multi-hour backtests do not run here).
   Older raw snapshots are pruned before the cache is saved.
3. **deploy** — publishes `_site` to GitHub Pages.
4. **next** — waits for the next hh:20 and dispatches the following run. The
   concurrency group allows one run in progress and one waiting, so the chain
   cannot multiply; cancelling a run stops the chain until the cron restarts it.

A failed data source is recorded in `health.json` (Status page) without failing
the run; the run fails (and GitHub notifies the owner) only when cubes or
forecasts could not be produced, and the previous forecasts stay published.

Runner time: about 10 minutes of work per hourly run once state is cached (the
daily SPD refresh adds a few minutes) plus the waiting `next` job; the first run
takes about an hour. Public repositories do not pay for GitHub-hosted runner
minutes.

## Local (verified)

```bash
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -e ".[dev,catboost,deep]"
export PYTHONPATH=src
python -m seacrime.ingest.run spd_crime spd_calls fire_911 weather events gtfs boundaries   # ~10 min
python -m seacrime.ingest.process          # reconcile SPD store, de-duplicate calls/fire
python -m seacrime.build                     # hourly cubes + active cells
python -m seacrime.validate                  # reports/data_validation.md
python -m seacrime.evaluation.backtest --category property            # identical-window backtest
python -m seacrime.train                     # production models
python -m seacrime.prospective.cycle         # ingest → build → forecast(freeze) → score → export
(cd web && npm ci && npm run build)          # static dashboard reading web/public/data
uvicorn seacrime.api.app:app --port 8000     # API + dashboard on http://127.0.0.1:8000
```

`docker compose up --build` runs the same API plus an in-container hourly
scheduler (`scripts/scheduler.py`) bound to localhost.

## Managed, low-cost cloud (Terraform, not applied)

`infra/terraform` (validated with `terraform validate`, provider google ~> 5.40)
creates, in one GCP project:

| Component | Purpose |
|---|---|
| GCS bucket (versioned, private) | persistent state: raw snapshots, offense store, models, frozen forecasts, export |
| Artifact Registry | container image built from `Dockerfile` |
| Cloud Run Job `seacrime-cycle` + Cloud Scheduler (`10 * * * *`) | hourly ingest/build/forecast/score/export; state pulled from / pushed to GCS (`scripts/state_sync.py`), raw snapshots and frozen forecasts are append-only |
| Cloud Run Job `seacrime-train` + Scheduler (Mon 03:30 UTC) | weekly production retraining |
| Cloud Run service `seacrime-api` | read-only API + static dashboard, scales to zero, **internal-only unless `public=true` is explicitly set** |
| Secret Manager | optional `SOCRATA_APP_TOKEN`, `ANTHROPIC_API_KEY` (values added out of band; never in git) |
| Logging metric + alert policy | e-mail alert on cycle errors (`alert_email`) |

Monitoring and error handling: every cycle step is recorded in the `jobs`
table (`data/prospective/registry.sqlite`) and surfaced in `health.json` /
`/api/healthz` (stale sources → `degraded`); failed steps do not stop later
steps; Cloud Run Jobs retry once; the alert policy notifies on errors.

### Cost estimate (approximate list prices, us-west1, before free tiers — verify before applying)

| Item | Assumption | ≈ USD / month |
|---|---|---|
| Hourly cycle job | 2 vCPU / 8 GiB, ~6 min × 720 runs (vCPU-s ≈ $0.000018, GiB-s ≈ $0.000002) | 12–15 |
| Weekly training job | 8 vCPU / 32 GiB, ~1.5 h × 4.3 | 4–5 |
| API service | scale-to-zero, light traffic | 0–2 |
| GCS | ~3–6 GB incl. daily SPD snapshots (≈ 90 MB each; prune/Nearline after 30 days) | 0.2–1 |
| Scheduler, Artifact Registry, logging | 2 scheduler jobs, < 1 GB images | 0–1 |
| **Total** | | **≈ $16–25** |

Cheaper alternatives: the free GitHub Actions + Pages workflow above, or a single
small VM running `docker compose` (2–4 vCPU, 8 GB, e.g. ~$7–30/month depending on
provider). `scripts/state_sync.py` syncs state with a GCS bucket for the Cloud Run
or VM setups.

GPU research runs (`scripts/train_deep.py`): an on-demand NVIDIA L4/T4 instance at
roughly $0.35–1.00 per hour (spot ~60–70 % cheaper); `configs/gpu/deep_property.yaml`
is sized for ≈ 1–3 GPU-hours.

### Applying (requires an authorized project — not done here)

```bash
gcloud builds submit --tag us-west1-docker.pkg.dev/$PROJECT/seacrime/app:$(git rev-parse --short HEAD)
cd infra/terraform && terraform init && terraform apply -var project_id=$PROJECT -var image=...
gcloud run jobs execute seacrime-train --region us-west1   # first training
```

## Secrets

Only environment variables (see `.env.example`); `.env` is git-ignored. No
credentials are required for the public data sources used by default.
