# kanban-agent — transcription and analysis worker

The processing half of the macOS work console. One of two deployable services:

| repo | service | Dokploy |
|---|---|---|
| [kanban-worker](https://github.com/cleriko/kanban-worker) | API | domain → port 8080 |
| **kanban-agent** (this one) | transcription + analysis | **no domain** |

It serves no HTTP. It claims jobs from Postgres, reads the recording from the
object store the API wrote it to, transcribes it locally with Whisper, sends the
*transcript* to the LLM for analysis, and writes the results back.

```
audio (object store) ──► faster-whisper ──► transcript ──► LLM ──► summary
                            (local)                      (Gemini)   decisions
                                                                    action items
```

Meeting audio never leaves the machine. Only the transcript is sent to Google.

## Deploy on Dokploy

Create → Application → this repo, Build Type **Dockerfile**. No Build Stage
needed; this repo builds the worker and nothing else.

- **No domain, no port.** Giving it one means a healthcheck that can never pass.
- Volumes:
  - `/var/lib/workconsole/objects` → `/var/lib/workconsole/objects`
    — **the same host path the API uses**
  - `/var/lib/workconsole/models` → `/var/lib/workconsole/models`
    — Whisper weights, so they survive a redeploy

Environment — the same as the API, except `WC_RUN_MIGRATIONS=false`:

```env
WC_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@PG_HOST:5432/DBNAME
WC_API_TOKEN=same-token-as-the-api
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

> **The shared volume is not optional.** The API writes the uploaded audio and
> this service reads it. Different host paths means every job fails with a
> missing file, while both containers look perfectly healthy.

## Verifying

There is no endpoint. Read the log — on boot it prints:

```
worker <host>:<pid> ready · db=<host>:5432 · stt=faster_whisper · llm=gemini
WC_* variables present: WC_API_TOKEN, WC_DATABASE_URL, ...
```

`db=localhost:5432` means `WC_DATABASE_URL` did not reach the container, and it
will never pick up a job. Processing a meeting then logs each stage:

```
meeting <id> transcribed: 42 segments
meeting <id> analysed: 3 action item(s)
```

## Whisper sizes

| model | size | when |
|---|---|---|
| `tiny.en` | ~75 MB | a very small box, clear speech only |
| `base.en` | ~145 MB | **the default** |
| `small.en` | ~480 MB | accents, crosstalk, poor microphones |

Allow ~600 MB while transcribing; near zero idle. With a GPU set
`WC_WHISPER_DEVICE=cuda` and `WC_WHISPER_COMPUTE_TYPE=float16`.

For a fully local stack, set `WC_LLM_PROVIDER=ollama` and `WC_OLLAMA_URL`, and
run Ollama as its own service. Summaries hold up from a 0.5B model because
decoding is schema-constrained; the agent needs 3B+ to be useful.

## Jobs

The queue is a Postgres table, claimed with `FOR UPDATE SKIP LOCKED`. Reusing the
row that already has to exist — the client polls it, the UI renders it — removes
a whole broker from the deployment.

A job whose worker dies is reclaimed once its lease expires
(`WC_JOB_LEASE_SECONDS`, default 30 min), so a killed container never strands
work. Model and network failures are retried up to `WC_JOB_MAX_ATTEMPTS`; bad
data is not retried.

A failure during analysis keeps the transcript. The useful half of the work is
not thrown away because the second half failed.
