import copy
import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from warden.agent import AgentContext
from warden.agents import (
    AgentOutputError,
    ArchitectAgent,
    DeveloperAgent,
    DocsAgent,
    ReleaseAgent,
    RequirementsAgent,
    TesterAgent,
    build_agents,
    sdlc_graph,
)
from warden.approval import ScriptedApprover
from warden.artifacts import ArtifactStore
from warden.bundle import pack, unpack
from warden.eventlog import EventLog
from warden.llm import LLMError, ScriptedClient
from warden.models import RunState
from warden.orchestrator import Orchestrator
from warden.policy import YamlPolicy
from warden.tools import Check, CommandResult, VerifyAgent

DEFAULT_POLICY = Path(__file__).resolve().parent.parent / "policies" / "default.yaml"


# ---------------------------------------------------------------- valid model outputs
def spec(**overrides: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "title": "URL shortener",
        "problem_statement": "Turn long URLs into short codes that redirect.",
        "requirements": [
            {"id": "R1", "text": "POST /shorten returns a short code", "category": "functional",
             "priority": "must", "acceptance_criteria": ["valid URL -> 201 with code"]},
            {"id": "R2", "text": "GET /{code} redirects", "category": "functional",
             "priority": "must", "acceptance_criteria": ["known code -> 307"]},
            {"id": "R3", "text": "Reject private-network targets", "category": "security",
             "priority": "should", "acceptance_criteria": ["http://127.0.0.1 -> 422"]},
        ],
        "assumptions": ["single region"],
        "open_questions": [],
        "out_of_scope": ["user accounts"],
    }
    out.update(overrides)
    return out


def question(blocking: bool) -> dict[str, Any]:
    return {"id": "Q1", "question": "How long should links live?", "why_it_matters": "storage",
            "blocking": blocking, "proposed_default": "forever"}


def design(**overrides: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "overview": "FastAPI app over SQLite.",
        "components": [{"name": "api", "responsibility": "HTTP layer", "module": "app/main.py"}],
        "api": [{"method": "POST", "path": "/shorten", "summary": "create", "status_codes": [201, 422],
                 "requirement_ids": ["R1"]}],
        "data_model": [{"name": "Link", "fields": ["code", "url"]}],
        "decisions": [{"id": "D1", "title": "Storage", "choice": "sqlite3", "rationale": "no server",
                       "alternatives": ["postgres"]}],
        "tasks": [
            {"id": "T1", "title": "Shorten endpoint", "owner": "developer", "requirement_ids": ["R1"],
             "depends_on": []},
            {"id": "T2", "title": "Redirect endpoint", "owner": "developer", "requirement_ids": ["R2"],
             "depends_on": ["T1"]},
            {"id": "T3", "title": "API tests", "owner": "tester", "requirement_ids": ["R1", "R2"],
             "depends_on": ["T1"]},
        ],
        "risks": [{"risk": "code collisions", "mitigation": "unique index + retry"}],
    }
    out.update(overrides)
    return out


def files(mapping: dict[str, str], notes: str = "") -> dict[str, Any]:
    return {"files": [{"path": p, "content": c} for p, c in mapping.items()], "notes": notes}


CODE = files({"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n", "requirements.txt": "fastapi\n"})
TESTS = files({"tests/test_api.py": "def test_r1():\n    assert True\n"})
DOCS = files({"README.md": "# Shortener\n", "docs/api.md": "POST /shorten\n"})
REVIEW = {"recommendation": "ready", "summary": "All must requirements covered.",
          "checklist": [{"item": "R1 implemented", "status": "pass", "evidence": "app/main.py"}],
          "blocking_issues": []}


def js(obj: Any) -> str:
    return json.dumps(obj)


class AgentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = ArtifactStore(self.root / "artifacts")
        self.store.put("request", "build a url shortener", produced_by="human:alice")

    def ctx(self, *consumes: str, feedback: str | None = None) -> AgentContext:
        return AgentContext("run1", "n1", 1, self.store, consumes, feedback)


