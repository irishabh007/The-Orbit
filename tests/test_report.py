import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from warden.agent import AgentContext, AgentResult, FunctionAgent
from warden.approval import ApprovalDecision, ScriptedApprover
from warden.artifacts import ArtifactStore
from warden.dag import Graph, NodeSpec
from warden.eventlog import EventLog
from warden.orchestrator import Orchestrator
from warden.report import compute_metrics, render_report


class TickClock:
    """Each event is exactly one second after the previous one."""

    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> str:
        self.t += timedelta(seconds=1)
        return self.t.isoformat()


class ReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.store = ArtifactStore(root / "artifacts")
        self.store.put("request", "x")
        self.log = EventLog(root / "events.jsonl", clock=TickClock())
        self.addCleanup(self.log.close)
        self.calls = 0

        def flaky(ctx: AgentContext) -> AgentResult:  # fails twice, then succeeds
            self.calls += 1
            if self.calls <= 2:
                raise RuntimeError("boom")
            return AgentResult({"code": "ok"}, summary="code ready")

        self.graph = Graph([
            NodeSpec("plan", "plan", (), ("request",), ("plan",), requires_approval=True),
            NodeSpec("build", "build", ("plan",), ("plan",), ("code",)),
        ])
        agents = {"plan": FunctionAgent("plan", lambda c: AgentResult({"plan": "p"}, summary="plan ready")),
                  "build": FunctionAgent("build", flaky)}
        approver = ScriptedApprover({"plan": [ApprovalDecision("approve", "alice", "looks good")]})
        Orchestrator(self.graph, agents, self.store, self.log, "run1", approver=approver).run()
        self.events = self.log.read_all()
        self.metrics = compute_metrics(self.events)


class MetricsTests(ReportTestCase):
    def test_run_level_success_and_latency(self) -> None:
        (run,) = self.metrics["runs"]
        self.assertEqual((run["run_id"], run["state"]), ("run1", "SUCCEEDED"))
        self.assertEqual(self.metrics["run_success_rate"], 1.0)
        self.assertEqual(run["latency_s"], self.events[-1].seq - self.events[0].seq)  # 1s per event

    def test_attempts_retries_and_success_rate(self) -> None:
        m = self.metrics
        self.assertEqual(m["node_attempts"], 4)  # plan 1 + build 3
        self.assertEqual(m["failures"], 2)
        self.assertEqual(m["retries"], 2)
        self.assertEqual(m["attempt_success_rate"], 0.5)
        self.assertEqual(m["retry_rate"], 0.5)
        self.assertEqual(m["rollbacks"], 0)

    def test_mttr_is_first_failure_to_recovery(self) -> None:
        build = [e for e in self.events if e.node_id == "build"]
        first_fail = next(e for e in build if e.data.get("to") == "FAILED")
        recovered = next(e for e in build if e.data.get("to") == "SUCCEEDED")
        self.assertEqual(self.metrics["recoveries"], 1)
        self.assertEqual(self.metrics["mttr_s"], float(recovered.seq - first_fail.seq))

    def test_approvals_record_who_what_and_how_long(self) -> None:
        (approval,) = self.metrics["approvals"]
        self.assertEqual((approval["node"], approval["verdict"], approval["by"], approval["comment"]),
                         ("plan", "approve", "human:alice", "looks good"))
        self.assertGreater(approval["wait_s"], 0)

    def test_per_node_summary(self) -> None:
        self.assertEqual(self.metrics["nodes"]["build"]["attempts"], 3)
        self.assertEqual(self.metrics["nodes"]["build"]["failures"], 2)
        self.assertEqual(self.metrics["nodes"]["build"]["state"], "SUCCEEDED")

    def test_unrecovered_failure_is_counted_but_not_in_mttr(self) -> None:
        events = [e for e in self.events if not (e.node_id == "build" and e.data.get("to") == "SUCCEEDED")]
        m = compute_metrics(events)
        self.assertEqual((m["recoveries"], m["unrecovered"], m["mttr_s"]), (0, 1, None))


class RenderTests(ReportTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.md = render_report(self.events, self.metrics, graph=self.graph, title="Unit run",
                                audit=self.log.verify())

    def test_has_every_section(self) -> None:
        for heading in ("# Unit run", "## Summary", "## Reliability metrics", "## Workflow", "## Timeline",
                        "## Human approvals", "## Policy decisions", "## Decision lineage", "## Audit integrity"):
            self.assertIn(heading, self.md)

    def test_workflow_is_a_mermaid_graph_with_final_states(self) -> None:
        self.assertIn("```mermaid", self.md)
        self.assertIn("plan --> build", self.md)
        self.assertIn("SUCCEEDED", self.md)

    def test_lineage_of_the_final_decision_reaches_the_human_approval(self) -> None:
        lineage = self.md.split("## Decision lineage", 1)[1].split("\n## ", 1)[0]
        self.assertIn("human:alice", lineage)

    def test_audit_section_reports_the_verified_chain(self) -> None:
        self.assertIn("verified", self.md.split("## Audit integrity", 1)[1])
