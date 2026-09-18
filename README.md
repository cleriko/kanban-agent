# Work Console — backend

The brain. Everything heavy runs here: transcription, analysis, agent reasoning,
storage and the job queue. The macOS client (`kanban-mac`) is a thin renderer.

By default **nothing leaves this machine**. Speech-to-text is faster-whisper running
locally; reasoning is a local LLM over Ollama. A cloud provider exists behind a config
flag and is never selected implicitly.

## Shape

```
macOS app ──HTTPS──► API (FastAPI)
                      ├── Postgres          tasks, meetings, jobs, agent actions
                      ├── Object storage    audio + nothing else
                      └── Job queue ──► Worker
                                          ├── faster-whisper   audio → transcript
                                          └── local LLM        transcript → notes,
                                                               decisions, action items
```

Two process types, one image: `api` serves requests, `worker` drains the queue. They
share nothing but Postgres and the object store, so the worker can be moved to a GPU
box later without touching the API.

## Layout

```
src/app/
├── api/              routes + wire schemas (the contract with the Mac app)
├── services/         the only code that touches the database
├── workers/          queue runner + the meeting pipeline
├── agent/            LangGraph loop, tools, prompts
├── ai/               provider interfaces (whisper / ollama / gemini / fake) + gateway
├── db/               SQLAlchemy models and session handling
└── infrastructure/   queue, object storage, SSE, logging
```

The rule the layout enforces: **the agent never touches the database.** It names a
tool, `agent/tools.py` decides what that means, and a service does the work.

## Running it

### Dokploy (what this is deployed with)

Full walkthrough in [DEPLOY.md](DEPLOY.md). The short version — and the one thing
people get wrong — is that this must be a **Compose** application, not a
Dockerfile one. It is four services, and the compose file creates them all.

1. New application → **Docker Compose**, pointed at this repo.
2. Paste `.env.example` into the Environment tab and **fill in the blanks** —
   `WC_API_TOKEN` and `POSTGRES_PASSWORD`. Nothing sets these for you;
   `scripts/generate-env.sh` will generate them locally if you want.
   `WC_DATABASE_URL`, `WC_OLLAMA_URL` and `WC_STORAGE_PATH` are set by compose,
   so leave those out.

   If `WC_DATABASE_URL` is somehow not set, the built-in default points at
   `localhost` — which inside a container is the container itself, so Postgres
   is refused and `/health` returns 503. The startup log says so explicitly.
3. Deploy. (`scripts/generate-env.sh` fills the blank secrets in `.env.example`
   locally if you want them generated for you.)
4. Migrate and pull the model:
   ```
   docker compose run --rm api alembic upgrade head
   docker compose exec ollama ollama pull qwen2.5:0.5b
   ```
5. Attach a domain to the **api** service on port **8080** (the only port the
   stack exposes; Postgres and Ollama stay on the internal network). Dokploy adds the Traefik
   labels and the certificate.
6. In the Mac app: Settings → VPS → that URL, plus the same `WC_API_TOKEN`.

Only `api` joins `dokploy-network`; Postgres, Ollama and the worker stay on the
internal network and are not reachable from outside. Persistent data lives in
`../files/` on the host, which is where Dokploy keeps Compose bind mounts.

### Plain Docker

```
cp .env.example .env      # set WC_API_TOKEN
docker compose up -d postgres ollama
docker compose run --rm api alembic upgrade head
docker compose exec ollama ollama pull qwen2.5:0.5b
docker compose up -d
```

### Without Docker

```
python -m venv .venv && .venv/bin/pip install '.[whisper,dev]'
export WC_DATABASE_URL=postgresql+asyncpg://...
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --port 8080     # API
.venv/bin/python -m app.workers.runner          # worker, separate process
```

`deploy/` has systemd units and a Caddyfile for this path.

## API

Versioned under `/api/v1`.

| | |
|---|---|
| `GET /health` | unauthenticated, so a tunnel can be probed. **503** when Postgres is unreachable |
| `GET/POST /tasks`, `GET/PATCH/DELETE /tasks/{id}` | task CRUD |
| `POST /tasks/{id}/move`, `/complete`, `/reopen` | explicit transitions |
| `GET /summary` | counts + agenda |
| `GET/POST /meetings`, `GET/DELETE /meetings/{id}` | meeting records |
| `PUT /meetings/{id}/audio` | streams the recording into object storage |
| `POST /meetings/{id}/process` | **returns a job**, does not block |
| `GET /meetings/{id}/transcript`, `/action-items` | results |
| `GET /jobs/{id}`, `GET /jobs/{id}/events` | polling, and SSE progress |
| `POST /agent/chat` | one agent turn |
| `POST /agent/actions/{id}/execute` | runs one confirmed operation |
| `GET /agent/actions` | pending, or `?history=true` for the audit trail |

Everything except `/health` needs `Authorization: Bearer $WC_API_TOKEN`.

Interactive docs at `/api/docs`.

## How the agent is kept on a leash

A read tool runs immediately — the model needs the result to reason.

A **write** does not. It is validated, rendered as a one-line summary, and stored in
`agent_actions` with a snapshot of the row it would change. The client shows it and the
user presses EXECUTE, which calls `/agent/actions/{id}/execute`. That is the only path
by which an agent write reaches the database.

Two consequences worth knowing:

- The task id is resolved **when the action is proposed**, not when it runs. You confirm
  the task you were shown, even if a better title match appears in between.
- `undo_state` already holds the pre-change row, so undo is a feature to add, not a
  schema change.

Meeting action items are a harder line: the agent has no tool that creates tasks from
them. `suggest_tasks` returns proposals and says to review them in the app.

## Swapping models

`WC_TRANSCRIPTION_PROVIDER` and `WC_LLM_PROVIDER` select an implementation of
`TranscriptionProvider` / `LLMProvider`. Nothing outside `ai/providers/` knows which one
is in use.

| | |
|---|---|
| `faster_whisper` | local speech-to-text (default) |
| `ollama` | local LLM (default) |
| `gemini` | **opt-in.** Sends transcripts to Google. Logs a warning on startup. |
| `fake` | deterministic, for tests and bring-up |

Defaults are small on purpose: `tiny.en` (~75 MB) and `qwen2.5:0.5b` (~400 MB), so the
whole stack runs in about 2 GB. Summaries and action items hold up at that size; the
agent does not. DEPLOY.md has the size/quality ladder and what each step buys.

## Tests

```
.venv/bin/pytest
```

50 tests, no Postgres and no models required — they run on SQLite with the `fake`
providers, and exercise the real job queue and the real worker pipeline.

`tests/test_contract.py` asserts against JSON generated by the macOS client's own
encoder (`tests/fixtures/`, copied over by `scripts/sync-contract-fixtures.sh` in the
client repo). If either side's types drift, those tests fail rather than the app
silently sending a field the server ignores.
