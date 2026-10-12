"""Record/replay backend: reviewers run every demo offline, with no API key.

Each request is stored as ``<dir>/<request.key()>.json`` (one file per request: easy
to review and diff, and parallel agents never contend for one file). Modes:

* ``replay``  recordings only; a miss raises ``LLMReplayMiss`` (fails closed, no network)
* ``record``  always call the inner client and (over)write the recording
* ``auto``    replay when recorded, otherwise call the inner client and record

Only successful responses are recorded. Recordings hold the request and response,
never credentials. Replay makes demos reproducible, not the model.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from warden.eventlog import utc_now
from warden.llm.base import LLMClient, LLMReplayMiss, LLMRequest, LLMResponse

Mode = Literal["replay", "record", "auto"]


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

    def _path(self, request: LLMRequest) -> Path:
        return self.directory / f"{request.key()}.json"

    def complete(self, request: LLMRequest) -> LLMResponse:
        path = self._path(request)
        if self.mode != "record" and path.exists():
            recorded = json.loads(path.read_text("utf-8"))["response"]
            return LLMResponse(recorded["text"], recorded["model"], self.name,
                               recorded.get("input_tokens"), recorded.get("output_tokens"))
        if self.inner is None:
            raise LLMReplayMiss(f"no recording for request {request.key()[:12]} in {self.directory}")
        response = self.inner.complete(request)  # errors propagate and are never recorded
        self._write(path, request, response)
        return response

    def _write(self, path: Path, request: LLMRequest, response: LLMResponse) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"key": request.key(), "recorded_at": utc_now(),
                   "request": asdict(request), "response": asdict(response)}
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash never leaves a half-written recording
