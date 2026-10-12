"""The six SDLC roles. Prompts are deliberately explicit: they are part of the contract.

Planner is merged into Architect (see HANDOFF): the design carries the task plan,
with every task traced to requirement ids and sequenced by ``depends_on``.
"""

from __future__ import annotations

from collections import Counter

from warden.agents.base import BundleAgent, StructuredAgent
from warden.agents.schemas import Design, FileBundle, ReleaseReview, RequirementsSpec

STACK = (
    "Python 3.11+, FastAPI, the standard-library sqlite3 module for storage, pytest with "
    "fastapi.testclient.TestClient (needs httpx) for tests. Application code lives in the app/ "
    "package (entry point app/main.py exposing `app`); tests live in tests/. Configuration comes "
    "from environment variables; never hard-code secrets."
)


def _requirement_ids(inputs: dict[str, str]) -> tuple[set[str], set[str]] | None:
    """(all ids, must ids) from an upstream spec.json, or None if it is not an input."""
    raw = inputs.get("spec.json")
    if raw is None:
        return None
    spec = RequirementsSpec.model_validate_json(raw)
    return {r.id for r in spec.requirements}, {r.id for r in spec.requirements if r.priority == "must"}


class RequirementsAgent(StructuredAgent[RequirementsSpec]):
    name = "requirements"
    schema = RequirementsSpec
    output = "spec.json"
    kind = "requirements"
    role = ("You are a senior requirements engineer. You turn vague product requests into precise, "
            "testable requirements. You never invent business facts: anything not stated is either an "
            "explicit assumption or an open question.")
    task = """
Normalize the request in `request` into a requirements specification.
- Number requirements R1, R2, ... Each needs testable acceptance criteria (concrete input -> expected outcome).
- Capture implied non-functional needs (security, reliability, observability) as their own requirements.
- Record every assumption you make.
- Ask a clarifying question (Q1, ...) for each real ambiguity. Mark it blocking only if a wrong guess would
  change the API, the data model or the security posture. Always give a sensible proposed_default.
- If human feedback answers questions, fold the answers into the requirements and assumptions and do not
  ask those questions again.
- List what is explicitly out of scope.
"""

    def summarize(self, model: RequirementsSpec) -> str:
        musts = sum(r.priority == "must" for r in model.requirements)
        text = (f"{model.title}: {len(model.requirements)} requirements ({musts} must), "
                f"{len(model.assumptions)} assumptions, {len(model.open_questions)} open question(s)")
        blocking = [q for q in model.open_questions if q.blocking]
        if blocking:
            text += ". Blocking: " + " | ".join(
                f"{q.id}: {q.question} (default: {q.proposed_default})" for q in blocking)
        return text

    def escalation(self, model: RequirementsSpec) -> str:
        blocking = [q.id for q in model.open_questions if q.blocking]
        if not blocking:
            return ""
        return (f"{len(blocking)} blocking clarifying question(s) {', '.join(blocking)}: answer them with "
                "'changes', or approve to accept the proposed defaults")


class ArchitectAgent(StructuredAgent[Design]):
    name = "architect"
    schema = Design
    output = "design.json"
    kind = "design"
    role = ("You are a pragmatic software architect and tech lead. You design the simplest architecture "
            "that meets the requirements, record decisions with the alternatives you rejected, and break "
            "the work into small, traceable tasks.")
    task = f"""
Design the system for `spec.json` and plan the work. Technology constraints: {STACK}
- components: one per module, with its file path.
- api: every endpoint with method, path, status codes and the requirement ids it serves.
- data_model; decisions (D1, ...) with rationale and rejected alternatives; risks with mitigations.
- tasks (T1, ...): small, each owned by developer, tester or docs, citing requirement ids, sequenced with
  depends_on. Every 'must' requirement needs at least one task.
- Tests and docs are written in parallel with the code from this design (contract-first), so pin module
  paths, function names and the API precisely.
"""

    def check(self, model: Design, inputs: dict[str, str]) -> list[str]:
        ids = _requirement_ids(inputs)
        if ids is None:
            return []
        known, musts = ids
        problems = [f"task {t.id} cites unknown requirement {r}"
                    for t in model.tasks for r in t.requirement_ids if r not in known]
        problems += [f"endpoint {e.method} {e.path} cites unknown requirement {r}"
                     for e in model.api for r in e.requirement_ids if r not in known]
        covered = {r for t in model.tasks for r in t.requirement_ids}
        problems += [f"must requirement {r} has no task" for r in sorted(musts - covered)]
        return problems

    def summarize(self, model: Design) -> str:
        owners = Counter(t.owner for t in model.tasks)
        return (f"{len(model.components)} components, {len(model.api)} endpoints, "
                f"{len(model.decisions)} decisions, {len(model.tasks)} tasks "
                f"({', '.join(f'{n} {o}' for o, n in sorted(owners.items()))}), {len(model.risks)} risks")


