# ---------------------------------------------------------------------------
# One image, two process types: the API (default CMD) and the worker
# (command overridden in compose). Dokploy builds this from the repo root.
#
# Build args:
#   EXTRAS=whisper   include the local speech-to-text runtime (needed by the worker,
#                    harmless in the API). Omit for a smaller API-only image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ARG EXTRAS=whisper

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv/src \
    # Whisper weights are cached here; mount a volume on this path so they are
    # downloaded once rather than on every deploy.
    HF_HOME=/var/lib/workconsole/models

WORKDIR /srv

# ffmpeg decodes the uploaded m4a for transcription. curl is used by the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so a code change does not reinstall the world.
COPY pyproject.toml ./
COPY src ./src
RUN if [ -n "$EXTRAS" ]; then pip install ".[$EXTRAS]"; else pip install .; fi

COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 workconsole \
    && mkdir -p /var/lib/workconsole/objects /var/lib/workconsole/models \
    && chown -R workconsole:workconsole /var/lib/workconsole /srv

USER workconsole

EXPOSE 8080

# Dokploy shows this as the container's health. /health needs no auth by design.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
