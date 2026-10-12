"""SDLC agents and the standard workflow graph.

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
    ReleaseAgent,
    RequirementsAgent,
    TesterAgent,
)
from warden.dag import Graph, NodeSpec
from warden.llm import LLMClient
from warden.tools import REPORT, VerifyAgent, verify_gate

ROLE_CLASSES = (RequirementsAgent, ArchitectAgent, DeveloperAgent, TesterAgent, DocsAgent, ReleaseAgent)


def build_agents(llm: LLMClient | Mapping[str, LLMClient], *, repair_attempts: int = 1,
                 verifier: VerifyAgent | None = None) -> dict[str, Agent]:
    """One agent per role. Pass one client for all roles, or a mapping role -> client."""
    agents: dict[str, Agent] = {}
    for cls in ROLE_CLASSES:
        client = llm[cls.name] if isinstance(llm, Mapping) else llm
        agents[cls.name] = cls(client, repair_attempts=repair_attempts)
    agents["verify"] = verifier or VerifyAgent()
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


__all__ = [
    "ROLE_CLASSES",
    "STACK",
    "AgentOutputError",
    "ArchitectAgent",
    "BundleAgent",
    "DeveloperAgent",
    "DocsAgent",
    "ReleaseAgent",
    "RequirementsAgent",
    "StructuredAgent",
    "TesterAgent",
    "build_agents",
    "sdlc_graph",
]
