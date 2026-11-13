# Deploying the agent on Dokploy

This repo is one half of the deployable split: it builds the transcription and
analysis worker and nothing else. It serves no HTTP — no domain, no port. The API
lives in [kanban-worker](https://github.com/cleriko/kanban-worker) and runs against
the same Postgres and the same object-storage volume. The combined reference
implementation is `kanban-backend`.

## The services

| # | Dokploy type | Build | Domain |
|---|---|---|---|
| 1 | Database → PostgreSQL | — | none |
| 2 | Application → this repo | Dockerfile | **none** |

Use the database the API already created; do not stand up a second one.

---

## 1. Postgres

If the API is already deployed, copy its **internal** Dokploy host — something like
`workconsole-db-a1b2c3`. It is *not* `localhost`, and it is not the external host.

If you are creating the database from scratch: Dokploy → Create → **Database** →
PostgreSQL. Any user/password/database name. If a Compose file creates Postgres,
`POSTGRES_PASSWORD` is required; there is no built-in default.

## 2. Agent

Create → **Application** → this repo, branch `main`.

- Build Type: **Dockerfile**. No Build Stage — the Dockerfile is single-stage and
  builds the worker only.
- **No domain and no port.** It serves no HTTP; a domain would healthcheck forever
  and never pass.
- Volumes:
  - `/var/lib/workconsole/objects` → `/var/lib/workconsole/objects`
    — **the same host path the API uses**
  - `/var/lib/workconsole/models` → `/var/lib/workconsole/models`
    — Whisper weights, so they survive a redeploy

Environment:

```env
WC_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@PG_HOST:5432/DBNAME
WC_GEMINI_API_KEY=your-google-ai-studio-key
WC_LLM_PROVIDER=gemini
WC_GEMINI_MODEL=gemini-2.5-flash
WC_TRANSCRIPTION_PROVIDER=faster_whisper
WC_WHISPER_MODEL=base.en
WC_WHISPER_COMPUTE_TYPE=int8
WC_WHISPER_LANGUAGE=en
WC_STORAGE_BACKEND=local
WC_STORAGE_PATH=/var/lib/workconsole/objects
WC_RUN_MIGRATIONS=false
WC_LOG_LEVEL=info
```

`WC_API_TOKEN` is not needed here: this service accepts no requests. Auth is
enforced by the API, which fails closed — with the token unset every authenticated
route returns **503**. `WC_ALLOW_UNAUTHENTICATED=1` is an API-side opt-out for a
private tunnel or local tests; never set it on a public host.

The API rejects audio that is not a recognised container (m4a/mp4, wav, mp3, ogg,
webm) with **400**, so this service only ever sees validated uploads.

> **The shared volume is not optional.** The API writes the uploaded audio and
> this service reads it. Different host paths means every job fails with a
> missing file, while both containers look perfectly healthy.

Only the API runs migrations. Keep `WC_RUN_MIGRATIONS=false` here or the two
services race to change the schema.

---

## "None of my environment variables are set"

If the startup log shows `db=localhost:5432` **and** `WC_GEMINI_API_KEY is empty`
all at once, none of them are reaching the container. That is one problem, not
two. The log says so directly:

```
ERROR  No WC_* environment variables are set in this container.
```

In order of likelihood:

1. **You saved the environment but did not redeploy.** Dokploy bakes the
   environment in at deploy time; editing it does nothing to a running container.
   Hit **Redeploy** after saving.
2. **It went into the wrong box.** Dokploy has a separate *Build-time variables*
   section. These are runtime settings — they belong in **Environment**.
3. **It is on the wrong service.** Each Application has its own environment.
4. **Blank lines or quotes.** Use `WC_DATABASE_URL=abc`, not
   `WC_DATABASE_URL="abc"` and not `export WC_DATABASE_URL=abc`.

To confirm from inside, open the service's terminal in Dokploy:

```
env | grep WC_
```

Nothing printed means the environment never arrived, and no amount of changing
the values will help until that is fixed.

## The things that break this

**1. The driver prefix.** Dokploy hands you `postgresql://…`. It must be
`postgresql+asyncpg://…` — the driver is async and a plain URL fails at startup.

**2. The host.** `WC_DATABASE_URL` must point at Postgres's *internal* Dokploy
host. If the startup log says `db=localhost:5432`, the variable is not set and the
agent is talking to itself. It will never pick up a job.

**3. The shared volume.** The API writes the uploaded audio; this service reads
it. If they are not on the same host path, every job fails with a missing file.
(To avoid sharing a filesystem, use `WC_STORAGE_BACKEND=s3` instead.)

---

## Checking it worked

There is no endpoint. Read the log — on boot it prints:

```
worker <host>:<pid> ready · db=<host>:5432 · stt=faster_whisper · llm=gemini
WC_* variables present: WC_DATABASE_URL, ...
```

`db=localhost:5432` means `WC_DATABASE_URL` did not reach the container, and it
will never pick up a job. Processing a meeting then logs each stage:

```
meeting <id> transcribed: 42 segments
meeting <id> analysed: 3 action item(s)
```

Then point the Mac app at the API, not at this service.

---

## Models and footprint

| | runs | model | size |
|---|---|---|---|
| speech-to-text | **on your VPS** | `base.en` | ~145 MB |
| LLM | **Gemini API** | `gemini-2.5-flash` | nothing to host |

Allow ~600 MB while transcribing; near zero idle. With a GPU set
`WC_WHISPER_DEVICE=cuda` and `WC_WHISPER_COMPUTE_TYPE=float16`.

### Where your data goes

- **Meeting audio** — never leaves the VPS. Whisper transcribes it locally, and
  the file is deleted once results are stored.
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

### Fully local

No Google at all. Costs memory:

```
WC_LLM_PROVIDER=ollama
WC_OLLAMA_URL=http://OLLAMA_HOST:11434
WC_OLLAMA_MODEL=qwen2.5:0.5b     # ~400 MB, weak agent
WC_LLM_NUM_CTX=4096
WC_ANALYSIS_CHUNK_CHARS=6000
```

Add an Ollama application from the `ollama/ollama:latest` image, no domain. Model
sizes: `qwen2.5:0.5b` ~400 MB, `llama3.2:1b` ~810 MB, `qwen2.5:3b` ~1.9 GB,
`llama3.1:8b` ~4.7 GB. Summaries hold up from 0.5B because decoding is
schema-constrained; the agent needs 3B+ to be useful.

### Transcribing with Gemini too

Removes Whisper entirely — no weights, no ffmpeg, ~512 MB for this container — but
uploads the meeting audio to Google:

```
WC_TRANSCRIPTION_PROVIDER=gemini
```

---

## Troubleshooting

**`db=localhost:5432` in the startup log** — `WC_DATABASE_URL` is not set.

**Health is fine but meetings never finish processing** — look at this service's
log. The usual causes are the model not being pulled, or the object-storage
volume not being shared with the API.

**`WC_GEMINI_API_KEY is empty`** — every analysis request will fail until it is
set.

**`model '...' is not available`** — only applies to the local-LLM path; run
`ollama pull` in the Ollama container.

**`FileNotFoundError` on an audio key** — the API and this service are not sharing
`/var/lib/workconsole/objects`.

**The app shows OFFLINE but curl works** — that is the API, not this service. The
API key does not match `WC_API_TOKEN` (401), or `WC_API_TOKEN` is unset on the
server (503).
