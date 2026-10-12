"""Bounded exponential backoff for transient model errors (rate limits, 503 "high demand").

The orchestrator's own retries are immediate, which does not help against a busy
API. This wrapper waits between attempts, but only for ``LLMTransientError``:
permanent errors fail at once, and the number of attempts is always bounded.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from warden.llm.base import LLMClient, LLMRequest, LLMResponse, LLMTransientError


class RetryingClient:
    def __init__(self, inner: LLMClient, *, attempts: int = 4, base_delay: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        self.inner = inner
        self.name = inner.name
        self.attempts = attempts
        self.base_delay = base_delay
        self._sleep = sleep

    def complete(self, request: LLMRequest) -> LLMResponse:
        for attempt in range(self.attempts):
            try:
                return self.inner.complete(request)
            except LLMTransientError:
                if attempt == self.attempts - 1:
                    raise
                self._sleep(self.base_delay * 2**attempt)
        raise AssertionError("unreachable")  # pragma: no cover
