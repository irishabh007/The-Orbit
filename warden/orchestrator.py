"""The orchestration engine: a stateful scheduler over an explicit DAG.

Design rules (see CLAUDE.md):
* Only the scheduler thread mutates node state. Worker threads only run agents.
* Agents return *staged* outputs. Nothing reaches the artifact store until the exit
  gate, policy check and (if required) human approval have all passed. A failed or
  discarded attempt therefore has nothing to undo.
* Every state change is an event in the hash-chained log, linked to the events that
  caused it (decision lineage).
* Failure handling is bounded: retries -> fallback agent -> terminal rollback + halt.
* Safe-stop (kill switch or step budget) discards in-flight work and commits nothing.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from warden.agent import Agent, AgentContext, AgentResult, PermanentAgentError
from warden.approval import (
    ApprovalDecision,
    ApprovalRequest,
    Approver,
    RejectAllApprover,
)
from warden.artifacts import ArtifactStore
from warden.dag import Graph
from warden.eventlog import EventLog
from warden.fsutil import atomic_write
from warden.models import EventType, NodeState, RunState, assert_transition
from warden.policy import AllowAllPolicy, Policy

_WAIT = "__waiting__"  # sentinel: node simply waits on dependencies (not a block)
_ACTOR = "orchestrator"


@dataclass(frozen=True)
class Config:
    max_workers: int = 4
    step_budget: int = 100  # max agent executions per run() call (safe-stop beyond this)


@dataclass
class NodeRecord:
    state: NodeState = NodeState.PENDING
    attempts: int = 0
    used_fallback: bool = False
    held: bool = False  # rolled back by a human; will not auto-run until released
    committed: dict[str, dict[str, Any]] = field(default_factory=dict)  # artifact -> {version, prev}
    last_event: int | None = None
    replans: int = 0  # failure-driven re-plan loops used since this node last succeeded


@dataclass(frozen=True)
class RunSummary:
    state: RunState
    reason: str
    nodes: dict[str, str]
    steps: int
    blocked: dict[str, str]


class Orchestrator:
    def __init__(
        self,
        graph: Graph,
        agents: dict[str, Agent],
        store: ArtifactStore,
        log: EventLog,
        run_id: str,
        *,
        policy: Policy | None = None,
        approver: Approver | None = None,
        config: Config | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        self.graph, self.agents, self.store, self.log, self.run_id = graph, agents, store, log, run_id
        self.policy: Policy = policy or AllowAllPolicy()
        self.approver: Approver = approver or RejectAllApprover()
        self.config = config or Config()
        self._state_path = Path(state_path) if state_path else None
        if self._state_path is not None:  # once here, not on every save (hot path)
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
        for n in graph.nodes.values():
            for name in (n.agent, n.fallback_agent):
                if name is not None and name not in agents:
                    raise ValueError(f"node {n.id} references unknown agent {name!r}")
        self._rec: dict[str, NodeRecord] = {i: NodeRecord() for i in graph.nodes}
        self._feedback: dict[str, str] = {}
        self._blocked: dict[str, str] = {}
        self._steps = 0
        self._halt = False
        self._halt_reason = ""
        self._stopped = False
        self._stop_event = threading.Event()
        self._stop_reason = ""
        self._run_start = 0
        self._load()

    # ------------------------------------------------------------------ public
    def states(self) -> dict[str, str]:
        return {i: r.state.value for i, r in self._rec.items()}

    def stop(self, reason: str = "operator requested stop") -> None:
        """Kill switch. Safe to call from any thread, including inside an agent."""
        self._stop_reason = reason
        self._stop_event.set()

    def adopt(self, node_id: str, actor: str = "human") -> None:
        """Brownfield entry: treat a node as already done because its artifacts exist."""
        spec, rec = self.graph.nodes[node_id], self._rec[node_id]
        missing = [a for a in spec.produces if self.store.latest(a) is None]
        if rec.state != NodeState.PENDING or missing:
            raise ValueError(f"cannot adopt {node_id}: state={rec.state.value}, missing={missing}")
        rec.state = NodeState.SUCCEEDED
        rec.last_event = self._log(
            EventType.NODE_TRANSITION, node_id,
            {"from": "PENDING", "to": "SUCCEEDED", "adopted": True, "by": actor},
        )
        self._save()

    def replan(self, changed: Iterable[str], cause: int | None = None) -> list[str]:
        """An upstream artifact changed: invalidate only the finished nodes that consume it."""
        changed_set = set(changed)
        affected: list[str] = []
        for nid in self.graph.order:
            spec, rec = self.graph.nodes[nid], self._rec[nid]
            if rec.state == NodeState.SUCCEEDED and changed_set & set(spec.consumes):
                self._transition(nid, NodeState.STALE, reason="upstream artifact changed")
                self._transition(nid, NodeState.PENDING)
                rec.attempts, rec.used_fallback = 0, False
                affected.append(nid)
        if affected:
            self._log(
                EventType.REPLAN, None,
                {"changed": sorted(changed_set), "invalidated": affected},
                [cause] if cause else [],
            )
        return affected

    def rollback(self, node_id: str, reason: str, actor: str = "human") -> list[str]:
        """Revert a finished node's outputs to their previous versions and hold it."""
        rec = self._rec[node_id]
        if rec.state != NodeState.SUCCEEDED:
            raise ValueError(f"can only roll back a SUCCEEDED node, {node_id} is {rec.state.value}")
        reverted: list[str] = []
        for name, info in rec.committed.items():
            if info["prev"] is None:
                self.store.drop(name)
            else:
                self.store.restore(name, info["prev"], produced_by=f"rollback:{actor}")
            reverted.append(name)
        seq = self._transition(node_id, NodeState.ROLLED_BACK, reason=reason, by=actor)
        self._log(EventType.ROLLBACK, node_id, {"reverted": reverted, "reason": reason, "by": actor}, [seq])
        rec.held, rec.committed = True, {}
        self.replan(reverted, cause=seq)
        self._save()
        return reverted

    def release(self, node_id: str) -> None:
        rec = self._rec[node_id]
        if rec.state == NodeState.ROLLED_BACK:
            rec.held = False
            self._transition(node_id, NodeState.PENDING, reason="released by human")

    def run(self) -> RunSummary:
        self._halt, self._halt_reason, self._stopped = False, "", False
        self._stop_event.clear()
        self._steps = 0
        self._blocked.clear()
        self._normalize()
        start = self._run_start = self._log(EventType.RUN_STARTED, None, {"nodes": list(self.graph.order)})
        futures: dict[Future[tuple[AgentResult, int]], tuple[str, dict[str, str | None]]] = {}
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            while True:
                self._sync_stop()
                if not (self._halt or self._stopped):
                    self._launch_ready(pool, futures)
                    self._sync_stop()  # a step-budget stop raised while launching
                if not futures:
                    break
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for fut in done:
                    nid, snapshot = futures.pop(fut)
                    self._process(nid, fut, snapshot)
        summary = self._summarize()
        self._log(
            EventType.RUN_FINISHED, None,
            {"state": summary.state.value, "reason": summary.reason, "steps": summary.steps},
            [start],
        )
        return summary

    # --------------------------------------------------------------- internals
    def _log(self, type_: EventType, node_id: str | None, data: dict[str, Any],
             caused_by: Iterable[int] = (), actor: str = _ACTOR) -> int:
        ev = self.log.append(self.run_id, type_, actor, node_id, data, sorted(set(caused_by)))
        return ev.seq

    def _causes(self, nid: str) -> list[int]:
        out = [self._rec[d].last_event for d in self.graph.nodes[nid].deps]
        out.append(self._rec[nid].last_event)
        return [c for c in out if c]

    def _sync_stop(self) -> None:
        """Latch the kill switch exactly once and record it in the audit log."""
        if self._stop_event.is_set() and not self._stopped:
            self._stopped = True
            self._log(EventType.SAFE_STOP, None, {"reason": self._stop_reason},
                      [self._run_start] if self._run_start else [])

    def _own(self, nid: str) -> list[int]:
        last = self._rec[nid].last_event
        return [last] if last else []

    def _transition(self, nid: str, dst: NodeState, _extra: Iterable[int] = (), **data: Any) -> int:
        rec = self._rec[nid]
        assert_transition(rec.state, dst)
        src, rec.state = rec.state, dst
        seq = self._log(EventType.NODE_TRANSITION, nid, {"from": src.value, "to": dst.value, **data},
                        [*self._causes(nid), *_extra])
        rec.last_event = seq
        self._save()
        return seq

    def _normalize(self) -> None:
        """Bring leftover state from a previous run/crash to something schedulable."""
        for nid, rec in self._rec.items():
            if rec.state in (NodeState.RUNNING, NodeState.AWAITING_APPROVAL):
                self._transition(nid, NodeState.FAILED, reason="interrupted before completion")
            if rec.state == NodeState.FAILED:
                self._transition(nid, NodeState.ROLLED_BACK, reason="recovered after interruption")
            if rec.state == NodeState.ROLLED_BACK and not rec.held:
                self._transition(nid, NodeState.PENDING)
                rec.attempts, rec.used_fallback = 0, False
            elif rec.state == NodeState.STALE:
                self._transition(nid, NodeState.PENDING)

    def _entry_block(self, nid: str) -> str | None:
        spec = self.graph.nodes[nid]
        for d in spec.deps:
            if self._rec[d].state != NodeState.SUCCEEDED:
                return f"dependency {d} is held (rolled back)" if self._rec[d].held else _WAIT
        missing = [c for c in spec.consumes if self.store.latest(c) is None]
        if missing:
            return f"missing input artifacts: {missing}"
        if spec.entry_gate is not None:
            gate = spec.entry_gate(self.store)
            if not gate.ok:
                return f"entry gate: {gate.reason}"
        return None

    def _launch_ready(self, pool: ThreadPoolExecutor,
                      futures: dict[Future[tuple[AgentResult, int]], tuple[str, dict[str, str | None]]]) -> None:
        for nid in self.graph.order:
            if self._halt or self._stop_event.is_set():
                return
            rec = self._rec[nid]
            if rec.state == NodeState.PENDING:
                reason = self._entry_block(nid)
                if reason is None:
                    self._transition(nid, NodeState.READY)
                elif reason != _WAIT:
                    self._blocked[nid] = reason
            in_flight = {n for n, _ in futures.values()}
            if rec.state == NodeState.READY and nid not in in_flight and len(futures) < self.config.max_workers:
                if self._steps >= self.config.step_budget:
                    self.stop(f"step budget of {self.config.step_budget} exhausted")
                    return
                self._launch(nid, pool, futures)

    def _launch(self, nid: str, pool: ThreadPoolExecutor,
                futures: dict[Future[tuple[AgentResult, int]], tuple[str, dict[str, str | None]]]) -> None:
        spec, rec = self.graph.nodes[nid], self._rec[nid]
        rec.attempts += 1
        self._steps += 1
        self._transition(nid, NodeState.RUNNING, attempt=rec.attempts,
                         agent=spec.fallback_agent if rec.used_fallback else spec.agent)
        agent = self.agents[spec.fallback_agent if rec.used_fallback and spec.fallback_agent else spec.agent]
        ctx = AgentContext(self.run_id, nid, rec.attempts, self.store, spec.consumes,
                           self._feedback.get(nid))  # kept until the node succeeds (retries need it)
        pre = self.policy.check_pre(spec, ctx)
        self._log(EventType.POLICY_CHECK, nid,
                  {"phase": "pre", "allow": pre.allow, "rule": pre.rule, "reason": pre.reason},
                  self._own(nid))
        if not pre.allow:
            self._fail(nid, f"policy {pre.rule or 'denied'}: {pre.reason}", retryable=False)
            return
        snapshot = {c: (a.sha256 if (a := self.store.latest(c)) else None) for c in spec.consumes}
        futures[pool.submit(self._run_agent, agent, ctx)] = (nid, snapshot)

    @staticmethod
    def _run_agent(agent: Agent, ctx: AgentContext) -> tuple[AgentResult, int]:
        t0 = time.monotonic()
        result = agent.run(ctx)
        return result, int((time.monotonic() - t0) * 1000)

    def _process(self, nid: str, fut: Future[tuple[AgentResult, int]], snapshot: dict[str, str | None]) -> None:
        spec, rec = self.graph.nodes[nid], self._rec[nid]
        self._sync_stop()
        if self._stopped:
            self._discard(nid, f"safe-stop: {self._stop_reason}")
            return
        try:
            result, ms = fut.result()
        except PermanentAgentError as exc:  # the agent says retrying cannot help
            self._log_usage(nid, getattr(exc, "usage", None))
            self._fail(nid, f"agent error: {type(exc).__name__}: {exc}", retryable=False)
            return
        except Exception as exc:  # noqa: BLE001 - agent code is untrusted: any error is a failed attempt
            self._log_usage(nid, getattr(exc, "usage", None))
            self._fail(nid, f"agent error: {type(exc).__name__}: {exc}")
            return
        if isinstance(result, AgentResult):
            self._log_usage(nid, result.usage)
        if not isinstance(result, AgentResult):
            self._fail(nid, "agent returned an invalid result")
            return
        missing = [a for a in spec.produces if a not in result.outputs]
        if missing:
            self._fail(nid, f"exit gate: missing declared outputs {missing}")
            return
        extra = [a for a in result.outputs if a not in spec.produces]
        if extra:
            self._fail(nid, f"exit gate: undeclared outputs {extra}", retryable=False)
            return
        if spec.exit_gate is not None:
            gate = spec.exit_gate(result)
            if not gate.ok:
                self._fail(nid, f"exit gate: {gate.reason}")
                return
        current = {c: (a.sha256 if (a := self.store.latest(c)) else None) for c in spec.consumes}
        if current != snapshot:  # inputs moved while the agent worked: result is stale
            rec.attempts -= 1
            self._log(EventType.REPLAN, nid, {"discarded": "inputs changed during execution"}, self._own(nid))
            self._fail(nid, "inputs changed during execution", requeue_only=True)
            return
        post = self.policy.check_post(spec, result)
        self._log(EventType.POLICY_CHECK, nid,
                  {"phase": "post", "allow": post.allow, "rule": post.rule, "reason": post.reason,
                   "requires_approval": post.requires_approval}, self._own(nid))
        if not post.allow:
            self._fail(nid, f"policy {post.rule or 'denied'}: {post.reason}", retryable=False)
            return
        reasons = [r for r in ("node requires approval" if spec.requires_approval else "",
                               f"policy: {post.reason}" if post.requires_approval else "",
                               f"agent asks for human input: {result.escalate}" if result.escalate else "") if r]
        if reasons:
            why = "; ".join(reasons)
            req = self._log(EventType.APPROVAL_REQUESTED, nid,
                            {"artifacts": sorted(result.outputs), "summary": result.summary, "reason": why},
                            self._own(nid))
            self._transition(nid, NodeState.AWAITING_APPROVAL)
            decision: ApprovalDecision = self.approver.decide(
                ApprovalRequest(self.run_id, nid, result.summary, tuple(sorted(result.outputs)), why))
            # the decision becomes this node's latest cause, so whatever follows (commit or
            # failure) links back to the human who made it: decision lineage reaches people
            self._rec[nid].last_event = self._log(
                EventType.APPROVAL_DECIDED, nid, {"verdict": decision.verdict, "comment": decision.comment},
                [req], actor=f"human:{decision.approver}")
            if decision.verdict == "reject":
                self._fail(nid, f"rejected by {decision.approver}: {decision.comment}", retryable=False)
                return
            if decision.verdict == "changes":
                self._feedback[nid] = decision.comment
                self._fail(nid, f"changes requested by {decision.approver}: {decision.comment}")
                return
        self._commit(nid, result, ms)

    def _log_usage(self, nid: str, usage: Any) -> None:
        """Model calls an attempt spent (tokens, latency), success or not; feeds cost metrics."""
        if isinstance(usage, list) and usage:
            self._log(EventType.NOTE, nid, {"model_usage": usage}, self._own(nid))

    def _commit(self, nid: str, result: AgentResult, duration_ms: int) -> None:
        spec, rec = self.graph.nodes[nid], self._rec[nid]
        agent = self.agents[spec.fallback_agent if rec.used_fallback and spec.fallback_agent else spec.agent]
        events: list[int] = []
        changed: set[str] = set()
        for name, content in result.outputs.items():
            prev = self.store.latest(name)
            put = self.store.put(name, content, kind=result.kind, produced_by=f"agent:{agent.name}",
                                 inputs=spec.consumes)
            if put.created:
                changed.add(name)
                rec.committed[name] = {"version": put.artifact.version, "prev": prev.version if prev else None}
            events.append(self._log(EventType.ARTIFACT_CREATED, nid,
                                    {"artifact": put.artifact.id, "sha256": put.artifact.sha256,
                                     "created": put.created}, self._own(nid)))
        seq = self._transition(nid, NodeState.SUCCEEDED, events,
                               duration_ms=duration_ms, attempts=rec.attempts)
        rec.attempts, rec.replans = 0, 0
        self._feedback.pop(nid, None)  # addressed: the next run of this node starts clean
        if changed:
            self.replan(changed, cause=seq)

    def _fail(self, nid: str, reason: str, retryable: bool = True, requeue_only: bool = False) -> None:
        spec, rec = self.graph.nodes[nid], self._rec[nid]
        seq = self._transition(nid, NodeState.FAILED, reason=reason)
        if requeue_only:
            self._transition(nid, NodeState.READY)
            return
        if retryable and rec.attempts <= spec.max_retries:
            self._log(EventType.RETRY_SCHEDULED, nid,
                      {"failed_attempt": rec.attempts, "reason": reason}, [seq])
            self._transition(nid, NodeState.READY)
            return
        if retryable and spec.fallback_agent and not rec.used_fallback:
            rec.used_fallback, rec.attempts = True, 0
            self._log(EventType.RETRY_SCHEDULED, nid,
                      {"fallback_agent": spec.fallback_agent, "reason": reason}, [seq])
            self._transition(nid, NodeState.READY)
            return
        if retryable and spec.on_failure_replan and rec.replans < spec.max_replans:
            self._replan_upstream(nid, reason, seq)
            return
        self._transition(nid, NodeState.ROLLED_BACK, reason=reason, terminal=True)
        self._log(EventType.ROLLBACK, nid,
                  {"reverted": [], "reason": reason,
                   "note": "staged outputs discarded; nothing had been committed"}, [seq])
        self._halt, self._halt_reason = True, f"{nid}: {reason}"

    def _replan_upstream(self, nid: str, reason: str, cause: int) -> None:
        """Non-linear recovery: route a failure back to the upstream nodes that can fix it."""
        spec, rec = self.graph.nodes[nid], self._rec[nid]
        rec.replans += 1
        self._transition(nid, NodeState.ROLLED_BACK, reason=reason)
        for target in spec.on_failure_replan:
            self._feedback[target] = f"{nid} failed: {reason}"
        scope = set(spec.on_failure_replan).union(*(self.graph.descendants(t) for t in spec.on_failure_replan))
        invalidated: list[str] = []
        for other in self.graph.order:
            orec = self._rec[other]
            if other in scope and orec.state == NodeState.SUCCEEDED:
                self._transition(other, NodeState.STALE, reason=f"re-plan after {nid} failed")
                self._transition(other, NodeState.PENDING)
                orec.attempts, orec.used_fallback = 0, False
                invalidated.append(other)
        self._transition(nid, NodeState.PENDING, reason="re-planned")
        rec.attempts, rec.used_fallback = 0, False
        self._log(EventType.REPLAN, nid,
                  {"reason": reason, "feedback_to": list(spec.on_failure_replan), "invalidated": invalidated,
                   "loop": rec.replans, "max_loops": spec.max_replans}, [cause])
        self._save()

    def _discard(self, nid: str, why: str) -> None:
        seq = self._transition(nid, NodeState.FAILED, reason=why)
        self._transition(nid, NodeState.ROLLED_BACK, reason=why)
        self._log(EventType.ROLLBACK, nid, {"reverted": [], "reason": why, "note": "in-flight result discarded"}, [seq])

    def _summarize(self) -> RunSummary:
        done = all(r.state in (NodeState.SUCCEEDED, NodeState.SKIPPED) for r in self._rec.values())
        if self._stopped:
            state, reason = RunState.SAFE_STOPPED, self._stop_reason
        elif self._halt:
            state, reason = RunState.FAILED, self._halt_reason
        elif done:
            state, reason = RunState.SUCCEEDED, ""
        else:
            state, reason = RunState.BLOCKED, "no runnable nodes remain"
        return RunSummary(state, reason, self.states(), self._steps, dict(self._blocked))

    # -------------------------------------------------------------- persistence
    def _save(self) -> None:
        if self._state_path is None:
            return
        payload = {i: {"state": r.state.value, "attempts": r.attempts, "used_fallback": r.used_fallback,
                       "held": r.held, "committed": r.committed, "last_event": r.last_event,
                       "replans": r.replans}
                   for i, r in self._rec.items()}
        atomic_write(self._state_path, json.dumps(payload, indent=2, sort_keys=True))

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        raw = json.loads(self._state_path.read_text("utf-8"))
        for nid, d in raw.items():
            if nid in self._rec:
                self._rec[nid] = NodeRecord(NodeState(d["state"]), d["attempts"], d["used_fallback"],
                                            d["held"], d["committed"], d["last_event"],
                                            d.get("replans", 0))  # absent in older state files
