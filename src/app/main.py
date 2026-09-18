from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .ai.gateway import get_gateway
from .api import agent, jobs, meetings, tasks
from .api.schemas import HealthDTO
from .config import get_settings
from .db.session import create_all, dispose, run_migrations, session_scope
from .infrastructure import logging as log_config

VERSION = "1.0.0"
log = logging.getLogger("workconsole")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log_config.configure(settings.log_level)

    if settings.is_sqlite:
        # Convenience for local runs and tests; Postgres uses Alembic.
        await create_all()
    elif settings.run_migrations:
        try:
            await run_migrations()
            log.info("migrations applied")
        except Exception as exc:  # noqa: BLE001
            # Do not take the process down: /health will report the database as
            # unreachable, which is the accurate signal.
            log.error("could not apply migrations: %s", exc)

    log.info("work console API %s up · db=%s · storage=%s · stt=%s · llm=%s · auth=%s",
             VERSION, _host_of(settings.database_url), settings.storage_backend,
             settings.transcription_provider, settings.llm_provider,
             "on" if settings.auth_enabled else "OFF")

    # Fail loudly at boot rather than leaving someone to work it out from a
    # SQLAlchemy traceback on the first health probe.
    if _in_container() and _host_of(settings.database_url).startswith(("localhost", "127.0.0.1")):
        log.error("WC_DATABASE_URL is not set. Falling back to localhost, which inside a "
                  "container is the container itself — nothing will work. Set it in the "
                  "environment (compose points it at the 'postgres' service).")
    if not settings.auth_enabled:
        log.warning("WC_API_TOKEN is empty: the API is unauthenticated. Only acceptable "
                    "behind a private tunnel.")
    if settings.uses_gemini:
        sent = []
        if settings.transcription_provider == "gemini":
            sent.append("meeting audio")
        if settings.llm_provider == "gemini":
            sent.append("transcripts and task data")
        log.warning("gemini is enabled: %s will be sent to Google", " and ".join(sent))
        if not settings.gemini_api_key:
            log.error("WC_GEMINI_API_KEY is empty; every request to Gemini will fail.")
    yield
    await dispose()


app = FastAPI(title="Work Console", version=VERSION, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")

_settings = get_settings()
if _settings.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=_settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])

API = "/api/v1"
app.include_router(tasks.router, prefix=API)
app.include_router(tasks.summary_router, prefix=API)
app.include_router(meetings.router, prefix=API)
app.include_router(jobs.router, prefix=API)
app.include_router(agent.router, prefix=API)


# Health failures are logged once, then counted. A container healthcheck probes
# every 30s and an unreachable database would otherwise emit a full SQLAlchemy
# traceback each time, burying everything else in the log.
_health_failures = 0


@app.get(f"{API}/health", response_model=HealthDTO, tags=["meta"])
async def health(response: Response) -> HealthDTO:
    """Unauthenticated: the Mac app probes this to show CONNECTED before it has
    anything to send, and a tunnel needs to be checkable without a key.

    Returns 503 when the database is unreachable. A backend that cannot reach its
    database is not healthy, and reporting 200 here would make the container
    healthcheck pass while nothing actually worked.
    """
    global _health_failures
    settings = get_settings()

    database_ok = True
    detail: str | None = None
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        if _health_failures:
            log.info("database reachable again after %d failed checks", _health_failures)
            _health_failures = 0
    except Exception as exc:  # noqa: BLE001
        database_ok = False
        detail = _connection_hint(settings.database_url, exc)
        if _health_failures == 0:
            log.error("database unreachable: %s", detail)
            log.debug("health check traceback", exc_info=True)
        _health_failures += 1

    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthDTO(
        status="ok" if database_ok else "degraded",
        version=VERSION,
        database=database_ok,
        storage=settings.storage_backend,
        transcription=settings.transcription_provider,
        llm=settings.llm_provider,
        detail=detail,
    )


def _connection_hint(url: str, exc: Exception) -> str:
    """Turns a connection failure into something actionable.

    The usual cause is WC_DATABASE_URL never being set, leaving the built-in
    localhost default — which inside a container points at the container itself.
    """
    host = _host_of(url)
    base = f"cannot connect to Postgres at {host}: {type(exc).__name__}"
    if host.startswith(("localhost", "127.0.0.1")) and _in_container():
        return (base + ". WC_DATABASE_URL is unset, so the built-in localhost default "
                "is being used — inside a container that is the container itself. Set "
                "WC_DATABASE_URL (compose normally points it at the 'postgres' service).")
    return base


def _host_of(url: str) -> str:
    """Host:port from a SQLAlchemy URL, with any password stripped."""
    try:
        remainder = url.split("://", 1)[1]
        authority = remainder.split("/", 1)[0]
        return authority.rsplit("@", 1)[-1] or "?"
    except (IndexError, AttributeError):
        return "?"


def _in_container() -> bool:
    return Path("/.dockerenv").exists()


@app.get("/health", include_in_schema=False)
async def health_alias(response: Response) -> HealthDTO:
    return await health(response)


@app.exception_handler(HTTPException)
async def http_exception(_request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code,
                        content={"error": _code(exc.status_code), "detail": exc.detail})


def _code(status: int) -> str:
    return {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
            409: "conflict", 422: "unprocessable", 503: "unavailable"}.get(status, "error")
