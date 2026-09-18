from __future__ import annotations

import json
import logging
from typing import Any

from .base import LLMProvider, ProviderError
from .ollama import _extract_json

log = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    """Optional cloud LLM.

    Off by default and never selected implicitly: choosing it sends transcripts and
    task data to Google. It exists so the LLMProvider seam is real and so the
    provider can be switched without touching the workers or the Mac app. Set
    WC_LLM_PROVIDER=gemini and WC_GEMINI_API_KEY to opt in.
    """

    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash",
                 *, temperature: float = 0.1, timeout: float = 120.0) -> None:
        if not api_key:
            raise ProviderError("WC_GEMINI_API_KEY is required when the gemini provider is selected")
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._timeout = timeout
        self._client: Any = None

    def _chat(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:
            raise ProviderError(
                "langchain-google-genai is not installed. Install the 'gemini' extra: "
                "pip install '.[gemini]'"
            ) from exc
        log.warning("gemini provider selected: transcript content will be sent to Google")
        self._client = ChatGoogleGenerativeAI(
            model=self._model, google_api_key=self._api_key,
            temperature=self._temperature, timeout=self._timeout, max_retries=2)
        return self._client

    async def generate(self, prompt: str, *, system: str | None = None,
                       temperature: float | None = None) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = ([SystemMessage(content=system)] if system else []) + [HumanMessage(content=prompt)]
        try:
            response = await self._chat().ainvoke(messages)
        except Exception as exc:  # noqa: BLE001 - surface provider failures uniformly
            raise ProviderError(f"gemini request failed: {exc}") from exc
        content = response.content
        if isinstance(content, list):
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part)
                              for part in content)
        return str(content).strip()

    async def generate_structured(self, prompt: str, schema: dict[str, Any], *,
                                  system: str | None = None,
                                  temperature: float | None = None) -> dict[str, Any]:
        instruction = (
            f"{prompt}\n\nReturn only JSON matching this schema, with no commentary:\n"
            f"{json.dumps(schema)}"
        )
        raw = await self.generate(instruction, system=system, temperature=temperature)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            salvaged = _extract_json(raw)
            if salvaged is not None:
                return salvaged
            raise ProviderError(f"gemini did not return JSON: {raw[:300]}")

    async def health(self) -> bool:
        return bool(self._api_key)
