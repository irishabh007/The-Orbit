"""Append-only, hash-chained audit log (JSON Lines).

Every event stores the hash of the previous event, so editing, deleting or
reordering any line breaks the chain and ``verify()`` reports where.
``caused_by`` records decision lineage: the earlier events that led to this one.

Scope note: this is tamper-*evident*, not tamper-proof. Someone who rewrites
the whole file and recomputes every hash can still forge a log; anchoring the
latest hash somewhere external would close that gap.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from warden.models import EventType

GENESIS_HASH = "0" * 64


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def compute_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EventLogCorrupt(Exception):
    """The log file could not be parsed."""


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    run_id: str
    type: str
    actor: str  # e.g. "orchestrator", "agent:developer", "human:alice"
    node_id: str | None
    data: dict[str, Any]
    caused_by: tuple[int, ...]
    prev_hash: str
    hash: str = field(default="")

    def hashable(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "run_id": self.run_id,
            "type": self.type,
            "actor": self.actor,
            "node_id": self.node_id,
            "data": self.data,
            "caused_by": list(self.caused_by),
            "prev_hash": self.prev_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.hashable(), "hash": self.hash}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Event:
        return cls(
            seq=raw["seq"],
            ts=raw["ts"],
            run_id=raw["run_id"],
            type=raw["type"],
            actor=raw["actor"],
            node_id=raw["node_id"],
            data=raw["data"],
            caused_by=tuple(raw["caused_by"]),
            prev_hash=raw["prev_hash"],
            hash=raw["hash"],
        )


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    count: int
    error: str | None = None
    bad_seq: int | None = None


class EventLog:
    """Thread-safe append-only log. Safe for parallel branches in one process."""

    def __init__(self, path: str | Path, clock: Callable[[], str] = utc_now) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._last_seq = 0
        self._last_hash = GENESIS_HASH
        for last in self._iter_raw():
            self._last_seq, self._last_hash = last.seq, last.hash

    # ---- writing -------------------------------------------------------
    def append(
        self,
        run_id: str,
        type: EventType | str,
        actor: str,
        node_id: str | None = None,
        data: dict[str, Any] | None = None,
        caused_by: Sequence[int] = (),
    ) -> Event:
        with self._lock:
            seq = self._last_seq + 1
            bad = [c for c in caused_by if not (1 <= c < seq)]
            if bad:
                raise ValueError(f"caused_by references unknown events: {bad}")
            draft = Event(
                seq=seq,
                ts=self._clock(),
                run_id=run_id,
                type=type.value if isinstance(type, EventType) else type,
                actor=actor,
                node_id=node_id,
                data=data or {},
                caused_by=tuple(caused_by),
                prev_hash=self._last_hash,
            )
            event = dataclasses.replace(draft, hash=compute_hash(draft.hashable()))
            line = json.dumps(event.to_dict(), sort_keys=True)  # raises before any write
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
            self._last_seq, self._last_hash = event.seq, event.hash
            return event

    # ---- reading -------------------------------------------------------
    def _iter_raw(self) -> Iterator[Event]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    yield Event.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise EventLogCorrupt(f"line {lineno}: {exc}") from exc

    def read_all(self, run_id: str | None = None) -> list[Event]:
        return [e for e in self._iter_raw() if run_id is None or e.run_id == run_id]

    def lineage(self, seq: int) -> list[Event]:
        """All events that (transitively) led to ``seq``, oldest first, including it."""
        by_seq = {e.seq: e for e in self._iter_raw()}
        if seq not in by_seq:
            raise KeyError(seq)
        seen: set[int] = set()
        stack = [seq]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(by_seq[cur].caused_by)
        return [by_seq[s] for s in sorted(seen)]

    # ---- integrity -----------------------------------------------------
    def verify(self) -> VerifyResult:
        prev_hash, prev_seq, count = GENESIS_HASH, 0, 0
        try:
            for event in self._iter_raw():
                count += 1
                if event.seq != prev_seq + 1:
                    return VerifyResult(False, count, f"sequence gap before {event.seq}", event.seq)
                if event.prev_hash != prev_hash:
                    return VerifyResult(False, count, "broken chain link", event.seq)
                if compute_hash(event.hashable()) != event.hash:
                    return VerifyResult(False, count, "content does not match hash", event.seq)
                if any(not (1 <= c < event.seq) for c in event.caused_by):
                    return VerifyResult(False, count, "invalid caused_by reference", event.seq)
                prev_hash, prev_seq = event.hash, event.seq
        except EventLogCorrupt as exc:
            return VerifyResult(False, count, f"unparseable: {exc}")
        return VerifyResult(True, count)
