"""Core domain types: node/run states, the legal transition table, event types.

The transition table is the single source of truth for what the orchestrator
may do to a node. Anything not listed is a bug and raises ``InvalidTransition``.
"""

from __future__ import annotations

import enum


class NodeState(str, enum.Enum):
    PENDING = "PENDING"  # waiting on upstream nodes
    READY = "READY"  # entry gate passed, can be scheduled
    RUNNING = "RUNNING"  # an agent is working
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # paused for a human decision
    SUCCEEDED = "SUCCEEDED"  # exit gate passed
    FAILED = "FAILED"  # attempt failed (may be retried)
    ROLLED_BACK = "ROLLED_BACK"  # outputs reverted
    SKIPPED = "SKIPPED"  # not needed in this run
    STALE = "STALE"  # upstream changed; output no longer trustworthy


class RunState(str, enum.Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SAFE_STOPPED = "SAFE_STOPPED"
    BLOCKED = "BLOCKED"  # nothing runnable: held node, missing input or failed entry gate


class EventType(str, enum.Enum):
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    NODE_TRANSITION = "NODE_TRANSITION"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    POLICY_CHECK = "POLICY_CHECK"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_DECIDED = "APPROVAL_DECIDED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    ROLLBACK = "ROLLBACK"
    REPLAN = "REPLAN"
    SAFE_STOP = "SAFE_STOP"
    NOTE = "NOTE"


class InvalidTransition(Exception):
    """Raised when a node is asked to make a transition the table forbids."""


_S = NodeState

TRANSITIONS: dict[NodeState, frozenset[NodeState]] = {
    _S.PENDING: frozenset({_S.READY, _S.SKIPPED}),
    _S.READY: frozenset({_S.RUNNING, _S.SKIPPED}),
    _S.RUNNING: frozenset({_S.SUCCEEDED, _S.FAILED, _S.AWAITING_APPROVAL}),
    # approved -> SUCCEEDED, rejected -> FAILED, "changes requested" -> RUNNING
    _S.AWAITING_APPROVAL: frozenset({_S.SUCCEEDED, _S.FAILED, _S.RUNNING}),
    _S.FAILED: frozenset({_S.READY, _S.ROLLED_BACK}),  # retry or roll back
    _S.SUCCEEDED: frozenset({_S.STALE, _S.ROLLED_BACK}),
    _S.STALE: frozenset({_S.PENDING}),  # re-plan resets it
    _S.ROLLED_BACK: frozenset({_S.PENDING}),
    _S.SKIPPED: frozenset({_S.PENDING}),
}

TERMINAL_STATES = frozenset({_S.SUCCEEDED, _S.SKIPPED})


def can_transition(src: NodeState, dst: NodeState) -> bool:
    return dst in TRANSITIONS[src]


def assert_transition(src: NodeState, dst: NodeState) -> None:
    if not can_transition(src, dst):
        raise InvalidTransition(f"{src.value} -> {dst.value} is not allowed")
