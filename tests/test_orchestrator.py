import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path

from warden.agent import AgentContext, AgentResult, FunctionAgent
from warden.approval import ApprovalDecision, ScriptedApprover
from warden.artifacts import ArtifactStore
from warden.dag import GateResult, Graph, NodeSpec
from warden.eventlog import EventLog
from warden.models import EventType, NodeState, RunState
from warden.orchestrator import Config, Orchestrator
from warden.policy import AllowAllPolicy, PolicyDecision


class DenyPolicy(AllowAllPolicy):
    def __init__(self, pre: set[str] = frozenset(), post: set[str] = frozenset(),  # type: ignore[assignment]
                 escalate: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.pre, self.post, self.escalate = pre, post, escalate

    def check_pre(self, node, ctx):  # type: ignore[no-untyped-def]
        if node.id in self.pre:
            return PolicyDecision(False, rule="no-go", reason="blocked before run")
        return PolicyDecision()

    def check_post(self, node, result):  # type: ignore[no-untyped-def]
        if node.id in self.post:
            return PolicyDecision(False, rule="no-secrets", reason="secret found in output")
        return PolicyDecision(requires_approval=node.id in self.escalate, reason="high impact")


def sdlc_nodes(**overrides: NodeSpec) -> list[NodeSpec]:
    nodes = {
        "req": NodeSpec("req", "req", (), ("request",), ("spec",)),
        "design": NodeSpec("design", "design", ("req",), ("spec",), ("design",)),
        "impl": NodeSpec("impl", "impl", ("design",), ("design",), ("code",)),
        "tests": NodeSpec("tests", "tests", ("design",), ("design",), ("test_suite",)),
        "docs": NodeSpec("docs", "docs", ("design",), ("design",), ("readme",)),
        "release": NodeSpec("release", "release", ("impl", "tests", "docs"),
                            ("code", "test_suite", "readme"), ("release_report",)),
    }
    nodes.update(overrides)
    return list(nodes.values())


class OrchestratorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = ArtifactStore(self.root / "artifacts")
        self.log = EventLog(self.root / "events.jsonl")
        self.addCleanup(self.log.close)  # runs before the temp dir cleanup
        self.store.put("request", "make links safer", produced_by="human:alice")
        self.calls: Counter[str] = Counter()
        self.lock = threading.Lock()

    def agent(self, name: str, outputs: tuple[str, ...], echo: str | None = None) -> FunctionAgent:
        """Scripted agent. With ``echo`` the output embeds that input, so changes propagate."""
        def fn(ctx: AgentContext) -> AgentResult:
            with self.lock:
                self.calls[name] += 1
            suffix = f" <- {ctx.read_text(echo)}" if echo else ""
            return AgentResult({o: f"{o} content{suffix}" for o in outputs}, summary=f"{name} done")
        return FunctionAgent(name, fn)

    def default_agents(self) -> dict[str, FunctionAgent]:
        return {
            "req": self.agent("req", ("spec",), echo="request"),
            "design": self.agent("design", ("design",), echo="spec"),
            "impl": self.agent("impl", ("code",)),
            "tests": self.agent("tests", ("test_suite",)),
            "docs": self.agent("docs", ("readme",)),
            "release": self.agent("release", ("release_report",)),
        }

    def build(self, nodes: list[NodeSpec] | None = None, agents=None, *,  # type: ignore[no-untyped-def]
              persist: bool = False, **kw):
        # Persisting state rewrites a file on every transition (slow on Windows); only the
        # persistence/resume tests need it, everything else runs in memory.
        if persist:
            kw.setdefault("state_path", self.root / "state.json")
        return Orchestrator(Graph(nodes or sdlc_nodes()), agents or self.default_agents(),
                            self.store, self.log, "run1", **kw)

    def types(self, t: EventType) -> list:  # type: ignore[type-arg]
        return [e for e in self.log.read_all() if e.type == t.value]


class HappyPathTests(OrchestratorTestCase):
    def test_full_run_succeeds_and_audit_log_is_intact(self) -> None:
        summary = self.build().run()
        self.assertEqual(summary.state, RunState.SUCCEEDED)
        self.assertTrue(all(s == "SUCCEEDED" for s in summary.nodes.values()))
        self.assertTrue(self.log.verify().ok)
        self.assertEqual(self.types(EventType.RUN_STARTED)[0].data["nodes"][0], "req")

    def test_artifact_lineage_is_recorded(self) -> None:
        self.build().run()
        code = self.store.latest("code")
        assert code is not None
        self.assertEqual(list(code.inputs), ["design"])
        self.assertEqual(code.produced_by, "agent:impl")

    def test_parallel_branches_really_run_concurrently(self) -> None:
        barrier = threading.Barrier(3, timeout=5)  # impl, tests, docs must all be inside at once

        def rendezvous(name: str, out: str) -> FunctionAgent:
            def fn(ctx: AgentContext) -> AgentResult:
                barrier.wait()  # raises BrokenBarrierError if branches run sequentially
                return AgentResult({out: f"{out} content"})
            return FunctionAgent(name, fn)

        agents = self.default_agents()
        agents.update(impl=rendezvous("impl", "code"), tests=rendezvous("tests", "test_suite"),
                      docs=rendezvous("docs", "readme"))
        summary = self.build(agents=agents).run()
        self.assertEqual(summary.state, RunState.SUCCEEDED)

    def test_join_waits_for_all_branches(self) -> None:
        order: list[str] = []
        agents = self.default_agents()
        for name, out in [("impl", "code"), ("tests", "test_suite"), ("docs", "readme"), ("release", "release_report")]:
            def fn(ctx: AgentContext, name: str = name, out: str = out) -> AgentResult:
                order.append(name)
                return AgentResult({out: f"{out} content"})
            agents[name] = FunctionAgent(name, fn)
        self.build(agents=agents).run()
        self.assertEqual(order[-1], "release")
        self.assertEqual(set(order[:3]), {"impl", "tests", "docs"})

    def test_decision_lineage_reaches_upstream_stages(self) -> None:
        self.build().run()
        final = next(e for e in self.log.read_all() if e.node_id == "release" and e.data.get("to") == "SUCCEEDED")
        reached = {e.node_id for e in self.log.lineage(final.seq)}
        self.assertTrue({"req", "design", "impl", "tests", "docs"} <= reached)


class FailureHandlingTests(OrchestratorTestCase):
    def flaky(self, fail_times: int, out: str = "code") -> FunctionAgent:
        def fn(ctx: AgentContext) -> AgentResult:
            with self.lock:
                self.calls["impl"] += 1
            if ctx.attempt <= fail_times:
                raise RuntimeError("boom")
            return AgentResult({out: "ok"})
        return FunctionAgent("impl", fn)

    def test_retry_then_success(self) -> None:
        agents = self.default_agents()
        agents["impl"] = self.flaky(2)
        summary = self.build(agents=agents).run()
        self.assertEqual(summary.state, RunState.SUCCEEDED)
        self.assertEqual(self.calls["impl"], 3)
        self.assertEqual(len(self.types(EventType.RETRY_SCHEDULED)), 2)

    def test_retries_are_bounded_then_rolled_back_and_run_halts(self) -> None:
        agents = self.default_agents()
        agents["impl"] = self.flaky(99)
        summary = self.build(agents=agents).run()
        self.assertEqual(self.calls["impl"], 3)  # 1 attempt + max_retries(2)
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertEqual(summary.nodes["impl"], "ROLLED_BACK")
        self.assertEqual(summary.nodes["release"], "PENDING")  # never started
        self.assertIsNone(self.store.latest("code"))  # nothing was committed
        self.assertTrue(self.log.verify().ok)

    def test_fallback_agent_is_used_after_retries(self) -> None:
        agents = self.default_agents()
        agents["impl"] = self.flaky(99)
        agents["impl_fallback"] = self.agent("impl_fallback", ("code",))
        nodes = sdlc_nodes(impl=NodeSpec("impl", "impl", ("design",), ("design",), ("code",),
                                         fallback_agent="impl_fallback"))
        summary = self.build(nodes, agents).run()
        self.assertEqual(summary.state, RunState.SUCCEEDED)
        code = self.store.latest("code")
        assert code is not None
        self.assertEqual(code.produced_by, "agent:impl_fallback")

    def test_missing_declared_output_fails_exit_gate_and_retries(self) -> None:
        agents = self.default_agents()
        agents["impl"] = FunctionAgent("impl", lambda ctx: AgentResult({}))
        summary = self.build(agents=agents).run()
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertIn("missing declared outputs", self.types(EventType.RETRY_SCHEDULED)[0].data["reason"])

    def test_custom_exit_gate_can_reject_output(self) -> None:
        def gate(r: AgentResult) -> GateResult:
            return GateResult("TODO" not in str(r.outputs["code"]), "contains TODO")

        agents = self.default_agents()
        agents["impl"] = FunctionAgent("impl", lambda ctx: AgentResult({"code": "TODO later"}))
        nodes = sdlc_nodes(impl=NodeSpec("impl", "impl", ("design",), ("design",), ("code",),
                                         exit_gate=gate, max_retries=0))
        self.assertEqual(self.build(nodes, agents).run().state, RunState.FAILED)

    def test_undeclared_output_is_not_retried(self) -> None:
        agents = self.default_agents()
        agents["impl"] = FunctionAgent("impl", lambda ctx: AgentResult({"code": "x", "secrets.env": "y"}))
        self.build(agents=agents).run()
        self.assertEqual(len(self.types(EventType.RETRY_SCHEDULED)), 0)
        self.assertIsNone(self.store.latest("secrets.env"))

    def test_entry_gate_blocks_node_without_running_it(self) -> None:
        def gate(store: ArtifactStore) -> GateResult:
            return GateResult(False, "design not reviewed")

        nodes = sdlc_nodes(impl=NodeSpec("impl", "impl", ("design",), ("design",), ("code",), entry_gate=gate))
        summary = self.build(nodes).run()
        self.assertEqual(summary.state, RunState.BLOCKED)
        self.assertEqual(self.calls["impl"], 0)
        self.assertIn("design not reviewed", summary.blocked["impl"])


class PolicyAndApprovalTests(OrchestratorTestCase):
    def test_pre_policy_denial_never_runs_agent_and_is_not_retried(self) -> None:
        summary = self.build(policy=DenyPolicy(pre={"impl"})).run()
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertEqual(self.calls["impl"], 0)
        self.assertEqual(len(self.types(EventType.RETRY_SCHEDULED)), 0)
        self.assertIn("no-go", summary.reason)

    def test_post_policy_denial_blocks_commit(self) -> None:
        summary = self.build(policy=DenyPolicy(post={"impl"})).run()
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertIsNone(self.store.latest("code"))
        self.assertEqual(self.calls["impl"], 1)

    def test_approval_required_nodes_fail_closed_without_a_human(self) -> None:
        nodes = sdlc_nodes(design=NodeSpec("design", "design", ("req",), ("spec",), ("design",),
                                           requires_approval=True))
        summary = self.build(nodes).run()  # default approver rejects
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertIsNone(self.store.latest("design"))

    def test_approval_granted_commits_and_is_audited(self) -> None:
        nodes = sdlc_nodes(design=NodeSpec("design", "design", ("req",), ("spec",), ("design",),
                                           requires_approval=True))
        approver = ScriptedApprover({"design": [ApprovalDecision("approve", "alice", "lgtm")]})
        summary = self.build(nodes, approver=approver).run()
        self.assertEqual(summary.state, RunState.SUCCEEDED)
        decided = self.types(EventType.APPROVAL_DECIDED)[0]
        self.assertEqual(decided.actor, "human:alice")
        self.assertEqual(len(decided.caused_by), 1)  # linked to the request

    def test_changes_requested_feeds_comment_back_then_approves(self) -> None:
        seen: list[str | None] = []

        def design(ctx: AgentContext) -> AgentResult:
            seen.append(ctx.feedback)
            return AgentResult({"design": f"design rev{ctx.attempt}"})

        agents = self.default_agents()
        agents["design"] = FunctionAgent("design", design)
        nodes = sdlc_nodes(design=NodeSpec("design", "design", ("req",), ("spec",), ("design",),
                                           requires_approval=True))
        approver = ScriptedApprover({"design": [ApprovalDecision("changes", "alice", "add rate limits"),
                                                ApprovalDecision("approve", "alice")]})
        summary = self.build(nodes, agents, approver=approver).run()
        self.assertEqual(summary.state, RunState.SUCCEEDED)
        self.assertEqual(seen, [None, "add rate limits"])
        design = self.store.latest("design")
        assert design is not None
        self.assertEqual(self.store.read_text(design), "design rev2")

    def test_policy_can_escalate_an_auto_node_to_human_approval(self) -> None:
        approver = ScriptedApprover({"release": [ApprovalDecision("reject", "bob", "not ready")]})
        summary = self.build(policy=DenyPolicy(escalate={"release"}), approver=approver).run()
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertEqual(len(approver.requests), 1)
        self.assertIsNone(self.store.latest("release_report"))


    def test_agent_can_ask_for_human_input(self) -> None:
        agents = self.default_agents()
        agents["req"] = FunctionAgent("req", lambda ctx: AgentResult(
            {"spec": "draft"}, escalate="2 blocking questions"))
        approver = ScriptedApprover({"req": [ApprovalDecision("reject", "alice", "answer first")]})
        summary = self.build(agents=agents, approver=approver).run()
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertEqual(len(approver.requests), 1)
        self.assertIn("2 blocking questions", approver.requests[0].reason)
        self.assertIsNone(self.store.latest("spec"))


class RepairLoopTests(OrchestratorTestCase):
    """A failing check (e.g. verify) sends feedback upstream and re-runs from there."""

    def verify_nodes(self, max_replans: int = 2) -> list[NodeSpec]:
        verify = NodeSpec("verify", "verify", ("impl", "tests"), ("code", "test_suite"), ("verify_report",),
                          max_retries=0, on_failure_replan=("impl",), max_replans=max_replans)
        release = NodeSpec("release", "release", ("verify", "docs"), ("code", "test_suite", "readme"),
                           ("release_report",))
        return [n for n in sdlc_nodes() if n.id != "release"] + [verify, release]

    def agents_with(self, verify_passes_on: int) -> tuple[dict[str, FunctionAgent], list[str | None]]:
        seen: list[str | None] = []

        def impl(ctx: AgentContext) -> AgentResult:
            seen.append(ctx.feedback)
            with self.lock:
                self.calls["impl"] += 1
                version = self.calls["impl"]
            return AgentResult({"code": f"code v{version}"})

        def verify(ctx: AgentContext) -> AgentResult:
            with self.lock:
                self.calls["verify"] += 1
            if self.calls["impl"] < verify_passes_on:
                raise RuntimeError("2 tests failed: test_r1")
            return AgentResult({"verify_report": "passed"})

        agents = self.default_agents()
        agents.update(impl=FunctionAgent("impl", impl), verify=FunctionAgent("verify", verify))
        return agents, seen

    def test_failure_feeds_back_upstream_and_reruns_until_it_passes(self) -> None:
        agents, seen = self.agents_with(verify_passes_on=2)
        summary = self.build(self.verify_nodes(), agents).run()
        self.assertEqual(summary.state, RunState.SUCCEEDED, summary.reason)
        self.assertEqual(self.calls["impl"], 2)
        self.assertEqual(self.calls["tests"], 1)  # not downstream of impl: untouched
        self.assertIsNone(seen[0])
        self.assertIn("2 tests failed", seen[1] or "")
        replans = [e for e in self.types(EventType.REPLAN) if e.data.get("feedback_to")]
        self.assertEqual(len(replans), 1)
        self.assertEqual(replans[0].data["feedback_to"], ["impl"])

    def test_repair_loops_are_bounded(self) -> None:
        agents, _ = self.agents_with(verify_passes_on=99)
        summary = self.build(self.verify_nodes(max_replans=2), agents).run()
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertEqual(self.calls["impl"], 3)  # first try + 2 repair loops
        self.assertIn("2 tests failed", summary.reason)

    def test_policy_denial_does_not_trigger_repair(self) -> None:
        agents, _ = self.agents_with(verify_passes_on=1)
        summary = self.build(self.verify_nodes(), agents, policy=DenyPolicy(post={"verify"})).run()
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertEqual(self.calls["impl"], 1)


class ReplanRollbackAndSafetyTests(OrchestratorTestCase):
    def test_replan_reruns_only_affected_nodes(self) -> None:
        orch = self.build()
        orch.run()
        before = Counter(self.calls)
        self.store.put("request", "make links safer and add analytics", produced_by="human:alice")
        affected = orch.replan(["request"])
        self.assertEqual(affected, ["req"])  # only the direct consumer is invalidated up front
        summary = orch.run()
        self.assertEqual(summary.state, RunState.SUCCEEDED)
        after = self.calls
        self.assertEqual(after["req"], before["req"] + 1)
        self.assertEqual(after["design"], before["design"] + 1)  # spec changed -> design re-ran
        self.assertEqual(after["impl"], before["impl"] + 1)  # design changed -> impl re-ran
        # impl/tests/docs emit identical content, so release's inputs never changed
        self.assertEqual(after["release"], before["release"])
        self.assertEqual(len(self.types(EventType.REPLAN)) >= 2, True)

    def test_unchanged_regeneration_does_not_cascade(self) -> None:
        orch = self.build()
        orch.run()
        before = Counter(self.calls)
        self.store.put("request", "make links safer", produced_by="human:alice")  # identical text
        self.assertEqual(orch.replan(["request"]), ["req"])  # explicit notification still re-runs req
        orch.run()
        self.assertEqual(self.calls["design"], before["design"])  # spec unchanged -> design untouched

    def test_rollback_restores_previous_version_and_holds_node(self) -> None:
        orch = self.build()
        orch.run()
        self.store.put("request", "new direction", produced_by="human:alice")
        orch.replan(["request"])
        orch.run()
        design = self.store.latest("design")
        assert design is not None
        self.assertIn("new direction", self.store.read_text(design))
        reverted = orch.rollback("design", "feature rejected in review")
        self.assertEqual(reverted, ["design"])
        restored = self.store.latest("design")
        assert restored is not None
        self.assertIn("make links safer", self.store.read_text(restored))
        self.assertEqual(orch.states()["design"], "ROLLED_BACK")
        calls_before = self.calls["design"]
        summary = orch.run()  # held node must not auto-run
        self.assertEqual(summary.state, RunState.BLOCKED)
        self.assertEqual(self.calls["design"], calls_before)
        orch.release("design")
        self.assertEqual(orch.run().state, RunState.SUCCEEDED)
        self.assertTrue(self.log.verify().ok)

    def test_rollback_of_first_version_removes_artifact(self) -> None:
        orch = self.build()
        orch.run()
        orch.rollback("impl", "drop feature")
        self.assertIsNone(self.store.latest("code"))

    def test_rollback_requires_a_succeeded_node(self) -> None:
        with self.assertRaises(ValueError):
            self.build().rollback("impl", "nope")

    def test_step_budget_triggers_safe_stop(self) -> None:
        summary = self.build(config=Config(step_budget=2)).run()
        self.assertEqual(summary.state, RunState.SAFE_STOPPED)
        self.assertEqual(summary.steps, 2)
        self.assertEqual(len(self.types(EventType.SAFE_STOP)), 1)
        self.assertIsNone(self.store.latest("release_report"))

    def test_kill_switch_discards_in_flight_work(self) -> None:
        holder: dict[str, Orchestrator] = {}

        def impl(ctx: AgentContext) -> AgentResult:
            holder["o"].stop("operator pulled the plug")
            return AgentResult({"code": "should never be committed"})

        agents = self.default_agents()
        agents["impl"] = FunctionAgent("impl", impl)
        orch = holder["o"] = self.build(agents=agents)
        summary = orch.run()
        self.assertEqual(summary.state, RunState.SAFE_STOPPED)
        self.assertIsNone(self.store.latest("code"))
        self.assertEqual(summary.nodes["release"], "PENDING")
        self.assertEqual(summary.nodes["impl"], "ROLLED_BACK")

    def test_brownfield_adopt_starts_mid_graph(self) -> None:
        self.store.put("spec", "existing spec", produced_by="human:alice")
        self.store.put("design", "existing design", produced_by="human:alice")
        orch = self.build()
        orch.adopt("req")
        orch.adopt("design")
        summary = orch.run()
        self.assertEqual(summary.state, RunState.SUCCEEDED)
        self.assertEqual(self.calls["req"], 0)
        self.assertEqual(self.calls["design"], 0)
        self.assertEqual(self.calls["impl"], 1)

    def test_adopt_requires_existing_outputs(self) -> None:
        with self.assertRaises(ValueError):
            self.build().adopt("design")

    def test_state_persists_and_resumes(self) -> None:
        self.build(persist=True).run()
        before = Counter(self.calls)
        resumed = self.build(persist=True)  # same state_path
        self.assertTrue(all(s == NodeState.SUCCEEDED.value for s in resumed.states().values()))
        self.assertEqual(resumed.run().state, RunState.SUCCEEDED)
        self.assertEqual(self.calls, before)  # nothing re-ran

    def test_interrupted_run_recovers_on_next_start(self) -> None:
        orch = self.build(persist=True)
        orch._transition("req", NodeState.READY)
        orch._transition("req", NodeState.RUNNING)  # simulate a crash mid-run
        recovered = self.build(persist=True)
        self.assertEqual(recovered.states()["req"], "RUNNING")
        self.assertEqual(recovered.run().state, RunState.SUCCEEDED)

    def test_unknown_agent_is_rejected_up_front(self) -> None:
        agents = self.default_agents()
        del agents["impl"]
        with self.assertRaises(ValueError):
            self.build(agents=agents)


if __name__ == "__main__":
    unittest.main()
