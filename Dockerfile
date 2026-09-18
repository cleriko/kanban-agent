# ---------------------------------------------------------------------------
# Multi-stage. Two runnable targets, so the same repo can be deployed twice as
# separate Dokploy applications:
#
#   target: api      the HTTP API          (Build Stage: api)
#   target: worker   transcription + LLM   (Build Stage: worker)
#
# Leaving Dokploy's "Build Stage" empty builds the last stage, which is `api`.
#
# The split is not cosmetic: the worker needs faster-whisper and ctranslate2,
# several hundred megabytes the API never touches. Building them separately keeps
# the API image small and its deploys fast.
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv/src \
    # Whisper weights cache here. Mount a volume on this path so they survive
    # a redeploy instead of being downloaded again.
    HF_HOME=/var/lib/workconsole/models

WORKDIR /srv

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 workconsole \
    && mkdir -p /var/lib/workconsole/objects /var/lib/workconsole/models \
    && chown -R workconsole:workconsole /var/lib/workconsole /srv


# --- worker ----------------------------------------------------------------
# Transcription and analysis. No HTTP server, so no port and no healthcheck —
# Dokploy should not expect either.
FROM base AS worker

# ffmpeg decodes the uploaded m4a; faster-whisper shells out to it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install ".[whisper]"

USER workconsole
CMD ["python", "-m", "app.workers.runner"]


# --- api -------------------------------------------------------------------
# Last stage on purpose: an empty "Build Stage" in Dokploy lands here.
FROM base AS api

USER workconsole
EXPOSE 8080

# /health returns 503 when Postgres is unreachable, so this genuinely reflects
# whether the service can do its job.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
