"""Consistency review: generated docs and design must be true of the code.

Found by an external review of the recorded .NET runs: the docs claimed click counters update
"atomically" while the code did a read-then-write, and a "purely additive migration" was really
``EnsureCreatedAsync()``, which never adds a table to an existing database. Tests on fresh
databases passed and the release reviewer approved, because nothing compared claims with code.
"""

import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from demo.fixtures import ANSWERS, DOTNET_CODE
from warden.agent import AgentContext, AgentResult
from warden.agents import (
    DocsAgent,
    ReviewerAgent,
    brownfield_graph,
    build_agents,
    sdlc_graph,
)
from warden.agents.base import AgentOutputError
from warden.approval import ScriptedApprover
from warden.artifacts import ArtifactStore
from warden.bundle import pack
from warden.eventlog import EventLog
from warden.llm import ScriptedClient
from warden.models import EventType, RunState
from warden.orchestrator import Orchestrator
from warden.policy import YamlPolicy
from warden.stacks import DOTNET, PYTHON
from warden.tools import Check, CommandResult, VerifyAgent, review_gate

DEFAULT_POLICY = Path(__file__).resolve().parent.parent / "policies" / "default.yaml"

LOST_UPDATE = """public async Task<string?> VisitAsync(string code)
{
    var link = await _db.ShortLinks.FindAsync(code);
    if (link is null) return null;
    link.ClickCount += 1;
    await _db.SaveChangesAsync();
    return link.OriginalUrl;
}
"""
ATOMIC = """public async Task<string?> VisitAsync(string code)
{
    await _db.ShortLinks.Where(l => l.Code == code)
        .ExecuteUpdateAsync(s => s.SetProperty(l => l.ClickCount, l => l.ClickCount + 1));
    return await _db.ShortLinks.Where(l => l.Code == code).Select(l => l.OriginalUrl).FirstOrDefaultAsync();
}
"""


def js(obj: Any) -> str:
    return json.dumps(obj)


def review(*findings: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "Checked docs and design against the code.", "findings": list(findings)}


def finding(severity: str = "blocking", owner: str = "docs", evidence: str = "README.md") -> dict[str, Any]:
    return {"severity": severity, "owner": owner, "claim": "README: counters update atomically",
            "reality": "LinkService increments with read-then-write", "evidence": evidence,
            "fix": "describe the real behaviour, or make the update atomic"}


class DotnetReviewRuleTests(unittest.TestCase):
    """Deterministic rules: no model involved, so a reviewer cannot be talked out of them."""

    def rules(self, files: dict[str, str]) -> list[dict[str, Any]]:
        return DOTNET.review_rules(files)

    def test_read_then_write_counter_is_flagged_with_its_line(self) -> None:
        (found,) = self.rules({"src/App/Services/LinkService.cs": LOST_UPDATE})
        self.assertEqual((found["severity"], found["owner"]), ("blocking", "developer"))
        self.assertIn("src/App/Services/LinkService.cs:5", found["evidence"])
        self.assertIn("ExecuteUpdateAsync", found["fix"])

    def test_any_click_or_hit_counter_counts(self) -> None:
        # the brownfield output also had `dailyClick.Clicks += 1`
        found = self.rules({"src/App/Services/LinkService.cs": "dailyClick.Clicks += 1;\nhit.Visits++;\n"})
        self.assertEqual([f["evidence"].split(":")[1] for f in found], ["1", "2"])

    def test_ensure_created_is_flagged_because_it_never_upgrades_a_database(self) -> None:
        (found,) = self.rules({"src/App/Program.cs": "await db.Database.EnsureCreatedAsync();\n"})
        self.assertIn("src/App/Program.cs:1", found["evidence"])
        self.assertIn("Migrate", found["fix"])

    def test_clean_code_and_test_code_pass(self) -> None:
        clean = {"src/App/Services/LinkService.cs": ATOMIC,
                 "src/App/Program.cs": "await db.Database.MigrateAsync();\n",
                 "tests/Factory.cs": "db.Database.EnsureCreated();\nhits.Count += 1;\n"}  # tests may
        self.assertEqual(self.rules(clean), [])

    def test_the_python_stack_has_no_dotnet_rules(self) -> None:
        self.assertEqual(PYTHON.review_rules({"src/App/Program.cs": "db.Database.EnsureCreated();"}), [])

    def test_the_recorded_dotnet_output_would_have_been_caught(self) -> None:
        repo = Path(__file__).resolve().parent.parent / "docs" / "evidence" / "greenfield" / "repo"
        files = {str(p.relative_to(repo)).replace("\\", "/"): p.read_text("utf-8")
                 for p in (repo / "src").rglob("*.cs") if "obj" not in p.parts and "bin" not in p.parts}
        fixes = " ".join(f["fix"] for f in self.rules(files))
        self.assertIn("ExecuteUpdateAsync", fixes)  # the lost-update counter
        self.assertIn("Migrate", fixes)  # EnsureCreated


class ReviewerAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ArtifactStore(Path(self._tmp.name) / "artifacts")
        self.store.put("spec.json", js(ANSWERS["requirements"]))
        self.store.put("design.json", js(ANSWERS["architect"]))
        self.store.put("code.bundle", pack({"src/App/Services/LinkService.cs": LOST_UPDATE}))
        self.store.put("tests.bundle", pack({"tests/LinkTests.cs": "// tests\n"}))
        self.store.put("docs.bundle", pack({"README.md": "Clicks are counted atomically.\n"}))
        self.consumes = ("spec.json", "design.json", "code.bundle", "tests.bundle", "docs.bundle")

    def run_reviewer(self, *answers: dict[str, Any]) -> tuple[AgentResult, ScriptedClient]:
        llm = ScriptedClient([js(a) for a in answers])
        ctx = AgentContext("run1", "reviewer", 1, self.store, self.consumes)
        return ReviewerAgent(llm, stack=DOTNET).run(ctx), llm

    def test_prompt_shows_code_docs_and_design_side_by_side(self) -> None:
        _, llm = self.run_reviewer(review())
        prompt = llm.calls[0].prompt
        for marker in ("link.ClickCount += 1", "counted atomically", "## design.json"):
            self.assertIn(marker, prompt)

    def test_rule_findings_are_added_even_when_the_model_sees_nothing(self) -> None:
        result, _ = self.run_reviewer(review())
        findings = json.loads(result.outputs["review.json"])["findings"]
        self.assertEqual([(f["source"], f["owner"]) for f in findings], [("rule", "developer")])
        self.assertIn("1 blocking", result.summary)

    def test_findings_must_cite_files_that_exist(self) -> None:
        with self.assertRaises(AgentOutputError):
            self.run_reviewer(review(finding(evidence="src/Imaginary.cs")),
                              review(finding(evidence="src/Imaginary.cs")))

    def test_gate_sends_each_blocking_finding_to_its_owner_only(self) -> None:
        result, _ = self.run_reviewer(review(finding(owner="docs"), finding(severity="minor", owner="tester")))
        gate = review_gate(result)
        self.assertFalse(gate.ok)
        self.assertEqual(gate.replan, ("developer", "docs"))  # the rule finding + the docs finding
        self.assertIn("counters update atomically", gate.reason)
        self.assertNotIn("tester", gate.replan)  # minor findings are reported, not blocking

    def test_gate_passes_when_nothing_blocks(self) -> None:
        self.store.put("code.bundle", pack({"src/App/Services/LinkService.cs": ATOMIC}))
        result, _ = self.run_reviewer(review(finding(severity="minor")))
        self.assertTrue(review_gate(result).ok)


class GraphTests(unittest.TestCase):
    def test_review_graph_documents_verified_code_then_reviews_before_release(self) -> None:
        for graph in (sdlc_graph(review=True), brownfield_graph(review=True)):
            nodes = graph.nodes
            self.assertEqual(nodes["docs"].deps, ("verify",))
            self.assertIn("code.bundle", nodes["docs"].consumes)
            self.assertEqual(nodes["reviewer"].deps, ("docs",))
            self.assertEqual(nodes["reviewer"].on_failure_replan, ("developer", "tester", "docs"))
            self.assertIn("reviewer", nodes["release"].deps)
            self.assertIn("review.json", nodes["release"].consumes)

    def test_without_review_the_graphs_are_unchanged(self) -> None:
        # the recorded scenarios replay with these graphs: their prompts must not move
        for graph in (sdlc_graph(), brownfield_graph()):
            self.assertNotIn("reviewer", graph.nodes)
            self.assertEqual(graph.nodes["docs"].deps, ("architect",))
            self.assertNotIn("code.bundle", graph.nodes["docs"].consumes)


class DocsFromCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ArtifactStore(Path(self._tmp.name) / "artifacts")
        self.store.put("spec.json", js(ANSWERS["requirements"]))
        self.store.put("design.json", js(ANSWERS["architect"]))
        self.store.put("code.bundle", DOTNET_CODE_BUNDLE)

    def prompt(self, *consumes: str) -> str:
        llm = ScriptedClient([js(ANSWERS["docs"])])
        DocsAgent(llm).run(AgentContext("run1", "docs", 1, self.store, consumes))
        return llm.calls[0].prompt

    def test_with_the_code_the_docs_agent_is_told_to_describe_what_it_does(self) -> None:
        prompt = self.prompt("spec.json", "design.json", "code.bundle")
        self.assertIn("never claim", prompt)
        self.assertIn("## code.bundle", prompt)

    def test_without_the_code_the_prompt_is_unchanged(self) -> None:
        self.assertNotIn("never claim", self.prompt("spec.json", "design.json"))


DOTNET_CODE_BUNDLE = pack({f["path"]: f["content"] for f in DOTNET_CODE["files"]})


class ReviewLoopTests(unittest.TestCase):
    """End to end through the orchestrator: a docs-only finding re-runs docs, not the code."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.store = ArtifactStore(root / "artifacts")
        self.store.put("request", "build a url shortener", produced_by="human:alice")
        self.log = EventLog(root / "events.jsonl")
        self.addCleanup(self.log.close)

    def test_docs_finding_is_fixed_by_docs_alone(self) -> None:
        clients = {role: ScriptedClient([], default=js(answer)) for role, answer in ANSWERS.items()}
        clients["reviewer"] = ScriptedClient([js(review(finding(owner="docs", evidence="README.md"))),
                                              js(review())])

        def passing(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
            return CommandResult(tuple(argv), 0, "ok", 1)

        agents = build_agents(clients, verifier=VerifyAgent((Check("tests", ("pytest",)),), runner=passing))
        summary = Orchestrator(sdlc_graph(review=True), agents, self.store, self.log, "run1",
                               policy=YamlPolicy.from_file(DEFAULT_POLICY), approver=ScriptedApprover()).run()
        self.assertEqual(summary.state, RunState.SUCCEEDED, summary.reason)
        self.assertEqual((len(clients["developer"].calls), len(clients["docs"].calls)), (1, 2))
        self.assertIn("counters update atomically", clients["docs"].calls[1].prompt)  # the finding reached docs
        replans = [e for e in self.log.read_all() if e.type == EventType.REPLAN.value and e.data.get("feedback_to")]
        self.assertEqual([e.data["feedback_to"] for e in replans], [["docs"]])
