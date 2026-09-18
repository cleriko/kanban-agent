from __future__ import annotations

import logging
from functools import lru_cache

from ..config import Settings, get_settings
from .providers.base import LLMProvider, TranscriptionProvider

log = logging.getLogger(__name__)


class AIGateway:
    """The only place the rest of the backend touches a model.

    Services and workers depend on this, not on faster-whisper or Ollama, so the
    model stack can change without touching business logic. Providers are created
    lazily: importing the API server should never load a model.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._transcription: TranscriptionProvider | None = None
        self._llm: LLMProvider | None = None

    @property
    def transcription(self) -> TranscriptionProvider:
        if self._transcription is None:
            self._transcription = self._build_transcription()
        return self._transcription

    @property
    def llm(self) -> LLMProvider:
        if self._llm is None:
            self._llm = self._build_llm()
        return self._llm

    def _build_transcription(self) -> TranscriptionProvider:
        choice = self._settings.transcription_provider
        if choice == "fake":
            from .providers.fake import FakeTranscriptionProvider
            log.warning("using the fake transcription provider; no real speech recognition")
            return FakeTranscriptionProvider()

        from .providers.whisper_local import FasterWhisperProvider
        return FasterWhisperProvider(
            model_size=self._settings.whisper_model,
            device=self._settings.whisper_device,
            compute_type=self._settings.whisper_compute_type,
            vad_filter=self._settings.whisper_vad_filter,
        )

    def _build_llm(self) -> LLMProvider:
        choice = self._settings.llm_provider
        if choice == "fake":
            from .providers.fake import FakeLLMProvider
            log.warning("using the fake LLM provider; analysis output is canned")
            return FakeLLMProvider()

        if choice == "gemini":
            from .providers.gemini import GeminiProvider
            return GeminiProvider(
                api_key=self._settings.gemini_api_key,
                model=self._settings.gemini_model,
                temperature=self._settings.llm_temperature,
                timeout=self._settings.llm_timeout,
            )

        from .providers.ollama import OllamaProvider
        return OllamaProvider(
            base_url=self._settings.ollama_url,
            model=self._settings.ollama_model,
            temperature=self._settings.llm_temperature,
            timeout=self._settings.llm_timeout,
            num_ctx=self._settings.llm_num_ctx,
        )

    # Used for injecting fakes in tests without touching configuration.
    def override(self, *, transcription: TranscriptionProvider | None = None,
                 llm: LLMProvider | None = None) -> None:
        if transcription is not None:
            self._transcription = transcription
        if llm is not None:
            self._llm = llm

    async def health(self) -> dict[str, bool]:
        return {
            "transcription": await self.transcription.health(),
            "llm": await self.llm.health(),
        }


@lru_cache(maxsize=1)
def get_gateway(settings: Settings | None = None) -> AIGateway:
    return AIGateway(settings or get_settings())
