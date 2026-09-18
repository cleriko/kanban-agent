from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Point everything at throwaway resources before the app imports its settings.
_TMP = Path(tempfile.mkdtemp(prefix="workconsole-tests-"))
os.environ.update(
    WC_DATABASE_URL=f"sqlite+aiosqlite:///{_TMP / 'test.db'}",
    WC_STORAGE_BACKEND="local",
    WC_STORAGE_PATH=str(_TMP / "objects"),
    WC_TRANSCRIPTION_PROVIDER="fake",
    WC_LLM_PROVIDER="fake",
    WC_API_TOKEN="",
    WC_JOB_POLL_SECONDS="0.01",
)

from app.ai.gateway import get_gateway  # noqa: E402
from app.ai.providers.fake import FakeLLMProvider, FakeTranscriptionProvider  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.infrastructure.storage import get_storage  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest_asyncio.fixture(autouse=True)
async def clean_database() -> AsyncIterator[None]:
    """A fresh schema per test. These are small tables; correctness beats speed here."""
    engine = db_session.engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    async with db_session.session_scope() as value:
        yield value


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    provider = FakeLLMProvider()
    get_gateway().override(llm=provider, transcription=FakeTranscriptionProvider())
    return provider


@pytest.fixture
def storage():
    return get_storage()


@pytest.fixture
def api() -> str:
    return "/api/v1"
