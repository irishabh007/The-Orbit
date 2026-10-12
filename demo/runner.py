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
from pathlib import Path
from typing import TextIO

from demo.console import ConsoleEventLog, roles_for, wrap_roles
from warden.agents import ROLE_CLASSES, build_agents, sdlc_graph
from warden.approval import Approver
from warden.artifacts import ArtifactStore
from warden.bundle import is_bundle
from warden.eventlog import EventLog
from warden.llm import LLMClient
from warden.models import EventType, RunState
from warden.orchestrator import Orchestrator, RunSummary
from warden.policy import YamlPolicy
from warden.tools import GitRepo, VerifyAgent, publish

ROOT = Path(__file__).resolve().parent.parent
REQUESTS = Path(__file__).resolve().parent / "requests"
DEFAULT_POLICY = ROOT / "policies" / "default.yaml"
RUN_MARKER = ".warden-run"
ROLES = tuple(cls.name for cls in ROLE_CLASSES)


@dataclass(frozen=True)
class Scenario:
    name: str
    request: str
    publish_bundles: tuple[str, ...] = ("code.bundle", "tests.bundle", "docs.bundle")


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
    return Scenario(name, path.read_text(encoding="utf-8"))


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
) -> ScenarioResult:
    run_dir = prepare_run_dir(run_dir)
    run_id = f"{scenario.name}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    store = ArtifactStore(run_dir / "artifacts")
    store.put("request", scenario.request, kind="request", produced_by="human:requester")
    clients = dict(llm) if isinstance(llm, Mapping) else roles_for(llm, ROLES)
    log_path = run_dir / "events.jsonl"
    commit: str | None = None
    repo: Path | None = None
    print(f"Warden run {run_id}  (scenario: {scenario.name}, output: {run_dir})", file=out, flush=True)
    with ConsoleEventLog(log_path, out=out) as log:
        orch = Orchestrator(sdlc_graph(), build_agents(wrap_roles(clients, out), verifier=verifier), store, log,
                            run_id, policy=YamlPolicy.from_file(policy_path), approver=approver,
                            state_path=run_dir / "state.json")
        summary = orch.run()
        _export(store, run_dir)
        if summary.state == RunState.SUCCEEDED and publish_repo:
            git = GitRepo(run_dir / "repo")
            published = publish(store, scenario.publish_bundles, git, _commit_message(store, log, run_id))
            commit, repo = published.commit, git.root
            log.append(run_id, EventType.NOTE, "orchestrator",
                       data={"published_commit": commit, "files": len(published.files), "repo": "repo"})
    _print_summary(summary, run_dir, commit, out)
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
    approvals = [f"- {e.node_id}: {e.data['verdict']} by {e.actor}" for e in log.read_all(run_id)
                 if e.type == EventType.APPROVAL_DECIDED.value]
    lines = [f"feat: {title}", "", f"Generated by Warden run {run_id}.",
             f"Requirements: {', '.join(req_ids) or 'n/a'}", "Approvals:", *approvals]
    return "\n".join(lines) + "\n"


def _print_summary(summary: RunSummary, run_dir: Path, commit: str | None, out: TextIO) -> None:
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
