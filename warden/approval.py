"""Human approval gateway interface.

The default approver rejects everything: with no human wired in, nothing that
needs approval can proceed. Fail closed, not open.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from warden.bundle import BundleError, unpack

Verdict = Literal["approve", "reject", "changes"]


@dataclass(frozen=True)
class ApprovalRequest:
    run_id: str
    node_id: str
    summary: str
    artifacts: tuple[str, ...]
    reason: str  # why a human is needed (node policy, or a policy escalation)
    # (artifact, readable excerpt) of the staged outputs, so the human sees what they approve.
    # Built after the post-run policy passed (no secrets); shown to the human, never logged.
    preview: tuple[tuple[str, str], ...] = ()


_PREVIEW_CHARS = 6000


def preview_outputs(outputs: Mapping[str, str | bytes]) -> tuple[tuple[str, str], ...]:
    """Readable excerpts: bundles as a file list with line counts, other text truncated."""
    previews = []
    for name in sorted(outputs):
        content = outputs[name]
        text = content if isinstance(content, str) else content.decode("utf-8", errors="replace")
        try:
            files = unpack(text)
        except BundleError:
            excerpt = text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "\n... (truncated)"
        else:
            excerpt = "\n".join(f"{path} ({len(body.splitlines())} lines)" for path, body in sorted(files.items()))
        previews.append((name, excerpt))
    return tuple(previews)


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
