"""Versioned, content-addressed artifact store.

This is what makes dynamic re-planning precise. Each artifact records the exact
content hash of every input it was built from. If an input is later replaced
with different content, everything built (directly or transitively) from the
old version is reported stale, and nothing else is touched.

Writing identical content again is a no-op (no new version), so an agent that
re-runs and produces the same output does not invalidate downstream work.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NamedTuple

from warden.eventlog import utc_now
from warden.fsutil import atomic_write


@dataclass(frozen=True)
class Artifact:
    name: str  # logical name, e.g. "spec.md" or "src/app.py"
    version: int
    sha256: str
    kind: str  # e.g. "requirements", "design", "code", "test", "doc"
    produced_by: str  # actor id
    inputs: dict[str, str]  # input name -> sha256 at build time
    created_at: str

    @property
    def id(self) -> str:
        return f"{self.name}@v{self.version}"


class PutResult(NamedTuple):
    artifact: Artifact
    created: bool  # False when content was identical to the latest version


class ArtifactStore:
    def __init__(self, root: str | Path, clock: Callable[[], str] = utc_now) -> None:
        self.root = Path(root)
        self._blobs = self.root / "blobs"
        self._index_path = self.root / "index.json"
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._index: dict[str, list[Artifact]] = self._load()

    # ---- persistence ---------------------------------------------------
    def _load(self) -> dict[str, list[Artifact]]:
        if not self._index_path.exists():
            return {}
        raw: dict[str, list[dict[str, Any]]] = json.loads(self._index_path.read_text("utf-8"))
        return {name: [Artifact(**a) for a in versions] for name, versions in raw.items()}

    def _save(self) -> None:
        payload = {n: [asdict(a) for a in vs] for n, vs in self._index.items()}
        # atomic: never leaves a half-written index
        atomic_write(self._index_path, json.dumps(payload, indent=2, sort_keys=True))

    # ---- writing -------------------------------------------------------
    def put(
        self,
        name: str,
        content: str | bytes,
        kind: str = "text",
        produced_by: str = "human",
        inputs: Iterable[str] = (),
    ) -> PutResult:
        data = content.encode("utf-8") if isinstance(content, str) else content
        sha = hashlib.sha256(data).hexdigest()
        with self._lock:
            input_hashes = {}
            for dep in inputs:
                latest_dep = self.latest(dep)
                if latest_dep is None:
                    raise KeyError(f"unknown input artifact: {dep}")
                input_hashes[dep] = latest_dep.sha256

            history = self._index.get(name, [])
            if history and history[-1].sha256 == sha:
                latest = history[-1]
                if latest.inputs != input_hashes:
                    # Same output, rebuilt against newer inputs: re-validate the
                    # artifact (no new version) so it stops being reported stale.
                    latest = dataclasses.replace(latest, inputs=input_hashes)
                    self._index[name] = [*history[:-1], latest]
                    self._save()
                return PutResult(latest, False)

            blob = self._blobs / sha
            if not blob.exists():
                blob.write_bytes(data)
            art = Artifact(
                name=name,
                version=len(history) + 1,
                sha256=sha,
                kind=kind,
                produced_by=produced_by,
                inputs=input_hashes,
                created_at=self._clock(),
            )
            self._index[name] = [*history, art]
            self._save()
            return PutResult(art, True)

    def restore(self, name: str, version: int, produced_by: str = "rollback") -> Artifact:
        """Re-publish an old version's content as a new latest version (history is kept)."""
        with self._lock:
            old = self.get(name, version)
            history = self._index[name]
            art = dataclasses.replace(
                old, version=len(history) + 1, produced_by=produced_by, created_at=self._clock()
            )
            self._index[name] = [*history, art]
            self._save()
            return art

    def drop(self, name: str) -> None:
        """Remove an artifact from the index (blobs stay on disk; the event log is the record)."""
        with self._lock:
            self._index.pop(name, None)
            self._save()

    # ---- reading -------------------------------------------------------
    def latest(self, name: str) -> Artifact | None:
        history = self._index.get(name)
        return history[-1] if history else None

    def get(self, name: str, version: int) -> Artifact:
        return self._index[name][version - 1]

    def history(self, name: str) -> list[Artifact]:
        return list(self._index.get(name, []))

    def names(self) -> list[str]:
        return sorted(self._index)

    def read_bytes(self, artifact: Artifact) -> bytes:
        return (self._blobs / artifact.sha256).read_bytes()

    def read_text(self, artifact: Artifact) -> str:
        return self.read_bytes(artifact).decode("utf-8")

    # ---- change impact -------------------------------------------------
    def dependents(self, name: str) -> set[str]:
        """Names whose latest version was (transitively) built from ``name``."""
        result: set[str] = set()
        frontier = {name}
        while frontier:
            nxt = {
                n
                for n in self._index
                if n not in result
                and n != name
                and any(dep in frontier for dep in self._index[n][-1].inputs)
            }
            result |= nxt
            frontier = nxt
        return result

    def stale(self) -> set[str]:
        """Artifacts built from an input that has since changed, plus all their dependents."""
        direct = set()
        for name, versions in self._index.items():
            for dep, recorded in versions[-1].inputs.items():
                current = self.latest(dep)
                if current is None or current.sha256 != recorded:
                    direct.add(name)
        result = set(direct)
        for name in direct:
            result |= self.dependents(name)
        return result
