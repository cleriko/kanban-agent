from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .base import LLMProvider, ProviderError

log = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    """Local LLM over Ollama's HTTP API.

    The default reasoning engine: the model runs on the VPS, so transcripts never
    leave the machine. Structured output uses Ollama's ``format`` parameter, which
    constrains generation to the JSON schema instead of hoping the model complies.
    """

    name = "ollama"

    def __init__(self, base_url: str, model: str, *, temperature: float = 0.1,
                 timeout: float = 300.0, num_ctx: int = 8192) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._temperature = temperature
        self._timeout = timeout
        self._num_ctx = num_ctx

    async def _chat(self, messages: list[dict[str, str]], *, temperature: float | None,
                    fmt: Any | None = None) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self._temperature if temperature is None else temperature,
                "num_ctx": self._num_ctx,
            },
        }
        if fmt is not None:
            payload["format"] = fmt

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(f"{self._base}/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"cannot reach the local LLM at {self._base}: {exc}. "
                "Is Ollama running, and has the model been pulled?"
            ) from exc

        if response.status_code == 404:
            raise ProviderError(
                f"model '{self._model}' is not available. Run: ollama pull {self._model}")
        if response.status_code >= 400:
            raise ProviderError(f"ollama returned {response.status_code}: {response.text[:300]}")

        body = response.json()
        content = (body.get("message") or {}).get("content", "")
        if not content:
            raise ProviderError("the local LLM returned an empty response")
        return content

    async def generate(self, prompt: str, *, system: str | None = None,
                       temperature: float | None = None) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        return await self._chat(messages, temperature=temperature)

    async def generate_structured(self, prompt: str, schema: dict[str, Any], *,
                                  system: str | None = None,
                                  temperature: float | None = None) -> dict[str, Any]:
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        raw = await self._chat(messages, temperature=temperature, fmt=schema)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Some models wrap JSON in prose even with a schema; salvage the object.
            salvaged = _extract_json(raw)
            if salvaged is not None:
                return salvaged
            raise ProviderError(f"the local LLM did not return JSON: {raw[:300]}")

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self._base}/api/tags")
            return response.status_code == 200
        except httpx.HTTPError:
            return False


def _extract_json(raw: str) -> dict[str, Any] | None:
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
