"""Gemini adapter (REST ``generateContent`` via httpx).

This is an adapter module: the only place in ``warden/`` allowed to import httpx
(CLAUDE.md rule 7, enforced by tests/test_dependencies.py). Nothing imports it
unless a real model is wanted, so the core and replay demos run without httpx.

Key handling: the key travels in the ``x-goog-api-key`` header (never the URL, which
shows up in error messages), is scrubbed from every error we raise, is not chained
into exceptions, and is hidden from ``repr``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from warden.llm.base import LLMError, LLMRequest, LLMResponse, LLMTransientError
from warden.llm.env import read_env_value

_OK_FINISH = {None, "STOP", "MAX_TOKENS"}


class GeminiClient:
    name = "gemini"
    DEFAULT_MODEL = "gemini-3.8-flash"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        timeout: float = 60.0,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Gemini API key is empty")
        self._key = api_key
        self.model = model
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                  headers={"x-goog-api-key": api_key})

    @classmethod
    def from_env(cls, env_file: str | Path = ".env", environ: Mapping[str, str] | None = None,
                 **kwargs: Any) -> GeminiClient:
        key = read_env_value("GEMINI_API_KEY", env_file, environ)
        if not key:
            raise LLMError(f"GEMINI_API_KEY is not set (environment or {env_file}); "
                           "use the replay backend to run offline")
        model = read_env_value("GEMINI_MODEL", env_file, environ) or cls.DEFAULT_MODEL
        return cls(key, model, **kwargs)

    def __repr__(self) -> str:
        return f"GeminiClient(model={self.model!r})"

    def close(self) -> None:
        self._http.close()

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***")

    def complete(self, request: LLMRequest) -> LLMResponse:
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": {"temperature": request.temperature},
        }
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        if request.json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        try:
            resp = self._http.post(f"/models/{self.model}:generateContent", json=body)
        except httpx.TransportError as exc:  # timeouts, DNS, connection resets
            raise LLMTransientError(self._scrub(f"gemini unreachable: {type(exc).__name__}: {exc}")) from None
        except httpx.HTTPError as exc:
            raise LLMError(self._scrub(f"gemini request failed: {type(exc).__name__}: {exc}")) from None
        if resp.status_code != 200:
            error = self._scrub(f"gemini HTTP {resp.status_code}: {self._error_message(resp)}")
            transient = resp.status_code == 429 or resp.status_code >= 500
            raise (LLMTransientError if transient else LLMError)(error)
        return self._parse(resp)

    @staticmethod
    def _error_message(resp: httpx.Response) -> str:
        try:
            return str(resp.json()["error"]["message"])
        except (ValueError, KeyError, TypeError):
            return resp.reason_phrase

    def _parse(self, resp: httpx.Response) -> LLMResponse:
        try:
            data = resp.json()
        except ValueError:
            raise LLMError("gemini returned a non-JSON body") from None
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"gemini returned no output: {reason}")
        first = candidates[0]
        finish = first.get("finishReason")
        if finish not in _OK_FINISH:
            raise LLMError(f"gemini stopped early: {finish}")
        parts = (first.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            raise LLMError("gemini returned empty text")
        usage = data.get("usageMetadata") or {}
        return LLMResponse(text, self.model, self.name,
                           usage.get("promptTokenCount"), usage.get("candidatesTokenCount"))
