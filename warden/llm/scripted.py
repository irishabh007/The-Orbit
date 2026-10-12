"""Scripted backend: fixed answers (or exceptions) in order. For tests and failure demos."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from warden.llm.base import LLMError, LLMRequest, LLMResponse


class ScriptedClient:
    name = "scripted"

    def __init__(self, script: Iterable[str | Exception], default: str | None = None) -> None:
        self._script = list(script)
        self._default = default
        self._lock = threading.Lock()  # parallel agents may share one client
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            self.calls.append(request)
            if self._script:
                item: str | Exception | None = self._script.pop(0)
            else:
                item = self._default
        if item is None:
            raise LLMError("scripted client has no responses left")
        if isinstance(item, Exception):
            raise item
        return LLMResponse(item, model="scripted", backend=self.name)