class StructuredOutputTests(AgentTestCase):
    def test_valid_output_becomes_canonical_json_artifact(self) -> None:
        result = RequirementsAgent(ScriptedClient([js(spec())])).run(self.ctx("request"))
        self.assertEqual(list(result.outputs), ["spec.json"])
        self.assertEqual(json.loads(result.outputs["spec.json"])["requirements"][0]["id"], "R1")
        self.assertEqual(result.kind, "requirements")
        self.assertIn("3 requirements", result.summary)
        self.assertEqual(result.escalate, "")

    def test_prompt_carries_inputs_schema_and_json_mode(self) -> None:
        llm = ScriptedClient([js(spec())])
        RequirementsAgent(llm).run(self.ctx("request"))
        request = llm.calls[0]
        self.assertTrue(request.json_mode)
        self.assertIn("build a url shortener", request.prompt)
        self.assertIn('"open_questions"', request.prompt)  # JSON schema is in the prompt
        self.assertTrue(request.system)

    def test_markdown_fenced_json_is_accepted(self) -> None:
        result = RequirementsAgent(ScriptedClient(["```json\n" + js(spec()) + "\n```"])).run(self.ctx("request"))
        self.assertIn("spec.json", result.outputs)

    def test_invalid_output_is_repaired_with_error_feedback(self) -> None:
        bad = spec(requirements=[{"id": "R1", "text": "x", "category": "functional", "priority": "urgent",
                                  "acceptance_criteria": ["y"]}])
        llm = ScriptedClient([js(bad), js(spec())])
        RequirementsAgent(llm).run(self.ctx("request"))
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("priority", llm.calls[1].prompt)  # the validation error is fed back
        self.assertIn("rejected", llm.calls[1].prompt)

    def test_still_invalid_after_repair_raises(self) -> None:
        llm = ScriptedClient(["not json", "still not json"])
        with self.assertRaises(AgentOutputError):
            RequirementsAgent(llm).run(self.ctx("request"))
        self.assertEqual(len(llm.calls), 2)  # 1 attempt + 1 repair, bounded

    def test_llm_errors_propagate_for_orchestrator_retry(self) -> None:
        with self.assertRaises(LLMError):
            RequirementsAgent(ScriptedClient([LLMError("down")])).run(self.ctx("request"))

    def test_human_feedback_is_in_the_prompt(self) -> None:
        llm = ScriptedClient([js(spec())])
        RequirementsAgent(llm).run(self.ctx("request", feedback="Q1: links expire after 30 days"))
        self.assertIn("links expire after 30 days", llm.calls[0].prompt)


class RequirementsAgentTests(AgentTestCase):
    def test_blocking_questions_escalate_to_a_human(self) -> None:
        result = RequirementsAgent(ScriptedClient([js(spec(open_questions=[question(True)]))])).run(
            self.ctx("request"))
        self.assertIn("1 blocking", result.escalate)
        self.assertIn("How long should links live?", result.summary)

    def test_non_blocking_questions_proceed_on_proposed_defaults(self) -> None:
        result = RequirementsAgent(ScriptedClient([js(spec(open_questions=[question(False)]))])).run(
            self.ctx("request"))
        self.assertEqual(result.escalate, "")

    def test_duplicate_requirement_ids_are_rejected(self) -> None:
        dup = spec()
        dup["requirements"][1]["id"] = "R1"
        with self.assertRaises(AgentOutputError):
            RequirementsAgent(ScriptedClient([js(dup)] * 2)).run(self.ctx("request"))


