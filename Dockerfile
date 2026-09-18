# ---------------------------------------------------------------------------
# The agent: transcription and meeting analysis. Single stage, one job — Dokploy
# builds this repo and gets the worker, no Build Stage field required.
#
# It serves no HTTP. Give it no domain and no port; it claims jobs from Postgres
# and reads audio from the object store the API writes to.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv/src \
    # Whisper weights cache here. Mount a volume on this path so a redeploy does
    # not re-download them.
    HF_HOME=/var/lib/workconsole/models

WORKDIR /srv

# ffmpeg decodes the uploaded m4a; faster-whisper shells out to it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install ".[whisper,gemini]"

COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 workconsole \
    && mkdir -p /var/lib/workconsole/objects /var/lib/workconsole/models \
    && chown -R workconsole:workconsole /var/lib/workconsole /srv

USER workconsole

# No EXPOSE and no HEALTHCHECK: there is nothing to probe. Liveness is visible in
# the log, which prints "worker <host>:<pid> ready" on boot.
CMD ["python", "-m", "app.workers.runner"]