class DeveloperAgent(BundleAgent):
    name = "developer"
    output = "code.bundle"
    kind = "code"
    role = ("You are a senior Python engineer. You write production-quality, readable, typed code a human "
            "can maintain: small modules, clear names, input validation, and no secrets in code.")
    task = f"""
Implement the design in `design.json` for the requirements in `spec.json`. Constraints: {STACK}
- Return every application file in full (no placeholders, no TODOs), plus requirements.txt listing the
  third-party runtime and test dependencies with minimum versions.
- Follow the module paths and API in the design exactly: tests are written in parallel against it.
- Do not write tests or docs; other agents own those.
"""

    def check(self, model: FileBundle, inputs: dict[str, str]) -> list[str]:
        paths = {f.path for f in model.files}
        problems = [f"{p}: tests belong to the tester" for p in sorted(paths) if p.startswith("tests/")]
        if "requirements.txt" not in paths:
            problems.append("requirements.txt is missing")
        if not any(p.endswith(".py") for p in paths):
            problems.append("no Python source files")
        return problems


class TesterAgent(BundleAgent):
    __test__ = False  # not a pytest test class, despite the name
    name = "tester"
    output = "tests.bundle"
    kind = "test"
    role = ("You are a test engineer. You write fast, deterministic pytest suites that verify behavior "
            "through public interfaces, one test per acceptance criterion where practical.")
    task = f"""
Write tests for the requirements in `spec.json` against the API and modules in `design.json`. The code is
being written in parallel, so rely only on that contract. Constraints: {STACK}
- Files under tests/ only. Name tests after requirement ids (e.g. test_r1_shorten_returns_code).
- Use fastapi.testclient.TestClient against app.main:app and isolate state (e.g. a temporary database).
- No network access and no sleeps.
"""

    def check(self, model: FileBundle, inputs: dict[str, str]) -> list[str]:
        paths = [f.path for f in model.files]
        problems = [f"{p}: tests must live under tests/" for p in paths if not p.startswith("tests/")]
        if not any(p.rsplit("/", 1)[-1].startswith("test_") and p.endswith(".py") for p in paths):
            problems.append("no tests/test_*.py file")
        return problems


class DocsAgent(BundleAgent):
    name = "docs"
    output = "docs.bundle"
    kind = "doc"
    role = ("You are a technical writer for developers. You write concise, accurate docs that let a new "
            "engineer run, use and extend the service.")
    task = """
Document the service described by `spec.json` and `design.json`.
- README.md: purpose, quick start (install, run, test), configuration, API summary with examples.
- docs/*.md: architecture (components, decisions with rationale) and API reference.
- Document only what the design specifies; say so where something is uncertain.
"""

    def check(self, model: FileBundle, inputs: dict[str, str]) -> list[str]:
        paths = [f.path for f in model.files]
        problems = [f"{p}: docs must be README.md or docs/*.md" for p in paths
                    if p != "README.md" and not (p.startswith("docs/") and p.endswith(".md"))]
        if "README.md" not in paths:
            problems.append("README.md is missing")
        return problems


class ReleaseAgent(StructuredAgent[ReleaseReview]):
    name = "release"
    schema = ReleaseReview
    output = "release_review.json"
    kind = "review"
    role = ("You are a release reviewer. You check delivered artifacts against the requirements and report "
            "evidence. You recommend; a human makes the final release decision.")
    task = """
Review every input against `spec.json`. `verify_report.json` holds the real test and lint results:
use it as the evidence for anything about tests passing.
- checklist: one item per 'must' requirement (plus key risks) with status pass, fail or unknown and
  concrete evidence (file, function and test names).
- Use unknown when the evidence is not in the inputs; never guess.
- Recommend 'ready' only if nothing fails and there are no blocking issues.
"""

    def summarize(self, model: ReleaseReview) -> str:
        status = Counter(c.status for c in model.checklist)
        text = (f"Recommendation: {model.recommendation}. {status['pass']}/{len(model.checklist)} checks pass, "
                f"{status['fail']} fail, {status['unknown']} unknown. {model.summary}")
        if model.blocking_issues:
            text += " Blocking: " + "; ".join(model.blocking_issues)
        return text
