"""Agent contract. An agent is a pure-ish function of its context to staged outputs.

Agents never write to the artifact store themselves: they *return* outputs and the
orchestrator validates (gates, policy, approval) before committing. That makes
"discard on failure" free and keeps all side effects in one governed place.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from warden.artifacts import ArtifactStore


@dataclass
class AgentResult:
    outputs: dict[str, str | bytes]  # artifact name -> content
    kind: str = "text"  # requirements | design | code | test | doc | ...
    summary: str = ""  # shown to humans in approval requests


@dataclass
class AgentContext:
    run_id: str
    node_id: str
    attempt: int
    store: ArtifactStore
    consumes: tuple[str, ...]
    feedback: str | None = None  # human "changes requested" comment, if any
    extras: dict[str, object] = field(default_factory=dict)

    def read_text(self, name: str) -> str:
        art = self.store.latest(name)
        if art is None:
            raise KeyError(f"missing input artifact: {name}")
        return self.store.read_text(art)


class Agent(Protocol):
    name: str

    def run(self, ctx: AgentContext) -> AgentResult: ...


class FunctionAgent:
    """Wrap a plain function as an agent (used for scripted and test agents)."""

    def __init__(self, name: str, fn: Callable[[AgentContext], AgentResult]) -> None:
        self.name = name
        self._fn = fn

    def run(self, ctx: AgentContext) -> AgentResult:
        return self._fn(ctx)
