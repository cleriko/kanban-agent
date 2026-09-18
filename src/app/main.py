from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .ai.gateway import get_gateway
from .api import agent, jobs, meetings, tasks
from .api.schemas import HealthDTO
from .config import get_settings
from .db.session import create_all, dispose, session_scope
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

    log.info("work console API %s up · storage=%s · stt=%s · llm=%s · auth=%s",
             VERSION, settings.storage_backend, settings.transcription_provider,
             settings.llm_provider, "on" if settings.auth_enabled else "OFF")
    if settings.llm_provider == "gemini":
        log.warning("LLM provider is gemini: transcripts will be sent to Google")
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


@app.get(f"{API}/health", response_model=HealthDTO, tags=["meta"])
async def health() -> HealthDTO:
    """Unauthenticated: the Mac app probes this to show CONNECTED before it has
    anything to send, and a tunnel needs to be checkable without a key."""
    settings = get_settings()
    database_ok = True
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        database_ok = False
        log.exception("health check: database unreachable")

    return HealthDTO(
        status="ok" if database_ok else "degraded",
        version=VERSION,
        database=database_ok,
        storage=settings.storage_backend,
        transcription=settings.transcription_provider,
        llm=settings.llm_provider,
    )


@app.get("/health", include_in_schema=False)
async def health_alias() -> HealthDTO:
    return await health()


@app.exception_handler(HTTPException)
async def http_exception(_request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code,
                        content={"error": _code(exc.status_code), "detail": exc.detail})


def _code(status: int) -> str:
    return {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
            409: "conflict", 422: "unprocessable", 503: "unavailable"}.get(status, "error")
