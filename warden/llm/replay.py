"""Record/replay backend: reviewers run every demo offline, with no API key.

Each request is stored as ``<dir>/<request.key()>.json`` (``-1``, ``-2``... for repeats; easy
to review and diff, and parallel agents never contend for one file). Modes:

* ``replay``  recordings only; a miss raises ``LLMReplayMiss`` (fails closed, no network)
* ``record``  always call the inner client and (over)write the recording
* ``auto``    replay when recorded, otherwise call the inner client and record

Only successful responses are recorded. Recordings hold the request and response,
never credentials. Replay makes demos reproducible, not the model.

*Drift.* Generated tests can be flaky, so a replayed run can see slightly different test
output than the recording did, and an agent's repair prompt then differs by a line or two.
On a miss, replay and auto serve the closest unused recording for the same agent (same
system prompt, >= ``DRIFT_THRESHOLD`` of prompt lines alike) and mark the response
``replay-nearest``. Anything less alike is still a miss.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from warden.eventlog import utc_now
from warden.fsutil import atomic_write
from warden.llm.base import LLMClient, LLMReplayMiss, LLMRequest, LLMResponse

Mode = Literal["replay", "record", "auto"]
DRIFT_THRESHOLD = 0.99


class RecordReplayClient:
    name = "replay"

    def __init__(self, directory: str | Path, inner: LLMClient | None = None, mode: Mode = "replay") -> None:
        if mode not in ("replay", "record", "auto"):
            raise ValueError(f"unknown mode {mode!r}")
        if mode != "replay" and inner is None:
            raise ValueError(f"mode {mode!r} needs an inner client to record from")
        self.directory = Path(directory)
        self.inner = inner
        self.mode = mode
        self._seen: Counter[str] = Counter()
        self._lock = threading.Lock()
        self.used: set[Path] = set()  # recordings read or written this session (for pruning)
        self.drifted: list[Path] = []  # recordings served for a prompt that differed slightly

    def _path(self, request: LLMRequest) -> Path:
        """The n-th identical request in this client's lifetime has its own recording.

        A retry re-sends the same prompt and must get a fresh model answer (recorded as
        ``<key>-1``, ``<key>-2``...), not a replay of the answer that just failed. Replay
        follows the same sequence, so a recorded run reproduces exactly.
        """
        key = request.key()
        with self._lock:
            n = self._seen[key]
        return self.directory / (f"{key}.json" if n == 0 else f"{key}-{n}.json")

    def _claim(self, request: LLMRequest, path: Path) -> None:
        """A slot is used only by a successful answer, so the retry of a failed call (e.g. a 503)
        lands in the same slot a later run (which may not see the 503) will look in."""
        with self._lock:
            self._seen[request.key()] += 1
            self.used.add(path)

    def complete(self, request: LLMRequest) -> LLMResponse:
        path = self._path(request)
        if self.mode != "record":
            if path.exists() and path not in self.used:
                return self._replay(request, path, self.name)
            nearest = self._nearest(request)
            if nearest is not None:
                with self._lock:
                    self.drifted.append(nearest)
                return self._replay(request, nearest, f"{self.name}-nearest")
        if self.inner is None:
            raise LLMReplayMiss(f"no recording for request {request.key()[:12]} in {self.directory}")
        response = self.inner.complete(request)  # errors propagate, are never recorded, claim no slot
        self._write(path, request, response)
        self._claim(request, path)
        return response

    def _replay(self, request: LLMRequest, path: Path, backend: str) -> LLMResponse:
        self._claim(request, path)
        recorded = json.loads(path.read_text("utf-8"))["response"]
        return LLMResponse(recorded["text"], recorded["model"], backend, recorded.get("input_tokens"),
                           recorded.get("output_tokens"), recorded.get("thinking_tokens"))

    def _nearest(self, request: LLMRequest) -> Path | None:
        """The unused recording for the same agent whose prompt is most alike, if alike enough."""
        want = request.prompt.splitlines()
        best, best_ratio = None, DRIFT_THRESHOLD
        for path in sorted(self.directory.glob("*.json")):
            with self._lock:
                if path in self.used:
                    continue
            recorded = json.loads(path.read_text("utf-8"))["request"]
            if (recorded.get("system"), recorded.get("json_mode"), recorded.get("temperature")) != (
                    request.system, request.json_mode, request.temperature):
                continue
            ratio = SequenceMatcher(None, recorded.get("prompt", "").splitlines(), want, autojunk=False).ratio()
            if ratio >= best_ratio and (best is None or ratio > best_ratio):
                best, best_ratio = path, ratio
        return best

    def prune(self) -> list[Path]:
        """Delete recordings this session did not use; call after a complete, successful run.

        Recording sessions leave answers from abandoned paths (fixed bugs, changed prompts).
        Pruning after a clean replay keeps exactly the set the demo needs.
        """
        if not self.used:
            raise ValueError("this session used no recordings; refusing to prune everything")
        removed = sorted(p for p in self.directory.glob("*.json") if p not in self.used)
        for path in removed:
            path.unlink()
        return removed

    def _write(self, path: Path, request: LLMRequest, response: LLMResponse) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"key": request.key(), "recorded_at": utc_now(),
                   "request": asdict(request), "response": asdict(response)}
        # atomic: a crash never leaves a half-written recording
        atomic_write(path, json.dumps(payload, indent=2, sort_keys=True))
