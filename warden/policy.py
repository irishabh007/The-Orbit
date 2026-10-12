"""Policy guardrail interface. Rules (YAML) plug in here in a later part.

Checked twice per attempt: before the agent runs (``check_pre``) and on its staged
outputs before anything is committed (``check_post``). A denial is never retried:
retrying a policy violation just burns budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from warden.agent import AgentContext, AgentResult
from warden.dag import NodeSpec


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool = True
    requires_approval: bool = False  # escalate to a human even if the node is "auto"
    rule: str = ""
    reason: str = ""


class Policy(Protocol):
    def check_pre(self, node: NodeSpec, ctx: AgentContext) -> PolicyDecision: ...

    def check_post(self, node: NodeSpec, result: AgentResult) -> PolicyDecision: ...


class AllowAllPolicy:
    def check_pre(self, node: NodeSpec, ctx: AgentContext) -> PolicyDecision:
        return PolicyDecision()

    def check_post(self, node: NodeSpec, result: AgentResult) -> PolicyDecision:
        return PolicyDecision()
