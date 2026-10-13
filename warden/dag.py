"""Explicit dependency graph with entry/exit gates.

Validation happens at construction, so an invalid workflow can never start:
unique ids, known dependencies, no cycles, every artifact produced exactly once,
and every consumed artifact either external (seeded by a human) or produced by an
*ancestor* node. That last rule is what makes parallel branches safe.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from warden.agent import AgentResult
from warden.artifacts import ArtifactStore


class GraphError(ValueError):
    """The workflow definition is invalid."""


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str = ""
    # on failure: which of the node's ``on_failure_replan`` targets are at fault (empty: all)
    replan: tuple[str, ...] = ()


EntryGate = Callable[[ArtifactStore], GateResult]
ExitGate = Callable[[AgentResult], GateResult]


@dataclass(frozen=True)
class NodeSpec:
    id: str
    agent: str
    deps: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    requires_approval: bool = False
    max_retries: int = 2  # retries after the first attempt
    fallback_agent: str | None = None
    entry_gate: EntryGate | None = None
    exit_gate: ExitGate | None = None
    # When retries/fallback are exhausted, instead of halting: send the failure reason
    # as feedback to these upstream nodes and re-run them and everything below them
    # (e.g. verify -> developer). Bounded by max_replans; never for policy denials or
    # human rejections.
    on_failure_replan: tuple[str, ...] = ()
    max_replans: int = 1


class Graph:
    def __init__(self, nodes: Iterable[NodeSpec]) -> None:
        self.nodes: dict[str, NodeSpec] = {}
        for n in nodes:
            if n.id in self.nodes:
                raise GraphError(f"duplicate node id: {n.id}")
            self.nodes[n.id] = n
        if not self.nodes:
            raise GraphError("graph has no nodes")
        for n in self.nodes.values():
            for d in n.deps:
                if d == n.id:
                    raise GraphError(f"{n.id} depends on itself")
                if d not in self.nodes:
                    raise GraphError(f"{n.id} depends on unknown node {d}")
        self.order = self._toposort()
        self.producer: dict[str, str] = {}
        for n in self.nodes.values():
            for art in n.produces:
                if art in self.producer:
                    raise GraphError(f"artifact {art} produced by both {self.producer[art]} and {n.id}")
                self.producer[art] = n.id
        for n in self.nodes.values():
            self._check_upstream_refs(n)

    def _check_upstream_refs(self, n: NodeSpec) -> None:
        """Consumed artifacts and re-plan targets must come from ancestors (parallel safety)."""
        upstream = self.ancestors(n.id)
        for art in n.consumes:
            src = self.producer.get(art)
            if src is not None and src not in upstream:
                raise GraphError(f"{n.id} consumes {art} but {src} is not an upstream dependency")
        for target in n.on_failure_replan:
            if target not in upstream:
                raise GraphError(f"{n.id} can only re-plan upstream nodes, {target} is not upstream")

    def _toposort(self) -> list[str]:
        indegree = {i: len(n.deps) for i, n in self.nodes.items()}
        ready = [i for i in self.nodes if indegree[i] == 0]  # insertion order = stable
        order: list[str] = []
        while ready:
            cur = ready.pop(0)
            order.append(cur)
            for i, n in self.nodes.items():
                if cur in n.deps:
                    indegree[i] -= 1
                    if indegree[i] == 0:
                        ready.append(i)
        if len(order) != len(self.nodes):
            stuck = sorted(set(self.nodes) - set(order))
            raise GraphError(f"cycle detected involving: {stuck}")
        return order

    def ancestors(self, node_id: str) -> set[str]:
        out: set[str] = set()
        stack = list(self.nodes[node_id].deps)
        while stack:
            cur = stack.pop()
            if cur not in out:
                out.add(cur)
                stack.extend(self.nodes[cur].deps)
        return out

    def descendants(self, node_id: str) -> set[str]:
        return {i for i in self.nodes if node_id in self.ancestors(i)}

    def levels(self) -> list[list[str]]:
        """Nodes grouped by depth; everything in one level may run in parallel."""
        depth: dict[str, int] = {}
        for i in self.order:
            depth[i] = 1 + max((depth[d] for d in self.nodes[i].deps), default=-1)
        grouped: dict[int, list[str]] = {}
        for i in self.order:
            grouped.setdefault(depth[i], []).append(i)
        return [grouped[k] for k in sorted(grouped)]

    def to_mermaid(self, states: dict[str, str] | None = None) -> str:
        lines = ["graph TD"]
        for i, n in self.nodes.items():
            label = f"{i}<br/>{n.agent}" + (" 🔒" if n.requires_approval else "")
            if states:
                label += f"<br/>{states.get(i, '')}"
            lines.append(f'    {i}["{label}"]')
        for i, n in self.nodes.items():
            for d in n.deps:
                lines.append(f"    {d} --> {i}")
        return "\n".join(lines)
