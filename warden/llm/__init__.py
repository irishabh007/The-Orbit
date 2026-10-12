"""LLM layer: one ``LLMClient`` protocol, three backends.

* ``ScriptedClient``      fixed answers/exceptions (tests, failure demos)
* ``RecordReplayClient``  offline replay of recorded calls (reviewers need no key)
* ``GeminiClient``        real model; import ``warden.llm.gemini`` explicitly, since it
                          is the one module that needs httpx
"""

from warden.llm.base import (
    LLMClient,
    LLMError,
    LLMReplayMiss,
    LLMRequest,
    LLMResponse,
    LLMTransientError,
)
from warden.llm.env import read_env_value
from warden.llm.replay import RecordReplayClient
from warden.llm.scripted import ScriptedClient

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMReplayMiss",
    "LLMRequest",
    "LLMResponse",
    "LLMTransientError",
    "RecordReplayClient",
    "ScriptedClient",
    "read_env_value",
]
