from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration is environment-driven so one image runs anywhere.

    Defaults are chosen so that a fresh VPS runs entirely on its own hardware: local
    Whisper for speech, a local LLM over Ollama for reasoning, Postgres and the
    filesystem for storage. No request leaves the machine unless you change these.
    """

    model_config = SettingsConfigDict(env_file=".env", env_prefix="WC_", extra="ignore")

    # --- API ---------------------------------------------------------------
    # Run `alembic upgrade head` on API startup. Off by default because a
    # multi-replica deployment should migrate as a separate step, but it removes
    # the one manual command when the API is a single container.
    run_migrations: bool = False

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"
    # Bearer token the macOS app sends. Empty disables auth — only acceptable
    # behind a tunnel on a trusted network.
    api_token: str = ""
    cors_origins: list[str] = []

    # --- Database ----------------------------------------------------------
    # Postgres in production. SQLite is supported so the suite runs without a server.
    database_url: str = "postgresql+asyncpg://workconsole:workconsole@localhost:5432/workconsole"

    # --- Object storage ----------------------------------------------------
    # Audio never goes in Postgres. "local" is a directory on the VPS; "s3" targets
    # MinIO or any S3-compatible endpoint.
    storage_backend: Literal["local", "s3"] = "local"
    storage_path: Path = Path("/var/lib/workconsole/objects")
    s3_endpoint: str = ""
    s3_bucket: str = "workconsole"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"

    # --- Jobs --------------------------------------------------------------
    job_poll_seconds: float = 2.0
    job_claim_batch: int = 1
    job_max_attempts: int = 3
    # A job claimed by a worker that then died is released after this long.
    job_lease_seconds: int = 1800

    # --- Transcription -----------------------------------------------------
    transcription_provider: Literal["faster_whisper", "fake"] = "faster_whisper"
    # tiny.en is ~75 MB as int8 and transcribes clear speech well enough for notes
    # and action items. Move up to base.en / small.en if accents, crosstalk or poor
    # microphones start costing you words.
    whisper_model: str = "tiny.en"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    whisper_language: str = "en"
    # Word-level timing costs time; segment timing is enough for a transcript view.
    whisper_vad_filter: bool = True

    # --- LLM ---------------------------------------------------------------
    # "ollama" runs a model on the VPS. "gemini" is opt-in and sends transcripts to
    # Google — it exists so the provider can be swapped, and is never the default.
    llm_provider: Literal["ollama", "gemini", "fake"] = "ollama"
    ollama_url: str = "http://localhost:11434"
    # ~400 MB at Q4. Small enough for a modest VPS, and schema-constrained decoding
    # keeps its JSON valid. Quality of the *content* scales with size — see DEPLOY.md.
    ollama_model: str = "qwen2.5:0.5b"
    llm_temperature: float = 0.1
    llm_timeout: float = 300.0
    # Small models are the ones that actually run out of context. 4k keeps memory
    # down; raise it alongside the model.
    llm_num_ctx: int = 4096
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # --- Agent -------------------------------------------------------------
    agent_max_iterations: int = 6
    # Destructive operations are proposed to the client rather than executed.
    agent_confirm_destructive: bool = True
    # How long a proposed operation stays executable.
    agent_action_ttl_seconds: int = 900

    # --- Analysis ----------------------------------------------------------
    # Long transcripts are summarised in chunks before the final pass. Sized to sit
    # comfortably inside llm_num_ctx with the prompt: ~6k chars is ~1.5k tokens.
    analysis_chunk_chars: int = 6_000

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_token)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
