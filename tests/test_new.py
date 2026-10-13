"""`run_demo.py new`: interactive intake -> discovery Q&A -> spec and plan approval -> build."""

import io
import json
import shutil
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from demo.console import ConsoleApprover, read_idea, render_design, render_spec
from demo.fixtures import DOTNET_ANSWERS, js
from demo.newproject import new_project_scenario, slugify
from demo.runner import run_scenario
from warden.approval import ApprovalRequest
from warden.llm import ScriptedClient
from warden.models import RunState
from warden.stacks import DOTNET
from warden.tools import CommandResult, VerifyAgent

QUESTION = {"id": "Q1", "question": "How long should links live?", "why_it_matters": "storage and expiry",
            "blocking": True, "proposed_default": "never expire"}
OPTIONAL = {"id": "Q2", "question": "Custom aliases?", "why_it_matters": "API shape", "blocking": False,
            "proposed_default": "yes, 3-64 characters"}


def spec_with(*questions: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {**DOTNET_ANSWERS["requirements"], "open_questions": list(questions), **overrides}


def spec_request(spec: dict[str, Any]) -> ApprovalRequest:
    return ApprovalRequest("run", "requirements", "summary", ("spec.json",), "node requires approval",
                           preview=(("spec.json", json.dumps(spec)),))


class Typist:
    """Feeds answers to input() prompts and records what was asked."""

    def __init__(self, *answers: str) -> None:
        self._answers: Iterator[str] = iter(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self._answers)


class IntakeTests(unittest.TestCase):
    def test_idea_is_read_until_an_empty_line(self) -> None:
        typist = Typist("A URL shortener", "for the marketing team", "")
        self.assertEqual(read_idea(typist, io.StringIO()), "A URL shortener\nfor the marketing team")

    def test_empty_idea_is_asked_again(self) -> None:
        self.assertEqual(read_idea(Typist("", "Todo API", ""), io.StringIO()), "Todo API")

    def test_slug(self) -> None:
        self.assertEqual(slugify("  Marketing URL Shortener!! "), "marketing-url-shortener")

    def test_new_project_needs_spec_approval_and_uses_dotnet_by_default(self) -> None:
        scenario = new_project_scenario("Todo API", "a todo api")
        self.assertEqual(scenario.stack, DOTNET)
        self.assertTrue(scenario.graph().nodes["requirements"].requires_approval)


class DiscoveryTests(unittest.TestCase):
    def test_questions_are_asked_one_at_a_time_with_defaults(self) -> None:
        typist = Typist("q", "90 days", "", "Only https targets please")
        out = io.StringIO()
        decision = ConsoleApprover("alice", input_fn=typist, out=out).decide(spec_request(spec_with(QUESTION, OPTIONAL)))
        self.assertEqual(decision.verdict, "changes")  # answers go back to the requirements agent
        self.assertIn("Q1 (How long should links live?): 90 days", decision.comment)
        self.assertIn("Q2 (Custom aliases?): accepted the proposed default: yes, 3-64 characters", decision.comment)
        self.assertIn("Additional notes: Only https targets please", decision.comment)
        self.assertIn("storage and expiry", out.getvalue())  # why it matters is shown
        self.assertIn("never expire", out.getvalue())  # the default is shown

    def test_accepting_all_defaults_goes_straight_to_review(self) -> None:
        # also what keeps an interactive replay on the recorded path (auto-approve accepted defaults)
        out = io.StringIO()
        typist = Typist("d", "a")
        decision = ConsoleApprover("alice", input_fn=typist, out=out).decide(spec_request(spec_with(QUESTION)))
        self.assertEqual((decision.verdict, decision.comment), ("approve", ""))
        self.assertIn("never expire", out.getvalue())  # the defaults being accepted are shown
        self.assertEqual(len(typist.prompts), 2)  # no per-question prompts

    def test_spec_without_questions_goes_to_review_and_approval(self) -> None:
        out = io.StringIO()
        decision = ConsoleApprover("alice", input_fn=Typist("a"), out=out).decide(spec_request(spec_with()))
        self.assertEqual(decision.verdict, "approve")
        shown = out.getvalue()
        self.assertIn("R1", shown)
        self.assertIn("valid URL -> 201 with code", shown)  # acceptance criteria rendered
        self.assertIn("locks", shown)  # the human is told approval locks the spec

    def test_event_feed_waits_while_a_human_is_answering(self) -> None:
        # approvals no longer block the scheduler, so events keep coming during a prompt; they
        # are held back so they do not interrupt what the human is reading
        from demo.console import ConsoleEventLog
        from warden.models import EventType
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, ConsoleEventLog(Path(tmp) / "e.jsonl", out=out) as log:
            def human(prompt: str) -> str:
                log.append("run", EventType.RUN_STARTED, "orchestrator", None, {"reason": "parallel work"})
                self.assertNotIn("parallel work", out.getvalue())  # not while the prompt is open
                return "a"
            ConsoleApprover("alice", input_fn=human, out=out).decide(spec_request(spec_with()))
            self.assertIn("parallel work", out.getvalue())  # shown once the human has answered

    def test_discovery_rounds_are_bounded(self) -> None:
        approver = ConsoleApprover("alice", input_fn=Typist("q", "x", "", "q", "x", "", "a"), out=io.StringIO(),
                                   discovery_rounds=2)
        for _ in range(2):
            self.assertEqual(approver.decide(spec_request(spec_with(QUESTION))).verdict, "changes")
        # third time: no more Q&A; the human reviews and decides (defaults apply if approved)
        self.assertEqual(approver.decide(spec_request(spec_with(QUESTION))).verdict, "approve")


class ReadableArtifactTests(unittest.TestCase):
    def test_spec_and_design_render_for_humans(self) -> None:
        spec_text = render_spec(json.dumps(spec_with(QUESTION)))
        self.assertIn("R1 [must, functional]", spec_text)
        self.assertIn("Q1", spec_text)
        design_text = render_design(json.dumps(DOTNET_ANSWERS["architect"]))
        self.assertIn("POST /shorten", design_text)
        self.assertIn("T1", design_text)


class RoleRouter:
    """Stands in for Gemini: answers each agent by its role (system prompt)."""

    name = "router"
    ROLES = (("requirements engineer", "requirements"), ("software architect", "architect"),
             (".NET engineer", "developer"), ("test engineer", "tester"), ("technical writer", "docs"),
             ("release reviewer", "release"))

    def complete(self, request: Any) -> Any:
        from warden.llm import LLMResponse
        role = next(r for marker, r in self.ROLES if marker in request.system)
        return LLMResponse(js(DOTNET_ANSWERS[role]), "router", "router", 10, 10, 0)


class CliNewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_idea_and_name_can_be_typed(self) -> None:
        from demo import cli
        args = cli._parser().parse_args(["new"])
        scenario = cli._new_scenario(args, Typist("A todo API", "", "Todo Service"), io.StringIO())
        self.assertEqual(scenario.name, "todo-service")
        self.assertIn("A todo API", scenario.request)

    def test_approver_is_the_logged_in_user_and_the_policy_can_be_chosen(self) -> None:
        import getpass
        from unittest import mock

        from demo import cli
        self.assertEqual(cli._parser().parse_args(["greenfield"]).approver, getpass.getuser())
        seen: dict[str, Any] = {}

        def fake_run(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return mock.Mock(summary=mock.Mock(state=RunState.SUCCEEDED))

        governed = Path(cli.ROOT) / "policies" / "governed.yaml"
        with mock.patch.object(cli, "run_scenario", fake_run), mock.patch("sys.stdout", io.StringIO()):
            cli.main(["greenfield", "--auto-approve", "--policy", str(governed)])
        self.assertEqual(seen["policy_path"], governed)

    def test_without_a_key_or_recording_it_explains_and_calls_no_model(self) -> None:
        from unittest import mock

        from demo import cli
        from warden.llm import LLMError
        brief = self.root / "idea.md"
        brief.write_text("a todo api", encoding="utf-8")

        def no_key() -> Any:
            raise LLMError("GEMINI_API_KEY is not set")

        with mock.patch.object(cli, "_gemini", no_key), mock.patch("sys.stderr", io.StringIO()) as err:
            code = cli.main(["new", "--request-file", str(brief), "--name", "Todo", "--out", str(self.root / "runs"),
                             "--recordings", str(self.root / "rec")])
        self.assertEqual(code, 2)
        self.assertIn("GEMINI_API_KEY", err.getvalue())

    @pytest.mark.slow
    @unittest.skipUnless(shutil.which("dotnet"), "needs the .NET SDK")
    def test_cli_new_builds_and_verifies_a_dotnet_api_end_to_end(self) -> None:
        from unittest import mock

        from demo import cli
        brief = self.root / "idea.md"
        brief.write_text("A link shortener for the marketing team.", encoding="utf-8")
        with mock.patch.object(cli, "_gemini", RoleRouter), mock.patch("sys.stdout", io.StringIO()):
            code = cli.main(["new", "--request-file", str(brief), "--name", "Link Shortener", "--auto-approve",
                             "--out", str(self.root / "runs"), "--recordings", str(self.root / "rec")])
        self.assertEqual(code, 0)
        repo = self.root / "runs" / "link-shortener" / "repo"
        self.assertTrue((repo / "src" / "App" / "App.csproj").exists())
        self.assertTrue((repo / "tests" / "Tests.csproj").exists())
        self.assertTrue(list((self.root / "rec" / "link-shortener").glob("*.json")))  # session recorded


class InteractiveNewProjectTests(unittest.TestCase):
    def test_whole_flow_with_a_human_at_the_console(self) -> None:
        def passing(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
            return CommandResult(tuple(argv), 0, "Passed!  - Failed: 0, Passed: 2, Skipped: 0, Total: 2", 1)

        clients = {role: ScriptedClient([], default=js(answer)) for role, answer in DOTNET_ANSWERS.items()}
        clients["requirements"] = ScriptedClient([js(spec_with(QUESTION)), js(spec_with(assumptions=["90-day expiry"]))])
        # discovery answer, no extra notes, then approve spec, plan, project files (x2) and release
        typist = Typist("q", "90 days", "", "a", "a", "a", "a", "a")
        with tempfile.TemporaryDirectory() as tmp:
            result = run_scenario(new_project_scenario("Shortener", "a link shortener"), Path(tmp) / "run",
                                  clients, ConsoleApprover("alice", input_fn=typist, out=io.StringIO()),
                                  verifier=VerifyAgent(DOTNET.verify_checks, runner=passing),
                                  publish_repo=False, out=io.StringIO())
            spec = json.loads((result.run_dir / "spec.json").read_text("utf-8"))
        self.assertEqual(result.summary.state, RunState.SUCCEEDED, result.summary.reason)
        self.assertIn("90 days", clients["requirements"].calls[1].prompt)  # answers reached the agent
        self.assertIn("90-day expiry", spec["assumptions"])