class ArchitectAgentTests(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.put("spec.json", js(spec()))

    def run_with(self, *outputs: dict[str, Any]) -> Any:
        return ArchitectAgent(ScriptedClient([js(o) for o in outputs])).run(self.ctx("spec.json"))

    def test_valid_design_with_traceable_tasks(self) -> None:
        result = self.run_with(design())
        self.assertIn("3 tasks", result.summary)

    def test_unknown_requirement_reference_is_rejected(self) -> None:
        bad = design()
        bad["tasks"][0]["requirement_ids"] = ["R9"]
        with self.assertRaisesRegex(AgentOutputError, "R9"):
            self.run_with(bad, bad)

    def test_every_must_requirement_needs_a_task(self) -> None:
        bad = design()
        bad["tasks"] = [t for t in bad["tasks"] if t["id"] == "T1"]
        with self.assertRaisesRegex(AgentOutputError, "R2"):
            self.run_with(bad, bad)

    def test_task_dependency_cycles_are_rejected(self) -> None:
        bad = copy.deepcopy(design())
        bad["tasks"][0]["depends_on"] = ["T2"]
        with self.assertRaisesRegex(AgentOutputError, "cycle"):
            self.run_with(bad, bad)


class BundleAgentTests(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.put("spec.json", js(spec()))
        self.store.put("design.json", js(design()))

    def test_developer_emits_code_bundle(self) -> None:
        result = DeveloperAgent(ScriptedClient([js(CODE)])).run(self.ctx("spec.json", "design.json"))
        self.assertEqual(set(unpack(result.outputs["code.bundle"])), {"app/main.py", "requirements.txt"})

    def test_developer_may_not_write_tests_and_must_pin_dependencies(self) -> None:
        for bad in (files({"app/main.py": "x", "requirements.txt": "fastapi\n", "tests/test_x.py": "x"}),
                    files({"app/main.py": "x"})):
            with self.subTest(paths=[f["path"] for f in bad["files"]]), self.assertRaises(AgentOutputError):
                DeveloperAgent(ScriptedClient([js(bad)] * 2)).run(self.ctx("spec.json", "design.json"))

    def test_tester_writes_only_tests(self) -> None:
        TesterAgent(ScriptedClient([js(TESTS)])).run(self.ctx("spec.json", "design.json"))
        with self.assertRaises(AgentOutputError):
            TesterAgent(ScriptedClient([js(CODE)] * 2)).run(self.ctx("spec.json", "design.json"))

    def test_docs_needs_readme_and_stays_in_docs(self) -> None:
        DocsAgent(ScriptedClient([js(DOCS)])).run(self.ctx("spec.json", "design.json"))
        for bad in (files({"docs/a.md": "x"}), files({"README.md": "x", "app/x.py": "y"})):
            with self.subTest(bad=bad), self.assertRaises(AgentOutputError):
                DocsAgent(ScriptedClient([js(bad)] * 2)).run(self.ctx("spec.json", "design.json"))

    def test_unsafe_paths_are_rejected(self) -> None:
        bad = files({"../evil.py": "x", "requirements.txt": "fastapi\n"})
        with self.assertRaises(AgentOutputError):
            DeveloperAgent(ScriptedClient([js(bad)] * 2)).run(self.ctx("spec.json", "design.json"))

    def test_bundle_inputs_are_shown_as_files_in_the_prompt(self) -> None:
        self.store.put("code.bundle", pack({"app/main.py": "APP_MARKER = 1\n"}))
        llm = ScriptedClient([js(REVIEW)])
        ReleaseAgent(llm).run(self.ctx("spec.json", "code.bundle"))
        self.assertIn("app/main.py", llm.calls[0].prompt)
        self.assertIn("APP_MARKER = 1", llm.calls[0].prompt)


class ReleaseAgentTests(AgentTestCase):
    def test_ready_with_failing_check_is_inconsistent(self) -> None:
        bad = copy.deepcopy(REVIEW)
        bad["checklist"][0]["status"] = "fail"
        with self.assertRaises(AgentOutputError):
            ReleaseAgent(ScriptedClient([js(bad)] * 2)).run(self.ctx("request"))

    def test_summary_states_recommendation_for_the_human(self) -> None:
        result = ReleaseAgent(ScriptedClient([js(REVIEW)])).run(self.ctx("request"))
        self.assertIn("ready", result.summary)


class PipelineTests(AgentTestCase):
    """All six agents through the real orchestrator, default policy, scripted humans."""

    def run_pipeline(self, approver: ScriptedApprover, req_answers: list[str],
                     verify_codes: tuple[int, ...] = ()) -> Any:
        clients = {
            "requirements": ScriptedClient(req_answers),
            "architect": ScriptedClient([], default=js(design())),
            "developer": ScriptedClient([], default=js(CODE)),
            "tester": ScriptedClient([], default=js(TESTS)),
            "docs": ScriptedClient([], default=js(DOCS)),
            "release": ScriptedClient([], default=js(REVIEW)),
        }
        codes = list(verify_codes)

        def runner(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
            return CommandResult(tuple(argv), codes.pop(0) if codes else 0, "FAILED test_r1 - KeyError", 1)

        log = EventLog(self.root / "events.jsonl")
        self.addCleanup(log.close)
        agents = build_agents(clients, verifier=VerifyAgent((Check("tests", ("pytest",)),), runner=runner))
        orch = Orchestrator(sdlc_graph(), agents, self.store, log, "run1",
                            policy=YamlPolicy.from_file(DEFAULT_POLICY), approver=approver)
        return orch.run(), clients

    def test_failed_verification_sends_developer_and_tester_back(self) -> None:
        summary, clients = self.run_pipeline(ScriptedApprover(), [js(spec())], verify_codes=(1, 0))
        self.assertEqual(summary.state, RunState.SUCCEEDED, summary.reason)
        for role, hint in (("developer", "cannot change tests"), ("tester", "Do not weaken")):
            prompts = [c.prompt for c in clients[role].calls]
            self.assertEqual(len(prompts), 2, role)
            self.assertIn("FAILED test_r1 - KeyError", prompts[1])
            self.assertIn(hint, prompts[1])  # role-specific repair guidance
            self.assertNotIn(hint, prompts[0])  # first prompt unchanged: recordings stay valid
            self.assertIn("Your previous", prompts[1])  # revises its own last output
        self.assertIn("app = FastAPI()", clients["developer"].calls[1].prompt)
        self.assertEqual(len(clients["docs"].calls), 1)
        report = self.store.latest("verify_report.json")
        assert report is not None
        self.assertTrue(json.loads(self.store.read_text(report))["passed"])

    def test_greenfield_pipeline_runs_end_to_end_with_human_gates(self) -> None:
        approver = ScriptedApprover()
        summary, _ = self.run_pipeline(approver, [js(spec())])
        self.assertEqual(summary.state, RunState.SUCCEEDED, summary.reason)
        for name in ("spec.json", "design.json", "code.bundle", "tests.bundle", "docs.bundle",
                     "verify_report.json", "release_review.json"):
            self.assertIsNotNone(self.store.latest(name), name)
        gated = {r.node_id for r in approver.requests}
        # architect + release always; developer because requirements.txt is a protected path
        self.assertEqual(gated, {"architect", "developer", "release"})

    def test_ambiguous_request_waits_for_human_answers_then_replans(self) -> None:
        from warden.approval import ApprovalDecision
        approver = ScriptedApprover({"requirements": [
            ApprovalDecision("changes", "alice", "Q1: links expire after 30 days"),
            ApprovalDecision("approve", "alice")]})
        summary, clients = self.run_pipeline(
            approver, [js(spec(open_questions=[question(True)])), js(spec(assumptions=["30-day expiry"]))])
        self.assertEqual(summary.state, RunState.SUCCEEDED, summary.reason)
        self.assertIn("30 days", clients["requirements"].calls[1].prompt)
        spec_art = self.store.latest("spec.json")
        assert spec_art is not None
        self.assertIn("30-day expiry", self.store.read_text(spec_art))

    def test_build_agents_accepts_one_shared_client(self) -> None:
        agents = build_agents(ScriptedClient([]))
        self.assertEqual(set(agents), {n.agent for n in sdlc_graph().nodes.values()})
