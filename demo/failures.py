"""Failure demos: how Warden behaves when things go wrong.

The orchestrator, policy engine, approval gates and audit log are the real ones; only
the model answers are scripted (see ``demo.fixtures``) so each failure is reproducible
and costs no model calls. Verification is scripted to pass, so each case isolates one
failure mode.
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from demo.console import ConsoleEventLog
from demo.fixtures import ANSWERS, APP, bundle, js
from demo.runner import DEFAULT_POLICY, prepare_run_dir, print_summary
from warden.agents import DeveloperAgent, build_agents, sdlc_graph
from warden.approval import ApprovalDecision, Approver, ScriptedApprover
from warden.artifacts import ArtifactStore
from warden.dag import Graph
from warden.llm import LLMClient, LLMTransientError, ScriptedClient
from warden.orchestrator import Config, Orchestrator, RunSummary
from warden.policy import YamlPolicy
from warden.report import write_report
from warden.tools import Check, CommandResult, VerifyAgent

# A well-formed AWS access key id, assembled at runtime so this file itself stays clean.
PLANTED_SECRET = "AKIA" + "Z7Q3EXAMPLEKEY12"
AUTO = ApprovalDecision("approve", "auto-approve", "failure demo")


@dataclass(frozen=True)
class Case:
    name: str
    title: str
    shows: str
    run: Callable[[Path, TextIO], tuple[RunSummary, ArtifactStore, Path]]


@dataclass(frozen=True)
class FailureResult:
    summary: RunSummary
    store: ArtifactStore
    log_path: Path
    run_dir: Path
    planted_secret: str = PLANTED_SECRET


def _scripted_verifier() -> VerifyAgent:
    def runner(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        return CommandResult(tuple(argv), 0, "scripted: 1 passed", 1)
    return VerifyAgent((Check("tests", ("pytest",)),), runner=runner)


def _clients(**overrides: LLMClient) -> dict[str, LLMClient]:
    clients: dict[str, LLMClient] = {role: ScriptedClient([], default=js(answer))
                                     for role, answer in ANSWERS.items()}
    clients.update(overrides)
    return clients


def _execute(run_dir: Path, out: TextIO, clients: dict[str, LLMClient], *, approver: Approver | None = None,
             graph: Graph | None = None, extra_agents: dict[str, DeveloperAgent] | None = None,
             config: Config | None = None) -> tuple[RunSummary, ArtifactStore, Path]:
    store = ArtifactStore(run_dir / "artifacts")
    store.put("request", "Build a URL shortener (scripted failure demo).", produced_by="human:requester")
    agents = build_agents(clients, verifier=_scripted_verifier())
    agents.update(extra_agents or {})
    log_path = run_dir / "events.jsonl"
    with ConsoleEventLog(log_path, out=out) as log:
        orch = Orchestrator(graph or sdlc_graph(), agents, store, log, f"failures-{run_dir.name}",
                            policy=YamlPolicy.from_file(DEFAULT_POLICY),
                            approver=approver or ScriptedApprover(default=AUTO), config=config)
        summary = orch.run()
    return summary, store, log_path


def _policy_block(run_dir: Path, out: TextIO) -> tuple[RunSummary, ArtifactStore, Path]:
    leaky = bundle({"app/main.py": APP, "app/config.py": f'AWS_ACCESS_KEY_ID = "{PLANTED_SECRET}"\n',
                    "requirements.txt": "fastapi>=0.110\n"})
    return _execute(run_dir, out, _clients(developer=ScriptedClient([], default=js(leaky))))


def _human_reject(run_dir: Path, out: TextIO) -> tuple[RunSummary, ArtifactStore, Path]:
    approver = ScriptedApprover({"architect": [ApprovalDecision(
        "reject", "reviewer", "No rate limiting in the design; resubmit with it.")]}, default=AUTO)
    return _execute(run_dir, out, _clients(), approver=approver)


def _with_developer_fallback() -> Graph:
    graph = sdlc_graph()
    nodes = [dataclasses.replace(n, fallback_agent="developer-fallback") if n.id == "developer" else n
             for n in graph.nodes.values()]
    return Graph(nodes)


def _busy_model() -> ScriptedClient:
    """A model that answers every call with a transient 503 (more than any retry budget)."""
    return ScriptedClient([LLMTransientError("503 model overloaded")] * 10)


def _retry_fallback(recovers: bool) -> Callable[[Path, TextIO], tuple[RunSummary, ArtifactStore, Path]]:
    def run(run_dir: Path, out: TextIO) -> tuple[RunSummary, ArtifactStore, Path]:
        fallback_client = ScriptedClient([], default=js(ANSWERS["developer"])) if recovers else _busy_model()
        return _execute(run_dir, out, _clients(developer=_busy_model()), graph=_with_developer_fallback(),
                        extra_agents={"developer-fallback": DeveloperAgent(fallback_client)})
    return run


def _safe_stop(run_dir: Path, out: TextIO) -> tuple[RunSummary, ArtifactStore, Path]:
    return _execute(run_dir, out, _clients(), config=Config(step_budget=4))


def _replan_on_change(run_dir: Path, out: TextIO) -> tuple[RunSummary, ArtifactStore, Path]:
    """Finish a run, then the human changes the request: only what actually changes is redone."""
    revised = {**ANSWERS["requirements"], "requirements": [
        *ANSWERS["requirements"]["requirements"],
        {"id": "R3", "text": "Reject targets on a malware domain denylist", "category": "security",
         "priority": "should", "acceptance_criteria": ["denylisted domain -> 422"]}]}
    clients = _clients(requirements=ScriptedClient([js(ANSWERS["requirements"]), js(revised)]))
    agents = build_agents(clients, verifier=_scripted_verifier())
    store = ArtifactStore(run_dir / "artifacts")
    store.put("request", "Build a URL shortener (scripted demo).", produced_by="human:requester")
    log_path, state = run_dir / "events.jsonl", run_dir / "state.json"

    def orchestrator(log: ConsoleEventLog, run_id: str) -> Orchestrator:
        return Orchestrator(sdlc_graph(), agents, store, log, run_id, policy=YamlPolicy.from_file(DEFAULT_POLICY),
                            approver=ScriptedApprover(default=AUTO), state_path=state)

    with ConsoleEventLog(log_path, out=out) as log:
        orchestrator(log, f"failures-{run_dir.name}").run()
        print("\n>>> The human changes the request (adds a denylist). Re-planning from the change:",
              file=out, flush=True)
        changed = store.put("request", "Build a URL shortener that also rejects denylisted domains.",
                            produced_by="human:requester")
        again = orchestrator(log, f"failures-{run_dir.name}-replan")  # resumes the saved state
        again.replan(["request"], cause=None)
        summary = again.run()
        print(f">>> request is now {changed.artifact.id}. Every node reading a changed input re-ran; "
              "code and tests came out identical (same content hash), so verify was skipped",
              file=out, flush=True)
    return summary, store, log_path


CASES: dict[str, Case] = {c.name: c for c in (
    Case("replan-on-change", "A requirement changes after the run finished",
         "dynamic re-planning: only nodes whose inputs changed re-run; unchanged outputs stop the cascade",
         _replan_on_change),
    Case("policy-block", "Generated code contains a hard-coded secret",
         "post-run policy denies the commit; no retry; the secret never reaches the audit log", _policy_block),
    Case("human-reject", "A human rejects the architecture",
         "fail closed: the design is not committed and nothing downstream runs", _human_reject),
    Case("retry-fallback-recover", "The developer model keeps failing; a fallback model works",
         "bounded retries (3 attempts), then the fallback agent recovers the run", _retry_fallback(True)),
    Case("retry-fallback-rollback", "The developer model and its fallback both fail",
         "retries and fallback exhausted: node rolled back, run halted, nothing committed",
         _retry_fallback(False)),
    Case("safe-stop", "The run exceeds its step budget",
         "safe-stop: in-flight work discarded, nothing further runs or is committed", _safe_stop),
)}


def run_case(name: str, run_dir: str | Path, out: TextIO = sys.stdout) -> FailureResult:
    case = CASES[name]
    directory = prepare_run_dir(run_dir)
    print(f"\n### Failure demo: {case.name}: {case.title}\n    Expect: {case.shows}\n"
          f"    (model answers scripted; orchestrator, policy and gates real)", file=out, flush=True)
    summary, store, log_path = case.run(directory, out)
    print_summary(summary, directory, None, out)
    report = write_report(log_path, graph=sdlc_graph(), title=f"Warden failure demo: {case.name}")
    print(f"Run report:       {report}", file=out, flush=True)
    return FailureResult(summary, store, log_path, directory)
