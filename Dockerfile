# --- stage 1: static dashboard -------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- stage 2: python runtime (API + scheduled jobs) ----------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SEACRIME_DATA_ROOT=/var/lib/seacrime/data SEACRIME_ARTIFACTS_ROOT=/var/lib/seacrime/artifacts \
    SEACRIME_EXPORT_DIR=/var/lib/seacrime/export SEACRIME_WEB_OUT=/app/web/out
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src/ src/
COPY configs/ configs/
COPY scripts/ scripts/
RUN pip install --no-cache-dir -e ".[catboost]"
COPY --from=web /web/out /app/web/out
RUN useradd -r -u 10001 seacrime && mkdir -p /var/lib/seacrime && chown -R seacrime /var/lib/seacrime
USER seacrime
EXPOSE 8000
# default: serve API + dashboard; jobs override the command (see docker-compose.yml / terraform)
CMD ["uvicorn", "seacrime.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
