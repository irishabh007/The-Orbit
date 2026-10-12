"""Human approval gateway interface.

The default approver rejects everything: with no human wired in, nothing that
needs approval can proceed. Fail closed, not open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Verdict = Literal["approve", "reject", "changes"]


@dataclass(frozen=True)
class ApprovalRequest:
    run_id: str
    node_id: str
    summary: str
    artifacts: tuple[str, ...]
    reason: str  # why a human is needed (node policy, or a policy escalation)


@dataclass(frozen=True)
class ApprovalDecision:
    verdict: Verdict
    approver: str
    comment: str = ""


class Approver(Protocol):
    def decide(self, request: ApprovalRequest) -> ApprovalDecision: ...


class RejectAllApprover:
    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision("reject", "system", "no approver configured")


class ScriptedApprover:
    """Replays a fixed list of decisions per node (used for tests and replayable demos)."""

    def __init__(
        self,
        script: dict[str, list[ApprovalDecision]] | None = None,
        default: ApprovalDecision | None = None,
    ) -> None:
        self._script = {k: list(v) for k, v in (script or {}).items()}
        self._default = default or ApprovalDecision("approve", "scripted")
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        queue = self._script.get(request.node_id)
        return queue.pop(0) if queue else self._default
