# Deploying on Dokploy

Two supported shapes. Pick one.

- **[Separate applications](#a-separate-applications)** — Build Type: Dockerfile, with
  a Build Stage. Four Dokploy entries. Fits Dokploy's model, gives each piece its own
  logs, resources and restart behaviour.
- **[One compose application](#b-one-compose-application)** — Build Type: Compose.
  One entry, everything defined in `docker-compose.yml`.

The default stack is three things: **api**, **worker**, **postgres**. The LLM is
Gemini, so there is no model server to run. Transcription stays local, so the
meeting audio never leaves your machine — only the transcript is sent to Google.

(Add an **ollama** service only if you want a fully local LLM too. It is behind
the `local-llm` compose profile and is not started by default.)

The API on its own cannot work — it has no database. If you see this in the log,
only the API is running:

```
work console API 1.0.0 up · db=localhost:5432 · ...
ERROR  WC_DATABASE_URL is not set. Falling back to localhost ...
GET /health 503 Service Unavailable
```

---

## A. Separate applications

The `Dockerfile` is multi-stage. The **Build Stage** field selects which process
you get:

| Build Stage | What runs |
|---|---|
| `api` | the HTTP API, port 8080 |
| `worker` | transcription + analysis, no port |
| *(empty)* | builds the last stage, which is `api` |

The API image does not include faster-whisper; the worker does. That is the point
of the split.

### 1. Postgres

Dokploy → Create → **Database** → PostgreSQL. Note the internal host, user,
password and database name.

### 2. Gemini key

Get an API key from Google AI Studio. Nothing to deploy — this replaces the model
server entirely.

If you would rather run the LLM locally, skip this and see
[Fully local](#fully-local) at the bottom.

### 3. The API

Dokploy → Create → **Application** → this repo, branch `main`.

- Build Type: **Dockerfile**
- Build Stage: **`api`**
- Domain: your hostname → port **8080**
- Volume: host path `/var/lib/workconsole/objects` → container
  `/var/lib/workconsole/objects`

Environment — paste `.env.example` and fill in the blanks, plus these three which
compose would otherwise have set for you:

```
WC_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@POSTGRES_HOST:5432/DBNAME
WC_STORAGE_PATH=/var/lib/workconsole/objects
WC_RUN_MIGRATIONS=true
WC_LLM_PROVIDER=gemini
WC_GEMINI_API_KEY=your-key
```

`+asyncpg` is required. Dokploy will hand you a `postgresql://` URL; a plain one
fails at startup because the driver is async.

`WC_RUN_MIGRATIONS=true` applies migrations when the API boots, so there is no
separate step.

### 4. The worker

Create a **second Application** from the same repo.

- Build Stage: **`worker`**
- **No domain, no port.** It serves no HTTP; giving it a domain will just fail a
  healthcheck forever.
- Volumes: the **same** `/var/lib/workconsole/objects` host path as the API, plus
  `/var/lib/workconsole/models` for the Whisper weights.
- Environment: the same values as the API, except `WC_RUN_MIGRATIONS=false`.

> **The shared volume is not optional.** The API writes the uploaded audio and the
> worker reads it. Different volumes means every job fails with a missing file.
> If you would rather not share a filesystem, switch both to S3
> (`WC_STORAGE_BACKEND=s3` and the `WC_S3_*` settings) and drop the volume.

### 5. Check

```
curl https://your-domain/health
```

`200` with `"database": true` means the API and Postgres are talking. A `503`
carries a `detail` field saying what is wrong.

The worker has no endpoint — look at its logs. On boot it prints:

```
worker <host>:<pid> ready · stt=faster_whisper · llm=ollama
```

---

## B. One compose application

Dokploy → Create → **Compose**, this repo, branch `main`, compose path
`docker-compose.yml`. It defines all four services, the shared volume and the
internal network.

Environment: paste `.env.example` and fill in `WC_API_TOKEN` and
`POSTGRES_PASSWORD`. Do **not** set `WC_DATABASE_URL`, `WC_OLLAMA_URL` or
`WC_STORAGE_PATH` — compose sets those to the internal service names.

Then apply migrations (or set `WC_RUN_MIGRATIONS=true` and skip this):

```
docker compose run --rm api alembic upgrade head
```

Attach the domain to the **api** service on port **8080**.

---

## Connecting the Mac app

Settings → VPS → Server URL = `https://your-domain`, API key = your
`WC_API_TOKEN`. Press CONNECT.

## Models and footprint

| | runs | model | size |
|---|---|---|---|
| speech-to-text | **on your VPS** | `base.en` | ~145 MB |
| LLM | **Gemini API** | `gemini-2.5-flash` | nothing to host |

Nothing else to install. Whole stack:

| | |
|---|---|
| postgres | ~256 MB |
| api | ~256 MB |
| worker | ~600 MB while transcribing, near zero idle |

**Around 1 GB.** A 2 GB VPS is fine. The 8 GB figure only applied when an 8B model
was running locally.

### Where your data goes

- **Meeting audio** — never leaves the VPS. Whisper transcribes it locally, and the
  file is deleted once results are stored.
- **Transcripts, task titles, meeting notes** — sent to Google as part of analysis
  and agent requests.

If that split is not acceptable, see [Fully local](#fully-local).

### Whisper sizes

| model | size | when |
|---|---|---|
| `tiny.en` | ~75 MB | a very small box, clear speech only |
| `base.en` | ~145 MB | **the default** |
| `small.en` | ~480 MB | accents, crosstalk, poor microphones |
| `medium.en` | ~1.5 GB | diminishing returns without a GPU |

With a GPU: `WC_WHISPER_DEVICE=cuda`, `WC_WHISPER_COMPUTE_TYPE=float16`.

### Fully local

No Google at all. Costs memory:

```
WC_LLM_PROVIDER=ollama
WC_OLLAMA_URL=http://OLLAMA_HOST:11434
WC_OLLAMA_MODEL=qwen2.5:0.5b     # ~400 MB, weak agent
WC_LLM_NUM_CTX=4096
WC_ANALYSIS_CHUNK_CHARS=6000
```

Compose: `docker compose --profile local-llm up -d`, then
`docker compose exec ollama ollama pull qwen2.5:0.5b`. Separate applications: add
an Ollama application from the `ollama/ollama:latest` image, no domain.

Model sizes: `qwen2.5:0.5b` ~400 MB, `llama3.2:1b` ~810 MB, `qwen2.5:3b` ~1.9 GB,
`llama3.1:8b` ~4.7 GB. Summaries hold up from 0.5B because decoding is
schema-constrained; the agent needs 3B+ to be useful.

### Transcribing with Gemini too

Removes Whisper entirely — no weights, no ffmpeg, ~512 MB total — but uploads the
meeting audio to Google:

```
WC_TRANSCRIPTION_PROVIDER=gemini
```

## Troubleshooting

**`db=localhost:5432` in the startup log** — `WC_DATABASE_URL` is not set.

**`503` on `/health`** — read the `detail` field. Usually Postgres is still
starting, or the URL is missing `+asyncpg`.

**Health is fine but meetings never finish processing** — look at the worker. It
is a separate process and never appears in the API's logs. The usual causes are
the model not being pulled, or the object-storage volume not being shared.

**`WC_GEMINI_API_KEY is empty`** — the API logs this at startup; every analysis
and agent request will fail until it is set.

**`model '...' is not available`** — only applies to the local-LLM path; run
`ollama pull` in the Ollama container.

**Worker logs `FileNotFoundError` on an audio key** — the API and worker are not
sharing `/var/lib/workconsole/objects`.

**The app shows OFFLINE but curl works** — the API key does not match
`WC_API_TOKEN`. `/health` is unauthenticated, so curl succeeds while everything
else 401s.
