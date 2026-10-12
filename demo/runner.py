"""Run one scenario end to end: request -> governed SDLC graph -> verified, approved repo."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TextIO

from demo.console import ConsoleApprover, ConsoleEventLog, roles_for, wrap_roles
from warden.agents import (
    BASELINE,
    ROLE_CLASSES,
    brownfield_graph,
    build_agents,
    sdlc_graph,
)
from warden.approval import ApprovalDecision, Approver, ScriptedApprover
from warden.artifacts import ArtifactStore
from warden.bundle import is_bundle, pack, unpack
from warden.dag import Graph
from warden.eventlog import EventLog
from warden.llm import LLMClient
from warden.models import EventType, RunState
from warden.orchestrator import Orchestrator, RunSummary
from warden.policy import YamlPolicy
from warden.report import write_report
from warden.stacks import DOTNET, PYTHON, StackProfile
from warden.tools import GitRepo, VerifyAgent, collect_bundles, publish
from warden.tools.git import ANCHOR_TRAILER

ROOT = Path(__file__).resolve().parent.parent
REQUESTS = Path(__file__).resolve().parent / "requests"
BASELINES = Path(__file__).resolve().parent / "baselines"
ANSWERS = Path(__file__).resolve().parent / "answers"
DEFAULT_POLICY = ROOT / "policies" / "default.yaml"
RUN_MARKER = ".warden-run"
ROLES = tuple(cls.name for cls in ROLE_CLASSES)


PUBLISHED = ("code.bundle", "tests.bundle", "docs.bundle")


@dataclass(frozen=True)
class Scenario:
    name: str
    request: str
    graph: Callable[[], Graph] = sdlc_graph
    baseline: dict[str, str] | None = None  # existing repo files (brownfield), path -> text
    answers: str | None = None  # scripted human answers to clarifying questions (ambiguous)
    stack: StackProfile = PYTHON  # technology of the generated project (recorded scenarios: Python)


# scenario -> (baseline snapshot under demo/baselines, graph); others are greenfield-shaped
_BROWNFIELD = {"brownfield": ("url-shortener-dotnet", brownfield_graph)}
# Every scenario builds a .NET product except this one, kept to show the stacks are pluggable
# (and because its real-model recordings were made with the Python stack).
_PYTHON_SCENARIOS = {"greenfield-python"}


class BaselineMissing(ValueError):
    """The brownfield scenario needs a snapshot of an existing .NET app first."""


@dataclass(frozen=True)
class ScenarioResult:
    summary: RunSummary
    run_dir: Path
    log_path: Path
    commit: str | None
    repo: Path | None


def load_scenario(name: str) -> Scenario:
    path = REQUESTS / f"{name}.md"
    if not path.is_file():
        available = sorted(p.stem for p in REQUESTS.glob("*.md"))
        raise ValueError(f"unknown scenario {name!r}; available: {', '.join(available)}")
    request = path.read_text(encoding="utf-8")
    stack = PYTHON if name in _PYTHON_SCENARIOS else DOTNET
    if name in _BROWNFIELD:
        snapshot, graph = _BROWNFIELD[name]
        baseline_file = BASELINES / f"{snapshot}.json"
        if not baseline_file.is_file():
            raise BaselineMissing(
                f"The brownfield scenario extends an existing .NET app, and {baseline_file} does not exist yet.\n"
                "Create it from a successful greenfield run:\n"
                "  python run_demo.py greenfield --record --auto-approve\n"
                "  python -m demo.baseline runs/greenfield")
        baseline = unpack(baseline_file.read_text(encoding="utf-8"))
        return Scenario(name, request, graph, baseline, stack=stack)
    answers_file = ANSWERS / f"{name}.md"
    if answers_file.is_file():  # vague request: a human must review (and can answer) the spec
        return Scenario(name, request, partial(sdlc_graph, require_spec_approval=True),
                        answers=answers_file.read_text(encoding="utf-8"), stack=stack)
    return Scenario(name, request, stack=stack)


def make_baseline(run_dir: str | Path, name: str = "url-shortener-dotnet") -> Path:
    """Snapshot a finished run's code, tests and docs as the 'existing app' for brownfield."""
    store = ArtifactStore(Path(run_dir) / "artifacts")
    files = collect_bundles(store, PUBLISHED)
    BASELINES.mkdir(parents=True, exist_ok=True)
    target = BASELINES / f"{name}.json"
    target.write_text(pack(files), encoding="utf-8")
    return target


AUTO_APPROVE = ApprovalDecision("approve", "auto-approve", "--auto-approve")


def scenario_approver(scenario: Scenario, *, auto: bool, name: str = "reviewer") -> Approver:
    """Interactive: a person at the terminal. Auto: approve every gate, except that a scenario
    with scripted answers first sends them back to the requirements agent ("changes")."""
    if not auto:
        return ConsoleApprover(name)
    script = {}
    if scenario.answers:
        script["requirements"] = [ApprovalDecision("changes", "product-owner (scripted)", scenario.answers.strip())]
    return ScriptedApprover(script, default=AUTO_APPROVE)


def _split(files: dict[str, str]) -> dict[str, dict[str, str]]:
    """Existing repo files -> the bundle each agent owns (tests, docs, everything else is code)."""
    out: dict[str, dict[str, str]] = {"code.bundle": {}, "tests.bundle": {}, "docs.bundle": {}}
    for path, text in files.items():
        owner = ("tests.bundle" if path.startswith("tests/")
                 else "docs.bundle" if path == "README.md" or path.startswith("docs/") else "code.bundle")
        out[owner][path] = text
    return out


