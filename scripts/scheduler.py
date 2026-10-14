#!/usr/bin/env python
"""Minimal in-container scheduler (for docker-compose / single VM deployments).

* every hour at :10 UTC  -> prospective cycle (ingest, build, forecast, score, export)
* Mondays 03:30 UTC      -> retrain production models, then a cycle
* If SEACRIME_STATE_URI is set, state is pulled before and pushed after each job.

Failures are logged and recorded in the jobs table by the cycle itself; the
scheduler keeps running. Managed deployments use Cloud Scheduler instead
(infra/terraform).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("scheduler")


def run(args: list[str]) -> int:
    log.info("running %s", " ".join(args))
    return subprocess.call([sys.executable, *args], cwd=ROOT)


def job(kind: str) -> None:
    run(["scripts/state_sync.py", "pull"])
    if kind == "train":
        run(["-m", "seacrime.train"])
    rc = run(["-m", "seacrime.prospective.cycle"])
    run(["scripts/state_sync.py", "push"])
    log.info("%s job finished rc=%s", kind, rc)


def next_run(now: datetime) -> tuple[datetime, str]:
    nxt = now.replace(minute=10, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(hours=1)
    kind = "train" if nxt.weekday() == 0 and nxt.hour == 3 else "cycle"
    return nxt, kind


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        nxt, kind = next_run(datetime.now(UTC))
        time.sleep(max(0.0, (nxt - datetime.now(UTC)).total_seconds()))
        try:
            job(kind)
        except Exception:  # never let the scheduler die
            log.exception("job crashed")


if __name__ == "__main__":
    main()
