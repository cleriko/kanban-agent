# Deploying on Dokploy

## The one thing that matters

Deploy this as a **Docker Compose** application, not as an Application/Dockerfile.

The repo is four services — `api`, `worker`, `postgres`, `ollama` — and
`docker-compose.yml` creates all of them. If Dokploy builds the `Dockerfile`
directly, you get the API container on its own with no database and no model
server, and it will sit there failing its healthcheck forever. The startup log
tells you when this has happened:

```
work console API 1.0.0 up · db=localhost:5432 · ...
ERROR  WC_DATABASE_URL is not set. Falling back to localhost ...
GET /health 503 Service Unavailable
```

`db=localhost:5432` means the compose file is not in play. When it is, that line
reads `db=postgres:5432` — compose sets it with a working default even if you
paste nothing into the Environment tab.

## Setup

**1. Create the application**

Dokploy → Create → **Compose**. Point it at `https://github.com/cleriko/kanban-worker`,
branch `main`. Leave the compose path as `docker-compose.yml`.

**2. Environment**

Paste `.env.example` into the Environment tab and fill in the two blanks:

```
WC_API_TOKEN=          # openssl rand -hex 24
POSTGRES_PASSWORD=     # openssl rand -hex 16
```

`scripts/generate-env.sh` will fill them locally and print the result.

Do **not** set `WC_DATABASE_URL`, `WC_OLLAMA_URL` or `WC_STORAGE_PATH` — compose
sets those to the internal service names, and overriding them by hand is the
usual way to break it.

**3. Deploy**, then run the two one-off commands:

```
docker compose run --rm api alembic upgrade head
docker compose exec ollama ollama pull llama3.1:8b
```

Migrations do not run on boot, and the LLM does nothing until a model is pulled.
The first pull is a few GB.

**4. Domain**

Attach your domain to the **`api`** service on port **8080**. That is the only
port the stack exposes; Postgres and Ollama stay on the internal network and are
not reachable from outside.

**5. Check it**

```
curl https://your-domain/health
```

Expect `200` and `"database": true`. A `503` means Postgres is unreachable — the
`detail` field says why.

**6. Point the Mac app at it**

Settings → VPS → Server URL = `https://your-domain`, API key = your
`WC_API_TOKEN`. Press CONNECT; the badge should read CONNECTED.

## Resources

The worker holds the Whisper model in memory while transcribing, and Ollama holds
the LLM. On CPU with the defaults (`base.en`, `llama3.1:8b`) allow roughly:

| | |
|---|---|
| postgres | 256 MB |
| api | 256 MB |
| worker | 2–6 GB while transcribing, idle otherwise |
| ollama | 6–8 GB with an 8B model loaded |

A 2 GB VPS will not run this. 8 GB is a sensible floor; 16 GB is comfortable. If
the box is small, use `WC_WHISPER_MODEL=tiny.en` and a 3B model such as
`llama3.2:3b`.

Transcription is much faster with a GPU. Uncomment the `deploy.resources` block
on the `ollama` service and set `WC_WHISPER_DEVICE=cuda`,
`WC_WHISPER_COMPUTE_TYPE=float16`.

## If you would rather use Dokploy's managed Postgres

Also fine. Create a Postgres database in Dokploy, then set `WC_DATABASE_URL`
explicitly in the Environment tab:

```
WC_DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname
```

Note the `+asyncpg` — the driver is async, and a plain `postgresql://` URL will
fail at startup. You can then delete the `postgres` service from the compose file.

## Troubleshooting

**`db=localhost:5432` in the startup log** — the compose file is not being used.
See the top of this page.

**`503` on `/health`** — read the `detail` field. Usually Postgres has not
finished starting, or `WC_DATABASE_URL` points somewhere wrong.

**Health passes but processing never finishes** — check the `worker` container.
It is a separate process and will not appear in the API's logs. `ollama pull` is
the usual omission.

**`model 'llama3.1:8b' is not available`** — step 3.

**The app says OFFLINE but curl works** — the API key in Settings does not match
`WC_API_TOKEN`. `/health` is unauthenticated, so curl succeeds while everything
else returns 401.
