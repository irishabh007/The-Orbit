"""The LLM contract every backend implements.

Backends never retry internally: they classify failures (``LLMTransientError`` for
rate limits, timeouts and server errors; plain ``LLMError`` otherwise) and the
orchestrator's bounded retry/fallback policy decides what happens next.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Protocol

from warden.agent import PermanentAgentError


class LLMError(Exception):
    """The model call failed and retrying the same request will not help."""


class LLMTransientError(LLMError):
    """Rate limit, timeout, network or server error: a later retry may succeed."""


class LLMQuotaExceeded(LLMError, PermanentAgentError):
    """Quota exhausted for longer than is worth waiting (e.g. a daily free-tier limit).

    Deliberately NOT transient: retrying before ``retry_after`` seconds only burns attempts.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMReplayMiss(LLMError, PermanentAgentError):
    """Replay mode found no recording for this request (no network call is made)."""


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    system: str = ""
    temperature: float = 0.0
    json_mode: bool = False  # ask the backend for a JSON object

    def key(self) -> str:
        """Stable hash of everything that shapes the answer; names replay recordings."""
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    backend: str  # "gemini" | "replay" | "scripted"
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None  # billed as output by thinking models


class LLMClient(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...
