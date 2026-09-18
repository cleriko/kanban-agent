# Deploying on Dokploy

## The three services

| # | Dokploy type | Build Stage | Domain |
|---|---|---|---|
| 1 | Database → PostgreSQL | — | none |
| 2 | Application → this repo | `api` | yes, → port **8080** |
| 3 | Application → this repo | `worker` | **none** |

Same repo deployed twice. The `Dockerfile` is multi-stage; the **Build Stage**
field picks which process you get. There is no Ollama — the LLM is the Gemini API.

---

## 1. Postgres

Dokploy → Create → **Database** → PostgreSQL. Any user/password/database name.

When it is running, open it and copy the **internal** host — something like
`workconsole-db-a1b2c3`. That is what the other two services connect to. It is
*not* `localhost`, and it is not the external host.

## 2. API

Create → **Application** → this repo, branch `main`.

- Build Type: **Dockerfile**
- Build Stage: **`api`**
- Domain: your hostname → port **8080**
- Volume: `/var/lib/workconsole/objects` → `/var/lib/workconsole/objects`

Environment:

```env
WC_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@PG_HOST:5432/DBNAME
WC_API_TOKEN=generate-with-openssl-rand-hex-24
WC_GEMINI_API_KEY=your-google-ai-studio-key
WC_LLM_PROVIDER=gemini
WC_GEMINI_MODEL=gemini-2.5-flash
WC_TRANSCRIPTION_PROVIDER=faster_whisper
WC_WHISPER_MODEL=base.en
WC_WHISPER_COMPUTE_TYPE=int8
WC_STORAGE_BACKEND=local
WC_STORAGE_PATH=/var/lib/workconsole/objects
WC_RUN_MIGRATIONS=true
WC_LOG_LEVEL=info
```

## 3. Worker

Create a **second Application** from the same repo.

- Build Stage: **`worker`**
- **No domain and no port.** It serves no HTTP; a domain would healthcheck
  forever and never pass.
- Volumes:
  - `/var/lib/workconsole/objects` → `/var/lib/workconsole/objects`
    — **the same host path as the API**
  - `/var/lib/workconsole/models` → `/var/lib/workconsole/models`
    — Whisper weights, so they survive redeploys

Environment: identical to the API, except:

```env
WC_RUN_MIGRATIONS=false
```

Both need `WC_GEMINI_API_KEY`: the API runs the agent, the worker runs meeting
analysis.

---

## The three things that break this

**1. The driver prefix.** Dokploy hands you `postgresql://…`. It must be
`postgresql+asyncpg://…` — the driver is async and a plain URL fails at startup.

**2. The host.** `WC_DATABASE_URL` must point at Postgres's *internal* Dokploy
host. If the startup log says `db=localhost:5432`, the variable is not set and
the API is talking to itself:

```
work console API 1.0.0 up · db=localhost:5432 · ...
ERROR  WC_DATABASE_URL is not set. Falling back to localhost ...
GET /health 503 Service Unavailable
```

**3. The shared volume.** The API writes the uploaded audio; the worker reads it.
If they are not on the same host path, every job fails with a missing file. (To
avoid sharing a filesystem, use `WC_STORAGE_BACKEND=s3` instead.)

---

## Checking it worked

```
curl https://your-domain/health
```

`200` with `"database": true`. A `503` carries a `detail` field saying why.

The API log should read `db=<your-pg-host>:5432`.

The worker has no endpoint — read its log. On boot:

```
worker <host>:<pid> ready · stt=faster_whisper · llm=gemini
```

Then point the Mac app at it: Settings → VPS → Server URL `https://your-domain`,
API key = your `WC_API_TOKEN`.

---

## Alternative: one compose application

If you would rather run it as a single Dokploy **Compose** application, the repo's
`docker-compose.yml` defines everything and sets the database URL and volumes for
you. Paste `.env.example` into the Environment tab, set `WC_API_TOKEN`,
`POSTGRES_PASSWORD` and `WC_GEMINI_API_KEY`, and attach the domain to the `api`
service on port 8080.

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
