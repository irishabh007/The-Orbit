import io
import tempfile
import unittest
from pathlib import Path

from demo.failures import CASES, run_case
from warden.models import EventType, RunState


class FailureDemoTests(unittest.TestCase):
    """Each failure demo runs the real orchestrator, policy and gates with scripted model answers."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def run_named(self, name: str):  # type: ignore[no-untyped-def]
        return run_case(name, self.root / name, out=io.StringIO())

    def events(self, result, type_: EventType) -> list:  # type: ignore[no-untyped-def,type-arg]
        import json
        lines = result.log_path.read_text("utf-8").splitlines()
        return [e for e in map(json.loads, lines) if e["type"] == type_.value]

    def test_all_cases_are_listed(self) -> None:
        self.assertEqual(set(CASES), {"policy-block", "human-reject", "retry-fallback-recover",
                                      "retry-fallback-rollback", "safe-stop", "replan-on-change"})

    def test_upstream_change_replans_only_what_actually_changed(self) -> None:
        result = self.run_named("replan-on-change")
        self.assertEqual(result.summary.state, RunState.SUCCEEDED, result.summary.reason)
        replans = self.events(result, EventType.REPLAN)
        self.assertTrue(replans)
        # second pass: every node that reads a changed input re-ran (the spec feeds most of them);
        # code and tests came out byte-identical, so verify (running the test suite) was skipped
        reran = {e["node_id"] for e in self.events(result, EventType.NODE_TRANSITION)
                 if e["run_id"].endswith("-replan") and e["data"]["to"] == "RUNNING"}
        self.assertEqual(reran, {"requirements", "architect", "developer", "tester", "docs", "release"})
        self.assertNotIn("verify", reran)

    def test_policy_block_denies_without_retry_commit_or_leak(self) -> None:
        result = self.run_named("policy-block")
        self.assertEqual(result.summary.state, RunState.FAILED)
        self.assertIn("secrets", result.summary.reason)
        self.assertEqual(self.events(result, EventType.RETRY_SCHEDULED), [])
        self.assertIsNone(result.store.latest("code.bundle"))
        self.assertNotIn(result.planted_secret, result.log_path.read_text("utf-8"))
        report = (result.run_dir / "report.md").read_text("utf-8")
        self.assertIn("DENY", report.split("## Policy decisions", 1)[1])
        self.assertNotIn(result.planted_secret, report)

    def test_human_reject_fails_closed_and_nothing_downstream_runs(self) -> None:
        result = self.run_named("human-reject")
        self.assertEqual(result.summary.state, RunState.FAILED)
        self.assertIsNone(result.store.latest("design.json"))
        self.assertEqual(result.summary.nodes["developer"], "PENDING")

    def test_retry_then_fallback_recovers(self) -> None:
        result = self.run_named("retry-fallback-recover")
        self.assertEqual(result.summary.state, RunState.SUCCEEDED, result.summary.reason)
        fallback = [e for e in self.events(result, EventType.RETRY_SCHEDULED) if e["data"].get("fallback_agent")]
        self.assertEqual(len(fallback), 1)

    def test_retry_then_fallback_then_rollback_halts(self) -> None:
        result = self.run_named("retry-fallback-rollback")
        self.assertEqual(result.summary.state, RunState.FAILED)
        self.assertEqual(result.summary.nodes["developer"], "ROLLED_BACK")
        self.assertTrue(self.events(result, EventType.ROLLBACK))
        self.assertIsNone(result.store.latest("code.bundle"))

    def test_safe_stop_on_step_budget(self) -> None:
        result = self.run_named("safe-stop")
        self.assertEqual(result.summary.state, RunState.SAFE_STOPPED)
        self.assertTrue(self.events(result, EventType.SAFE_STOP))
        self.assertIsNone(result.store.latest("release_review.json"))
