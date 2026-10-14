"""Read-only HTTP API + static dashboard server.

    uvicorn seacrime.api.app:app --host 127.0.0.1 --port 8000

* ``/data/...``      dashboard JSON documents (see docs/API_CONTRACT.md)
* ``/api/healthz``   liveness/readiness with data-freshness summary
* ``/api/forecast``  latest forecast for a model/category/horizon (cell list)
* ``/``              the static Next.js export (web/out) when built

There are deliberately no endpoints returning incident-level records or
person-level information.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import REPO_ROOT

DATA_DIR = Path(os.environ.get("SEACRIME_EXPORT_DIR", REPO_ROOT / "web" / "public" / "data"))
WEB_OUT = Path(os.environ.get("SEACRIME_WEB_OUT", REPO_ROOT / "web" / "out"))

app = FastAPI(title="Seattle crime forecast research API", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")


def _load(rel: str) -> dict:
    p = (DATA_DIR / rel).resolve()
    if not str(p).startswith(str(DATA_DIR.resolve())) or not p.exists():
        raise HTTPException(404, f"{rel} not found")
    return json.loads(p.read_text())


@app.get("/api/healthz")
def healthz() -> JSONResponse:
    try:
        h = _load("health.json")
    except HTTPException:
        return JSONResponse({"status": "starting", "detail": "no export yet"}, status_code=503)
    bad = [s["name"] for s in h.get("sources", []) if s.get("status") != "ok"]
    return JSONResponse({"status": "ok" if not bad else "degraded", "stale_sources": bad, "generated_at": h["provenance"]["generated_at"]})


@app.get("/api/forecast")
def forecast(model: str = Query("lgbm"), category: str = Query("property"), horizon: int = Query(6)) -> dict:
    idx = _load("forecasts/index.json")
    fid = idx.get("latest", {}).get(model, {}).get(category)
    if not fid:
        raise HTTPException(404, "no forecast for this model/category")
    doc = _load(f"forecasts/{fid}.json")
    hz = doc["horizons"].get(str(horizon))
    if hz is None:
        raise HTTPException(404, "horizon not available")
    cols = doc["columns"]
    return {
        "forecast_id": fid,
        "provenance": doc["provenance"],
        "issued_at": doc["issued_at"],
        "model_version": doc["model_version"],
        "window_start": hz["window_start"],
        "window_end": hz["window_end"],
        "cells": [{"cell_id": c, **dict(zip(cols, v, strict=True))} for c, v in hz["cells"].items()],
    }


if DATA_DIR.exists():
    app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")
if WEB_OUT.exists():
    app.mount("/", StaticFiles(directory=WEB_OUT, html=True), name="web")
