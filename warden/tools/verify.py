"""Verify node: run the generated tests and lint on a scratch copy of the bundles.

The agent never touches the real workspace: it materializes the consumed bundles in
a temporary directory, runs each check there and returns a ``verify_report.json``.
``verify_gate`` turns a failed report into a closed exit gate whose reason carries
the failing output, which the orchestrator routes back to the developer.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from warden.agent import AgentContext, AgentResult
from warden.dag import GateResult
from warden.tools.runner import Runner, run_command
from warden.tools.workspace import collect_bundles, write_files

REPORT = "verify_report.json"
_TAIL = 3000  # characters of output kept per check
_GATE_TAIL = 1500
_DURATION = re.compile(r"\bin \d+(?:\.\d+)?s\b")


def normalize_output(text: str, scratch: Path) -> str:
    """Remove run-specific noise (scratch path, timings) from check output.

    The output is fed back to the developer in repair prompts; anything that changes
    from run to run would change the prompt and make replay miss its recording.
    """
    for root in sorted({str(scratch), str(scratch.resolve())}, key=len, reverse=True):
        text = text.replace(root + os.sep, "./").replace(root, ".")
    if os.sep == "\\":
        text = text.replace("\\", "/")
    return _DURATION.sub("in <t>s", text)


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]


DEFAULT_CHECKS = (
    Check("tests", (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")),
    Check("lint", (sys.executable, "-m", "ruff", "check", "--no-cache", ".")),
)


class VerifyAgent:
    name = "verify"

    def __init__(self, checks: Sequence[Check] = DEFAULT_CHECKS, *, runner: Runner = run_command,
                 timeout: float = 300.0) -> None:
        self.checks = tuple(checks)
        self.runner = runner
        self.timeout = timeout

    def run(self, ctx: AgentContext) -> AgentResult:
        files = collect_bundles(ctx.store, ctx.consumes)
        results: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="warden-verify-") as tmp:
            scratch = Path(tmp)
            write_files(scratch, files)
            for check in self.checks:
                res = self.runner(check.argv, scratch, self.timeout)
                results.append({"name": check.name, "command": " ".join(check.argv[1:] or check.argv),
                                "passed": res.ok, "returncode": res.returncode, "timed_out": res.timed_out,
                                "duration_ms": res.duration_ms,
                                "output_tail": normalize_output(res.output, scratch)[-_TAIL:]})
        passed = all(r["passed"] for r in results)
        report = {"passed": passed, "files": len(files), "checks": results}
        ok = sum(r["passed"] for r in results)
        failing = [r["name"] for r in results if not r["passed"]]
        summary = f"{ok}/{len(results)} checks passed" + (f"; failing: {', '.join(failing)}" if failing else "")
        return AgentResult({REPORT: json.dumps(report, indent=2, sort_keys=True)}, kind="verification",
                           summary=summary)


def verify_gate(result: AgentResult) -> GateResult:
    try:
        report = json.loads(result.outputs[REPORT])
    except (KeyError, TypeError, ValueError):
        return GateResult(False, f"missing or unreadable {REPORT}")
    if report.get("passed"):
        return GateResult(True)
    reasons = [f"{c['name']} failed (exit {c['returncode']}{', timed out' if c.get('timed_out') else ''}):\n"
               f"{c['output_tail'][-_GATE_TAIL:]}" for c in report.get("checks", []) if not c.get("passed")]
    return GateResult(False, "\n".join(reasons) or "verification failed")
