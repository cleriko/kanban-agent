# Deploying on Dokploy

Two supported shapes. Pick one.

- **[Separate applications](#a-separate-applications)** — Build Type: Dockerfile, with
  a Build Stage. Four Dokploy entries. Fits Dokploy's model, gives each piece its own
  logs, resources and restart behaviour.
- **[One compose application](#b-one-compose-application)** — Build Type: Compose.
  One entry, everything defined in `docker-compose.yml`.

Whichever you choose, the stack is always four things: **api**, **worker**,
**postgres**, **ollama**. The API on its own cannot work — it has no database and
no model server. If you see this in the log, only the API is running:

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

### 2. Ollama

Dokploy → Create → **Application** → Docker image `ollama/ollama:latest`.

- No domain. Nothing outside should reach it.
- Mount a volume on `/root/.ollama` or the model is re-downloaded on every deploy.
- 2 GB is enough for the default 0.5B model.

Once it is up, pull a model from its terminal:

```
ollama pull qwen2.5:0.5b
```

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
WC_OLLAMA_URL=http://OLLAMA_HOST:11434
WC_STORAGE_PATH=/var/lib/workconsole/objects
WC_RUN_MIGRATIONS=true
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

Then:

```
docker compose run --rm api alembic upgrade head
docker compose exec ollama ollama pull qwen2.5:0.5b
```

Attach the domain to the **api** service on port **8080**.

---

## Connecting the Mac app

Settings → VPS → Server URL = `https://your-domain`, API key = your
`WC_API_TOKEN`. Press CONNECT.

## Models and footprint

The defaults are deliberately small — about **475 MB of weights** total:

| | model | on disk | RAM in use |
|---|---|---|---|
| speech-to-text | `tiny.en` | ~75 MB | ~400 MB while transcribing |
| LLM | `qwen2.5:0.5b` | ~400 MB | ~1 GB loaded |

With Postgres (~256 MB) and the API (~256 MB) the whole stack sits around
**2 GB**, so a 4 GB VPS is comfortable and 2 GB is survivable.

### What you give up

Be clear-eyed about this. A 0.5B model is small.

- **Summaries and action items**: usable. The JSON schema is enforced during
  decoding, so the shape is always valid, and pulling commitments out of a
  transcript is a narrow task. Expect blunter summaries and the occasional missed
  action item.
- **The agent**: weak. Choosing a tool and composing arguments is exactly what
  small models are bad at. If you want the agent to be genuinely useful, this is
  the part that needs a bigger model.
- **`tiny.en`**: fine for clear speech on a decent microphone. Accents, crosstalk
  and laptop mics across a room will cost you words, and a worse transcript makes
  everything downstream worse.

### Scaling up

Change two variables and pull the model. Nothing else moves.

| Budget | `WC_WHISPER_MODEL` | `WC_OLLAMA_MODEL` | Weights | Notes |
|---|---|---|---|---|
| ~475 MB | `tiny.en` | `qwen2.5:0.5b` | ~475 MB | the default |
| ~1 GB | `tiny.en` | `llama3.2:1b` | ~885 MB | noticeably better prose |
| ~2 GB | `base.en` | `qwen2.5:1.5b` | ~1.2 GB | good balance |
| ~4 GB | `small.en` | `qwen2.5:3b` | ~2.5 GB | agent starts being useful |
| 8 GB+ | `small.en` | `llama3.1:8b` | ~5.3 GB | best without a GPU |

Raise `WC_LLM_NUM_CTX` with the model (4096 → 8192) and
`WC_ANALYSIS_CHUNK_CHARS` with it (6000 → 12000), or long meetings get condensed
in more passes than they need.

With a GPU: `WC_WHISPER_DEVICE=cuda` and `WC_WHISPER_COMPUTE_TYPE=float16`.

### A middle path

Transcription quality matters more than LLM size for getting action items right —
a missed sentence cannot be recovered by a smarter summariser. If you only have
room to spend once, spend it on `base.en` before a bigger LLM.

## Troubleshooting

**`db=localhost:5432` in the startup log** — `WC_DATABASE_URL` is not set.

**`503` on `/health`** — read the `detail` field. Usually Postgres is still
starting, or the URL is missing `+asyncpg`.

**Health is fine but meetings never finish processing** — look at the worker. It
is a separate process and never appears in the API's logs. The usual causes are
the model not being pulled, or the object-storage volume not being shared.

**`model 'qwen2.5:0.5b' is not available`** — run `ollama pull` in the Ollama
container.

**Worker logs `FileNotFoundError` on an audio key** — the API and worker are not
sharing `/var/lib/workconsole/objects`.

**The app shows OFFLINE but curl works** — the API key does not match
`WC_API_TOKEN`. `/health` is unauthenticated, so curl succeeds while everything
else 401s.