def _seed_baseline(store: ArtifactStore, files: dict[str, str]) -> None:
    """The existing repo enters twice: an immutable snapshot to scan, and bundles to revise."""
    split = _split(files)
    for name, content in split.items():
        store.put(name, pack(content), kind="baseline", produced_by="human:existing-repo")
    store.put(BASELINE[0], pack(split["code.bundle"]), kind="baseline", produced_by="human:existing-repo")
    store.put(BASELINE[1], pack(split["tests.bundle"]), kind="baseline", produced_by="human:existing-repo")


def prepare_run_dir(path: str | Path) -> Path:
    """Fresh output directory. Only ever deletes a directory this demo created itself."""
    run_dir = Path(path)
    marker = run_dir / RUN_MARKER
    if run_dir.exists():
        if not marker.exists() and any(run_dir.iterdir()):
            raise ValueError(f"{run_dir} exists and was not created by run_demo.py; refusing to delete it")
        # clear everything except the marker, so an interrupted cleanup stays recognisably ours
        for child in run_dir.iterdir():
            if child == marker:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, onerror=_make_writable_and_retry)
            else:
                child.unlink()
    run_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text("Created by run_demo.py; safe to delete.\n", encoding="utf-8")
    return run_dir


def _make_writable_and_retry(func: Callable[[str], object], path: str, _exc: object) -> None:
    """Git marks object files read-only; Windows will not delete those until they are writable."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def run_scenario(
    scenario: Scenario,
    run_dir: str | Path,
    llm: LLMClient | Mapping[str, LLMClient],
    approver: Approver,
    *,
    verifier: VerifyAgent | None = None,
    publish_repo: bool = True,
    policy_path: Path = DEFAULT_POLICY,
    out: TextIO = sys.stdout,
    mode: str = "",  # how the model was used, shown in the report title
) -> ScenarioResult:
    run_dir = prepare_run_dir(run_dir)
    run_id = f"{scenario.name}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    store = ArtifactStore(run_dir / "artifacts")
    store.put("request", scenario.request, kind="request", produced_by="human:requester")
    if scenario.baseline is not None:
        _seed_baseline(store, scenario.baseline)
    clients = dict(llm) if isinstance(llm, Mapping) else roles_for(llm, ROLES)
    log_path = run_dir / "events.jsonl"
    commit: str | None = None
    repo: Path | None = None
    print(f"Warden run {run_id}  (scenario: {scenario.name}, output: {run_dir})", file=out, flush=True)
    git = GitRepo(run_dir / "repo")
    if scenario.baseline is not None and publish_repo:  # history: existing code first, then the change
        publish(store, PUBLISHED, git, "chore: import existing codebase (brownfield baseline)\n")
    with ConsoleEventLog(log_path, out=out) as log:
        orch = Orchestrator(scenario.graph(), build_agents(wrap_roles(clients, out), verifier=verifier, stack=scenario.stack), store, log,
                            run_id, policy=YamlPolicy.from_file(policy_path), approver=approver,
                            state_path=run_dir / "state.json")
        summary = orch.run()
        _export(store, run_dir)
        if summary.state == RunState.SUCCEEDED and publish_repo:
            published = publish(store, PUBLISHED, git, _commit_message(store, log, run_id))
            commit, repo = published.commit, git.root
            log.append(run_id, EventType.NOTE, "orchestrator",
                       data={"published_commit": commit, "files": len(published.files), "repo": "repo"})
    title = f"Warden run report: {scenario.name}" + (f" ({mode})" if mode else "")
    anchors = git.audit_anchors() if (git.root / ".git").exists() else []
    report = write_report(log_path, graph=scenario.graph(), title=title, anchors=anchors)
    print_summary(summary, run_dir, commit, out)
    print(f"Run report:       {report}", file=out, flush=True)
    return ScenarioResult(summary, run_dir, log_path, commit, repo)


def _export(store: ArtifactStore, run_dir: Path) -> None:
    """Latest structured artifacts next to the log, for humans to read (bundles go to the repo)."""
    for name in store.names():
        art = store.latest(name)
        if art is not None and not is_bundle(name):
            (run_dir / name).write_bytes(store.read_bytes(art))


def _commit_message(store: ArtifactStore, log: EventLog, run_id: str) -> str:
    spec_art = store.latest("spec.json")
    spec = json.loads(store.read_text(spec_art)) if spec_art else {}
    title = spec.get("title", "generated feature")
    req_ids = [r["id"] for r in spec.get("requirements", [])]
    approvals = [f"- {e.node_id}: {e.data['verdict']} by {e.actor}"
                 + (f": {' '.join(str(e.data['comment']).split())}" if e.data.get("comment") else "")
                 for e in log.read_all(run_id) if e.type == EventType.APPROVAL_DECIDED.value]
    seq, digest = log.head()  # anchors the audit log in git: a later log rewrite cannot match it
    lines = [f"feat: {title}", "", f"Generated by Warden run {run_id}.",
             f"Requirements: {', '.join(req_ids) or 'n/a'}", "Approvals:", *approvals, "",
             f"{ANCHOR_TRAILER}: {seq} {digest}"]
    return "\n".join(lines) + "\n"


def print_summary(summary: RunSummary, run_dir: Path, commit: str | None, out: TextIO) -> None:
    print("\n" + "-" * 78, file=out)
    print(f"Run {summary.state.value}" + (f": {summary.reason}" if summary.reason else ""), file=out)
    for node, state in summary.nodes.items():
        blocked = f"  blocked: {summary.blocked[node]}" if node in summary.blocked else ""
        print(f"  {node:<12} {state}{blocked}", file=out)
    print(f"Agent executions: {summary.steps}", file=out)
    print(f"Audit log:        {run_dir / 'events.jsonl'}", file=out)
    if commit:
        print(f"Generated repo:   {run_dir / 'repo'} (commit {commit[:10]})", file=out)
    print("-" * 78, file=out, flush=True)
