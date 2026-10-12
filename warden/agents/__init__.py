"""SDLC agents and the workflow graphs (greenfield ``sdlc_graph``, ``brownfield_graph``).

    requirements -> architect (approval) -> developer | tester | docs
                 developer + tester -> verify (tests + lint; failures go back to both)
                 verify + docs -> release (approval)

Agents are adapters: this package may use Pydantic (CLAUDE.md rule 7). Approval on
architect and release is set on the graph itself, so it holds even without a policy;
the policy can add more (e.g. protected paths), never remove it.
"""

from __future__ import annotations

from collections.abc import Mapping

from warden.agent import Agent
from warden.agents.base import AgentOutputError, BundleAgent, StructuredAgent
from warden.agents.roles import (
    STACK,
    ArchitectAgent,
    DeveloperAgent,
    DocsAgent,
    ImpactAgent,
    ReleaseAgent,
    RequirementsAgent,
    TesterAgent,
)
from warden.dag import Graph, NodeSpec
from warden.llm import LLMClient
from warden.tools import REPORT, ScanAgent, VerifyAgent, verify_gate

ROLE_CLASSES = (RequirementsAgent, ImpactAgent, ArchitectAgent, DeveloperAgent, TesterAgent, DocsAgent,
                ReleaseAgent)


def build_agents(llm: LLMClient | Mapping[str, LLMClient], *, repair_attempts: int = 1,
                 verifier: VerifyAgent | None = None) -> dict[str, Agent]:
    """Agents for the LLM roles plus the tool agents (scan, verify).

    Pass one client for every role, or a mapping role -> client to build only those roles
    (the orchestrator rejects a graph that names an agent that was not built).
    """
    agents: dict[str, Agent] = {}
    for cls in ROLE_CLASSES:
        if isinstance(llm, Mapping):
            if cls.name not in llm:
                continue
            client = llm[cls.name]
        else:
            client = llm
        agents[cls.name] = cls(client, repair_attempts=repair_attempts)
    agents["verify"] = verifier or VerifyAgent()
    agents["scan"] = ScanAgent()
    return agents


def sdlc_graph(*, max_retries: int = 2, max_repair_loops: int = 3) -> Graph:
    spec, design = "spec.json", "design.json"
    built = ("code.bundle", "tests.bundle", "docs.bundle")
    return Graph([
        NodeSpec("requirements", "requirements", (), ("request",), (spec,), max_retries=max_retries),
        NodeSpec("architect", "architect", ("requirements",), (spec,), (design,),
                 requires_approval=True, max_retries=max_retries),
        NodeSpec("developer", "developer", ("architect",), (spec, design), ("code.bundle",),
                 max_retries=max_retries),
        NodeSpec("tester", "tester", ("architect",), (spec, design), ("tests.bundle",),
                 max_retries=max_retries),
        NodeSpec("docs", "docs", ("architect",), (spec, design), ("docs.bundle",), max_retries=max_retries),
        # failing tests are deterministic: no blind retry; send the output to both authors, each
        # fixes the files it owns (developer: code, tester: tests)
        NodeSpec("verify", "verify", ("developer", "tester"), ("code.bundle", "tests.bundle"), (REPORT,),
                 exit_gate=verify_gate, max_retries=0, on_failure_replan=("developer", "tester"),
                 max_replans=max_repair_loops),
        NodeSpec("release", "release", ("verify", "docs"), (spec, design, *built, REPORT),
                 ("release_review.json",), requires_approval=True, max_retries=max_retries),
    ])


BASELINE = ("baseline-code.bundle", "baseline-tests.bundle")


def brownfield_graph(*, max_retries: int = 2, max_repair_loops: int = 3) -> Graph:
    """Change an existing codebase.

    Seed: ``request`` (the change), ``baseline-*.bundle`` (immutable snapshot of the existing
    repo, scanned) and ``code/tests/docs.bundle`` (the same files, which the agents revise).

        scan -> requirements -> impact (approval) -> architect (approval) -> developer | tester | docs
             -> verify (failures back to developer + tester) -> release (approval)
    """
    spec, design, impact, codebase = "spec.json", "design.json", "impact.json", "codebase.json"
    built = ("code.bundle", "tests.bundle", "docs.bundle")
    return Graph([
        NodeSpec("scan", "scan", (), BASELINE, (codebase,), max_retries=0),
        NodeSpec("requirements", "requirements", ("scan",), ("request", codebase), (spec,), max_retries=max_retries),
        NodeSpec("impact", "impact", ("requirements",), (spec, codebase), (impact,),
                 requires_approval=True, max_retries=max_retries),
        NodeSpec("architect", "architect", ("impact",), (spec, impact, codebase), (design,),
                 requires_approval=True, max_retries=max_retries),
        NodeSpec("developer", "developer", ("architect",), (spec, design, impact), ("code.bundle",),
                 max_retries=max_retries),
        NodeSpec("tester", "tester", ("architect",), (spec, design, impact), ("tests.bundle",),
                 max_retries=max_retries),
        NodeSpec("docs", "docs", ("architect",), (spec, design), ("docs.bundle",), max_retries=max_retries),
        NodeSpec("verify", "verify", ("developer", "tester"), ("code.bundle", "tests.bundle"), (REPORT,),
                 exit_gate=verify_gate, max_retries=0, on_failure_replan=("developer", "tester"),
                 max_replans=max_repair_loops),
        NodeSpec("release", "release", ("verify", "docs"), (spec, impact, design, *built, REPORT),
                 ("release_review.json",), requires_approval=True, max_retries=max_retries),
    ])


__all__ = [
    "BASELINE",
    "ROLE_CLASSES",
    "STACK",
    "AgentOutputError",
    "ArchitectAgent",
    "BundleAgent",
    "DeveloperAgent",
    "DocsAgent",
    "ImpactAgent",
    "ReleaseAgent",
    "RequirementsAgent",
    "StructuredAgent",
    "TesterAgent",
    "brownfield_graph",
    "build_agents",
    "sdlc_graph",
]
