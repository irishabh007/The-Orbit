import io
import json
import stat
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

import pytest

from demo.console import ConsoleApprover
from demo.runner import (
    RUN_MARKER,
    load_scenario,
    prepare_run_dir,
    run_scenario,
    scenario_approver,
)
from tests.test_agents import CODE, DOCS, REVIEW, TESTS, design, js, spec
from warden.approval import ApprovalRequest, ScriptedApprover
from warden.llm import RecordReplayClient, ScriptedClient
from warden.models import EventType, RunState
from warden.tools import Check, CommandResult, GitRepo, VerifyAgent

RECORDINGS = Path(__file__).resolve().parent.parent / "recordings"


def scripted_clients() -> dict[str, ScriptedClient]:
    return {
        "requirements": ScriptedClient([], default=js(spec())),
        "architect": ScriptedClient([], default=js(design())),
        "developer": ScriptedClient([], default=js(CODE)),
        "tester": ScriptedClient([], default=js(TESTS)),
        "docs": ScriptedClient([], default=js(DOCS)),
        "release": ScriptedClient([], default=js(REVIEW)),
    }


def passing_verifier() -> VerifyAgent:
    def runner(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        return CommandResult(tuple(argv), 0, "ok", 1)
    return VerifyAgent((Check("tests", ("pytest",)),), runner=runner)


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class RunDirTests(TmpCase):
    def test_fresh_dir_is_created_and_marked(self) -> None:
        run_dir = prepare_run_dir(self.root / "runs" / "greenfield")
        self.assertTrue((run_dir / RUN_MARKER).exists())

    def test_previous_run_is_replaced(self) -> None:
        run_dir = prepare_run_dir(self.root / "r")
        (run_dir / "old.txt").write_text("x", encoding="utf-8")
        prepare_run_dir(self.root / "r")
        self.assertFalse((run_dir / "old.txt").exists())

    def test_previous_run_with_read_only_files_is_replaced(self) -> None:
        # git marks its object files read-only; on Windows plain rmtree cannot delete them
        run_dir = prepare_run_dir(self.root / "r")
        locked = run_dir / "repo" / ".git" / "objects" / "ab" / "cdef"
        locked.parent.mkdir(parents=True)
        locked.write_text("x", encoding="utf-8")
        locked.chmod(stat.S_IREAD)
        prepare_run_dir(self.root / "r")
        self.assertFalse(locked.exists())

    def test_interrupted_cleanup_keeps_the_marker_so_a_rerun_still_works(self) -> None:
        run_dir = prepare_run_dir(self.root / "r")
        (run_dir / "repo").mkdir()

        def rmtree_that_fails_midway(path: Path, **_: object) -> None:
            # what really happened: removing the whole run dir deleted top-level files
            # (the marker) first, then hit a locked file inside repo/.git
            if Path(path) == run_dir:
                (run_dir / RUN_MARKER).unlink()
            raise OSError("locked")

        with mock.patch("demo.runner.shutil.rmtree", side_effect=rmtree_that_fails_midway), \
                self.assertRaises(OSError):
            prepare_run_dir(self.root / "r")
        self.assertTrue((run_dir / RUN_MARKER).exists())
        prepare_run_dir(self.root / "r")  # not refused: still recognisably ours
        self.assertFalse((run_dir / "repo").exists())

    def test_refuses_to_wipe_a_directory_it_did_not_create(self) -> None:
        precious = self.root / "precious"
        precious.mkdir()
        (precious / "thesis.docx").write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            prepare_run_dir(precious)
        self.assertTrue((precious / "thesis.docx").exists())


class ScenarioTests(TmpCase):
    def test_greenfield_request_is_loaded(self) -> None:
        scenario = load_scenario("greenfield")
        self.assertIn("shorten", scenario.request.lower())

    def test_greenfield_runs_end_to_end_without_publishing(self) -> None:
        out = io.StringIO()
        result = run_scenario(load_scenario("greenfield"), self.root / "run", scripted_clients(),
                              ScriptedApprover(), verifier=passing_verifier(), publish_repo=False, out=out)
        self.assertEqual(result.summary.state, RunState.SUCCEEDED, result.summary.reason)
        self.assertTrue(result.log_path.exists())
        self.assertIsNone(result.commit)
        self.assertIn("architect", out.getvalue())  # progress is printed as events happen
        self.assertIn("SUCCEEDED", out.getvalue())

    def test_every_run_writes_a_report_and_metrics(self) -> None:
        result = run_scenario(load_scenario("greenfield"), self.root / "run", scripted_clients(),
                              ScriptedApprover(), verifier=passing_verifier(), publish_repo=False,
                              out=io.StringIO())
        report = (result.run_dir / "report.md").read_text("utf-8")
        self.assertIn("## Reliability metrics", report)
        self.assertIn("```mermaid", report)
        self.assertIn("Hash chain **verified**", report)
        metrics = json.loads((result.run_dir / "metrics.json").read_text("utf-8"))
        self.assertEqual(metrics["run_success_rate"], 1.0)

    @pytest.mark.slow  # spawns a subprocess
    def test_report_can_be_regenerated_from_any_log(self) -> None:
        result = run_scenario(load_scenario("greenfield"), self.root / "run", scripted_clients(),
                              ScriptedApprover(), verifier=passing_verifier(), publish_repo=False,
                              out=io.StringIO())
        import subprocess
        import sys
        target = self.root / "again.md"
        proc = subprocess.run([sys.executable, "-m", "warden.report", str(result.log_path), "-o", str(target)],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("## Reliability metrics", target.read_text("utf-8"))


BROWNFIELD_IMPACT = {
    "summary": "Add daily click analytics.", "change_type": "feature",
    "affected": [{"target": "app/main.py", "kind": "module", "change": "modify", "reason": "new route"},
                 {"target": "GET /analytics/{code}/daily", "kind": "endpoint", "change": "add", "reason": "new"}],
    "risk": "medium", "risks": [{"risk": "data migration", "mitigation": "additive table"}],
    "regression_tests": ["tests/test_api.py"],
}


class AmbiguousScenarioTests(TmpCase):
    def test_vague_request_comes_with_scripted_human_answers(self) -> None:
        scenario = load_scenario("ambiguous")
        self.assertIn("safer", scenario.request)
        self.assertIsNotNone(scenario.answers)
        self.assertTrue(scenario.graph().nodes["requirements"].requires_approval)

    def test_questions_are_answered_by_a_human_and_requirements_replan(self) -> None:
        question = {"id": "Q1", "question": "What does 'safer' mean?", "why_it_matters": "scope",
                    "blocking": True, "proposed_default": "SSRF protection only"}
        clients = {**scripted_clients(),
                   "requirements": ScriptedClient([js(spec(open_questions=[question])),
                                                   js(spec(assumptions=["malware denylist"]))])}
        scenario = load_scenario("ambiguous")
        assert scenario.answers is not None
        result = run_scenario(scenario, self.root / "run", clients, scenario_approver(scenario, auto=True),
                              verifier=passing_verifier(), publish_repo=False, out=io.StringIO())
        self.assertEqual(result.summary.state, RunState.SUCCEEDED, result.summary.reason)
        prompts = [c.prompt for c in clients["requirements"].calls]
        self.assertEqual(len(prompts), 2)
        self.assertIn(scenario.answers.strip().splitlines()[0], prompts[1])
        spec_now = json.loads((result.run_dir / "spec.json").read_text("utf-8"))
        self.assertIn("malware denylist", spec_now["assumptions"])


class BrownfieldScenarioTests(TmpCase):
    def run_brownfield(self, approver: ScriptedApprover, publish: bool = False):  # type: ignore[no-untyped-def]
        clients = {**scripted_clients(), "impact": ScriptedClient([], default=js(BROWNFIELD_IMPACT))}
        return run_scenario(load_scenario("brownfield"), self.root / "run", clients, approver,
                            verifier=passing_verifier(), publish_repo=publish, out=io.StringIO())

    def test_baseline_is_the_real_greenfield_output(self) -> None:
        scenario = load_scenario("brownfield")
        assert scenario.baseline is not None
        self.assertIn("app/main.py", scenario.baseline)
        self.assertIn("tests/test_api.py", scenario.baseline)

    def test_brownfield_scans_existing_code_and_gates_the_impact_analysis(self) -> None:
        approver = ScriptedApprover()
        result = self.run_brownfield(approver)
        self.assertEqual(result.summary.state, RunState.SUCCEEDED, result.summary.reason)
        self.assertIn("impact", {r.node_id for r in approver.requests})
        codebase = json.loads((result.run_dir / "codebase.json").read_text("utf-8"))
        self.assertIn("GET /analytics/{code}",
                      {f"{e['method']} {e['path']}" for e in codebase["endpoints"]})

    def test_developer_revises_the_existing_code(self) -> None:
        clients = {**scripted_clients(), "impact": ScriptedClient([], default=js(BROWNFIELD_IMPACT))}
        run_scenario(load_scenario("brownfield"), self.root / "run", clients, ScriptedApprover(),
                     verifier=passing_verifier(), publish_repo=False, out=io.StringIO())
        self.assertIn("def get_analytics", clients["developer"].calls[0].prompt)  # existing app/main.py


class CliTests(unittest.TestCase):
    def test_invalid_flag_combinations_fail_before_any_model_call(self) -> None:
        # found by accident: `--live --prune` started a full live run before rejecting --prune
        from demo import cli

        def no_model() -> None:
            raise AssertionError("a model client was created")

        for flags in (["--live", "--prune"], ["--record", "--prune"]):
            with self.subTest(flags=flags), mock.patch.object(cli, "_gemini", no_model), \
                    mock.patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(cli.main(["greenfield", *flags]), 2)
                self.assertIn("--prune", err.getvalue())


class ConsoleApproverTests(unittest.TestCase):
    def request(self) -> ApprovalRequest:
        return ApprovalRequest("run1", "architect", "3 components, 5 tasks", ("design.json",),
                               "node requires approval")

    def decide(self, *answers: str) -> tuple[object, str]:
        replies = iter(answers)
        out = io.StringIO()
        approver = ConsoleApprover("alice", input_fn=lambda _: next(replies), out=out)
        return approver.decide(self.request()), out.getvalue()

    def test_shows_context_and_approves(self) -> None:
        decision, shown = self.decide("a")
        self.assertEqual((decision.verdict, decision.approver), ("approve", "alice"))  # type: ignore[attr-defined]
        for text in ("architect", "3 components, 5 tasks", "design.json", "node requires approval"):
            self.assertIn(text, shown)

    def test_view_shows_the_staged_content_before_deciding(self) -> None:
        replies = iter(["v", "a"])
        out = io.StringIO()
        request = ApprovalRequest("run1", "architect", "summary", ("design.json",), "node requires approval",
                                  preview=(("design.json", '{"overview": "FastAPI over SQLite"}'),))
        decision = ConsoleApprover("alice", input_fn=lambda _: next(replies), out=out).decide(request)
        self.assertEqual(decision.verdict, "approve")
        self.assertIn("FastAPI over SQLite", out.getvalue())

    def test_changes_require_a_comment_and_bad_input_reprompts(self) -> None:
        decision, _ = self.decide("maybe", "c", "", "add rate limiting")
        self.assertEqual((decision.verdict, decision.comment),  # type: ignore[attr-defined]
                         ("changes", "add rate limiting"))


@pytest.mark.slow
class PublishedRunTests(TmpCase):
    def test_successful_run_is_committed_to_the_generated_repo(self) -> None:
        result = run_scenario(load_scenario("greenfield"), self.root / "run", scripted_clients(),
                              ScriptedApprover(), verifier=passing_verifier(), out=io.StringIO())
        self.assertIsNotNone(result.commit)
        assert result.repo is not None
        self.assertTrue((result.repo / "app" / "main.py").exists())
        self.assertTrue((result.repo / "README.md").exists())
        # the commit anchors the audit log externally, and the report checks the anchor
        ((commit, seq, digest),) = GitRepo(result.repo).audit_anchors()
        self.assertEqual(commit, result.commit)
        from warden.eventlog import EventLog
        with EventLog(result.log_path) as log:
            self.assertTrue(log.matches_anchor(seq, digest))
        self.assertIn("matches", (result.run_dir / "report.md").read_text("utf-8").split("## Audit integrity")[1])


@pytest.mark.slow
class PublishedBrownfieldTests(TmpCase):
    def test_repo_history_is_baseline_then_feature(self) -> None:
        clients = {**scripted_clients(), "impact": ScriptedClient([], default=js(BROWNFIELD_IMPACT))}
        result = run_scenario(load_scenario("brownfield"), self.root / "run", clients, ScriptedApprover(),
                              verifier=passing_verifier(), out=io.StringIO())
        assert result.repo is not None
        subjects = [s for _, s in GitRepo(result.repo).log()]
        self.assertEqual(len(subjects), 2)
        self.assertTrue(subjects[0].startswith("feat: "))
        self.assertIn("existing codebase", subjects[1])


@pytest.mark.slow
@unittest.skipUnless((RECORDINGS / "greenfield").is_dir(), "no greenfield recordings yet")
class RecordedGreenfieldTests(TmpCase):
    """The committed recordings replay offline to a verified, released URL shortener."""

    def test_replay_reaches_release_with_real_tests_passing(self) -> None:
        llm = RecordReplayClient(RECORDINGS / "greenfield", mode="replay")
        result = run_scenario(load_scenario("greenfield"), self.root / "run", llm, ScriptedApprover(),
                              out=io.StringIO())
        self.assertEqual(result.summary.state, RunState.SUCCEEDED, result.summary.reason)
        report = json.loads((result.run_dir / "verify_report.json").read_text("utf-8"))
        self.assertTrue(report["passed"])
        events = [json.loads(line) for line in result.log_path.read_text("utf-8").splitlines()]
        self.assertTrue(any(e["type"] == EventType.APPROVAL_DECIDED.value for e in events))
