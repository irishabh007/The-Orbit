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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from warden.agent import AgentContext, AgentResult
from warden.dag import GateResult
from warden.tools.runner import CommandResult, Runner, run_command
from warden.tools.workspace import collect_bundles, write_files

REPORT = "verify_report.json"
_TAIL = 3000  # characters of output kept per check
_GATE_TAIL = 4000  # characters of failing output fed back to the agents
_DURATION = re.compile(r"\bin \d+(?:\.\d+)?s\b")
# .NET timing lines: "Time Elapsed 00:00:07.59", "Duration: 37 ms", "(in 1.2 sec)"
_DOTNET_TIMINGS = (
    (re.compile(r"Time Elapsed \d{2}:\d{2}:\d{2}(?:\.\d{1,7})?"), "Time Elapsed <t>"),
    (re.compile(r"Duration: \d{1,9}(?:\.\d{1,9})? ?(?:ms|s|m|h)\b"), "Duration: <t>"),
    (re.compile(r"\(in \d{1,9}(?:\.\d{1,9})? ?(?:ms|sec|s)\)"), "(in <t>)"),
)


def normalize_output(text: str, scratch: Path) -> str:
    """Remove run-specific noise (scratch path, timings) from check output.

    The output is fed back to the developer in repair prompts; anything that changes
    from run to run would change the prompt and make replay miss its recording.
    """
    for root in sorted({str(scratch), str(scratch.resolve())}, key=len, reverse=True):
        text = text.replace(root + os.sep, "./").replace(root, ".")
    if os.sep == "\\":
        text = text.replace("\\", "/")
    for pattern, replacement in _DOTNET_TIMINGS:
        text = pattern.sub(replacement, text)
    return _DURATION.sub("in <t>s", text)


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    # regex the output must match for the check to pass (exit code 0 alone can be a false green,
    # e.g. `dotnet test` exits 0 when nothing was built or run)
    requires: str | None = None
    # turns the raw result into the feedback agents read (e.g. a structured test-results digest);
    # None = the console output itself
    digest: Callable[[CommandResult, Path], str] | None = None

    def passed(self, result: CommandResult) -> bool:
        return result.ok and (self.requires is None or re.search(self.requires, result.output) is not None)

    def feedback(self, result: CommandResult, cwd: Path) -> str:
        return self.digest(result, cwd) if self.digest is not None else result.output


_MAX_GROUPS = 8


_Row = tuple[str, str, str, str]  # (outcome, test name, first message line, first stack frame)


def _trx_rows(trx: Path) -> list[_Row]:
    rows = []
    for element in ElementTree.parse(trx).getroot().iter():
        if element.tag.rsplit("}", 1)[-1] != "UnitTestResult":
            continue
        texts = {child.tag.rsplit("}", 1)[-1]: child.text or "" for child in element.iter()}
        message_lines = texts.get("Message", "").strip().splitlines()
        frame = next((ln.strip() for ln in texts.get("StackTrace", "").splitlines() if ":line " in ln), "")
        rows.append((element.get("outcome", ""), element.get("testName", "?"),
                     message_lines[0][:300] if message_lines else "", frame))
    return rows


def _format_digest(rows: list[_Row]) -> str:
    failed = sorted((r for r in rows if r[0] == "Failed"), key=lambda r: r[1])
    lines = [f"Tests: {len(rows)} total, {sum(r[0] == 'Passed' for r in rows)} passed, {len(failed)} failed."]
    groups: dict[str, list[_Row]] = {}
    for row in failed:
        groups.setdefault(row[2] or "(no message)", []).append(row)
    if groups:
        lines.append("Failures grouped by error (most frequent first):")
    for message, members in sorted(groups.items(), key=lambda g: (-len(g[1]), g[0]))[:_MAX_GROUPS]:
        lines.append(f"- {len(members)} test(s): {message}")
        lines.append(f"    first: {members[0][1]}" + (f"\n    {members[0][3]}" if members[0][3] else ""))
    if len(groups) > _MAX_GROUPS:
        lines.append(f"... and {len(groups) - _MAX_GROUPS} more distinct error(s)")
    return "\n".join(lines)


def dotnet_test_digest(result: CommandResult, cwd: Path) -> str:
    """Failures from the TRX results file, grouped by error, most frequent first.

    Quiet console output names failing tests but not why they failed; TRX carries the error
    message and stack trace. Sorted and timing-free, so the same failures always produce the
    same text (replay stays deterministic). Falls back to the console output if there is no
    readable TRX (for example the test host crashed).
    """
    trx_files = sorted(cwd.glob("**/TestResults/*.trx"))
    try:
        rows = [row for trx in trx_files for row in _trx_rows(trx)]
    except ElementTree.ParseError:
        return result.output
    return _format_digest(rows) if trx_files else result.output


DEFAULT_CHECKS = (
    # short tracebacks and no captured logs: the output is feedback for an agent, keep it signal
    Check("tests", (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
                    "--show-capture=no")),
    # pinned to ruff's classic correctness rules (syntax, undefined names, unused imports), so
    # a new ruff release cannot change what "lint passes" means for generated code
    Check("lint", (sys.executable, "-m", "ruff", "check", "--no-cache", "--select", "E4,E7,E9,F", ".")),
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
                # no timings in the report: downstream agents read it, and anything that varies
                # between runs changes their prompts and breaks replay (the event log has timings)
                results.append({"name": check.name, "command": " ".join(check.argv[1:] or check.argv),
                                "passed": check.passed(res), "returncode": res.returncode,
                                "timed_out": res.timed_out,
                                "output_tail": normalize_output(check.feedback(res, scratch), scratch)[-_TAIL:]})
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
