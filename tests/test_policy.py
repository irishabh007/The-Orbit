import tempfile
import unittest
from pathlib import Path
from typing import Any

from warden.agent import AgentContext, AgentResult, FunctionAgent
from warden.approval import ScriptedApprover
from warden.artifacts import ArtifactStore
from warden.dag import Graph, NodeSpec
from warden.eventlog import EventLog
from warden.models import EventType, RunState
from warden.orchestrator import Orchestrator
from warden.policy import PolicyError, YamlPolicy

DEFAULT_POLICY = Path(__file__).resolve().parent.parent / "policies" / "default.yaml"

FAKE_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # split so this file never matches a scanner


def rules(**overrides: Any) -> dict[str, Any]:
    """A small, fully permissive rule set; each test switches on what it needs."""
    raw: dict[str, Any] = {
        "version": 1,
        "secrets": {"patterns": []},
        "dependencies": {"manifests": [], "allowed": []},
        "protected_paths": [],
        "autonomy": {"default": "full", "roles": {}},
    }
    raw.update(overrides)
    return raw


def node(agent: str = "developer", produces: tuple[str, ...] = ("out",)) -> NodeSpec:
    return NodeSpec("n1", agent, produces=produces)


def ctx(store: ArtifactStore) -> AgentContext:
    return AgentContext("run1", "n1", 1, store, ())


class LoadingTests(unittest.TestCase):
    def test_default_policy_file_loads(self) -> None:
        YamlPolicy.from_file(DEFAULT_POLICY)

    def test_unknown_top_level_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(PolicyError, "unknown key"):
            YamlPolicy.from_dict(rules(secrest={}))

    def test_wrong_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(PolicyError, "version"):
            YamlPolicy.from_dict(rules(version=2))

    def test_invalid_regex_is_rejected(self) -> None:
        with self.assertRaisesRegex(PolicyError, "bad"):
            YamlPolicy.from_dict(rules(secrets={"patterns": [{"name": "bad", "regex": "(["}]}))

    def test_unknown_autonomy_level_is_rejected(self) -> None:
        with self.assertRaisesRegex(PolicyError, "autonomy"):
            YamlPolicy.from_dict(rules(autonomy={"default": "full", "roles": {"dev": "yolo"}}))

    def test_non_mapping_document_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.yaml"
            path.write_text("- just\n- a list\n", encoding="utf-8")
            with self.assertRaises(PolicyError):
                YamlPolicy.from_file(path)


class SecretRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = YamlPolicy.from_dict(rules(secrets={"patterns": [
            {"name": "aws-access-key-id", "regex": "AKIA[0-9A-Z]{16}"}]}))

    def test_clean_output_is_allowed(self) -> None:
        decision = self.policy.check_post(node(), AgentResult({"out": "print('hi')"}))
        self.assertTrue(decision.allow)

    def test_secret_in_output_is_denied_without_echoing_it(self) -> None:
        content = f"x = 1\nKEY = '{FAKE_AWS_KEY}'\n"
        decision = self.policy.check_post(node(), AgentResult({"out": content}))
        self.assertFalse(decision.allow)
        self.assertIn("secrets", decision.rule)
        self.assertIn("aws-access-key-id", decision.reason)
        self.assertIn("out line 2", decision.reason)
        self.assertNotIn(FAKE_AWS_KEY, decision.reason)

    def test_secret_in_summary_is_denied(self) -> None:
        # the summary is written to the audit log, so it must be scanned too
        decision = self.policy.check_post(node(), AgentResult({"out": "ok"}, summary=FAKE_AWS_KEY))
        self.assertFalse(decision.allow)
        self.assertIn("summary", decision.reason)

    def test_bytes_output_is_scanned(self) -> None:
        decision = self.policy.check_post(node(), AgentResult({"out": FAKE_AWS_KEY.encode()}))
        self.assertFalse(decision.allow)


class DependencyRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = YamlPolicy.from_dict(rules(dependencies={
            "manifests": ["requirements*.txt", "pyproject.toml"],
            "allowed": ["fastapi", "pydantic-settings", "pytest"]}))

    def check(self, name: str, content: str) -> Any:
        return self.policy.check_post(node(produces=(name,)), AgentResult({name: content}))

    def test_allowlisted_requirements_pass_with_normalized_names(self) -> None:
        content = "# deps\nFastAPI[all]>=0.110 ; python_version >= '3.11'\npydantic_settings==2.*\n\n"
        self.assertTrue(self.check("requirements.txt", content).allow)

    def test_unlisted_requirement_is_denied(self) -> None:
        decision = self.check("requirements-dev.txt", "pytest\nleftpad==1.0\n")
        self.assertFalse(decision.allow)
        self.assertIn("dependencies", decision.rule)
        self.assertIn("leftpad", decision.reason)

    def test_pip_options_and_direct_urls_are_denied(self) -> None:
        for line in ("--index-url https://evil.example/simple", "-e git+https://x/y.git#egg=y",
                     "fastapi @ https://evil.example/fastapi.whl"):
            with self.subTest(line=line):
                self.assertFalse(self.check("requirements.txt", line + "\n").allow)

    def test_pyproject_dependencies_are_checked(self) -> None:
        ok = '[project]\nname = "app"\ndependencies = ["fastapi>=0.110"]\n' \
             '[project.optional-dependencies]\ndev = ["pytest"]\n'
        bad = '[project]\nname = "app"\ndependencies = ["requests"]\n'
        self.assertTrue(self.check("pyproject.toml", ok).allow)
        self.assertFalse(self.check("pyproject.toml", bad).allow)

    def test_unparseable_pyproject_is_denied(self) -> None:
        self.assertFalse(self.check("pyproject.toml", "[project\n").allow)

    def test_non_manifest_files_are_ignored(self) -> None:
        self.assertTrue(self.check("src/app.py", "import requests\n").allow)


class ProtectedPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = YamlPolicy.from_dict(rules(protected_paths=[".github/workflows/*", "Dockerfile"]))

    def test_protected_path_escalates_to_approval(self) -> None:
        decision = self.policy.check_post(
            node(produces=(".github/workflows/ci.yml",)), AgentResult({".github/workflows/ci.yml": "on: push"}))
        self.assertTrue(decision.allow)
        self.assertTrue(decision.requires_approval)
        self.assertIn("protected_paths", decision.rule)
        self.assertIn(".github/workflows/ci.yml", decision.reason)

    def test_windows_style_paths_are_normalized(self) -> None:
        name = ".\\.github\\workflows\\ci.yml"
        decision = self.policy.check_post(node(produces=(name,)), AgentResult({name: "on: push"}))
        self.assertTrue(decision.requires_approval)

    def test_unprotected_path_does_not_escalate(self) -> None:
        decision = self.policy.check_post(node(), AgentResult({"out": "x"}))
        self.assertEqual((decision.allow, decision.requires_approval), (True, False))

    def test_denial_beats_escalation_and_all_findings_are_reported(self) -> None:
        policy = YamlPolicy.from_dict(rules(
            protected_paths=["Dockerfile"],
            secrets={"patterns": [{"name": "aws-access-key-id", "regex": "AKIA[0-9A-Z]{16}"}]}))
        decision = policy.check_post(node(produces=("Dockerfile",)), AgentResult({"Dockerfile": FAKE_AWS_KEY}))
        self.assertFalse(decision.allow)
        self.assertIn("secrets", decision.rule)


class AutonomyRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ArtifactStore(Path(self._tmp.name))
        self.policy = YamlPolicy.from_dict(rules(autonomy={
            "default": "supervised", "roles": {"developer": "full", "deployer": "none"}}))

    def test_none_autonomy_is_denied_before_running(self) -> None:
        decision = self.policy.check_pre(node("deployer"), ctx(self.store))
        self.assertFalse(decision.allow)
        self.assertIn("autonomy", decision.rule)

    def test_full_autonomy_runs_and_commits_without_approval(self) -> None:
        self.assertTrue(self.policy.check_pre(node("developer"), ctx(self.store)).allow)
        self.assertFalse(self.policy.check_post(node("developer"), AgentResult({"out": "x"})).requires_approval)

    def test_unknown_role_falls_back_to_default(self) -> None:
        decision = self.policy.check_post(node("architect"), AgentResult({"out": "x"}))
        self.assertTrue(decision.requires_approval)
        self.assertIn("supervised", decision.reason)


class DefaultPolicyTests(unittest.TestCase):
    """The shipped policies/default.yaml catches each class of violation."""

    def setUp(self) -> None:
        self.policy = YamlPolicy.from_file(DEFAULT_POLICY)

    def post(self, agent: str, outputs: dict[str, str | bytes]) -> Any:
        return self.policy.check_post(node(agent, tuple(outputs)), AgentResult(outputs))

    def test_routine_developer_output_is_auto_committed(self) -> None:
        decision = self.post("developer", {"src/app.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
        self.assertEqual((decision.allow, decision.requires_approval), (True, False))

    def test_catches_private_key(self) -> None:
        pem = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIE...\n"
        self.assertFalse(self.post("developer", {"src/k.py": pem}).allow)

    def test_catches_hardcoded_password_but_not_env_lookup(self) -> None:
        self.assertFalse(self.post("developer", {"src/c.py": 'password = "' + 'hunter2hunter2"\n'}).allow)
        self.assertTrue(self.post("developer", {"src/c.py": 'password = os.environ["DB_PASSWORD"]\n'}).allow)

    def test_denies_unlisted_dependency(self) -> None:
        self.assertFalse(self.post("developer", {"requirements.txt": "fastapi\nleftpad\n"}).allow)

    def test_ci_workflow_needs_approval(self) -> None:
        self.assertTrue(self.post("developer", {".github/workflows/ci.yml": "on: push\n"}).requires_approval)

    def test_architect_and_release_are_supervised(self) -> None:
        for role in ("architect", "release"):
            with self.subTest(role=role):
                self.assertTrue(self.post(role, {"out.md": "plan"}).requires_approval)


class OrchestratorIntegrationTests(unittest.TestCase):
    """YamlPolicy plugged into the real orchestrator: denials never retry, never leak."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.store = ArtifactStore(root / "artifacts")
        self.log_path = root / "events.jsonl"
        self.log = EventLog(self.log_path)
        self.addCleanup(self.log.close)
        self.policy = YamlPolicy.from_dict(rules(
            secrets={"patterns": [{"name": "aws-access-key-id", "regex": "AKIA[0-9A-Z]{16}"}]},
            protected_paths=["Dockerfile"],
            autonomy={"default": "full", "roles": {"deployer": "none"}}))
        self.calls = 0

    def run_one(self, agent: str, outputs: dict[str, str | bytes],
                approver: ScriptedApprover | None = None) -> Any:
        def fn(c: AgentContext) -> AgentResult:
            self.calls += 1
            return AgentResult(outputs, summary="done")
        graph = Graph([NodeSpec("n1", agent, produces=tuple(outputs), max_retries=2)])
        orch = Orchestrator(graph, {agent: FunctionAgent(agent, fn)}, self.store, self.log, "run1",
                            policy=self.policy, approver=approver)
        return orch.run()

    def events(self, t: EventType) -> list[Any]:
        return [e for e in self.log.read_all() if e.type == t.value]

    def test_secret_fails_run_without_retry_commit_or_leak(self) -> None:
        summary = self.run_one("developer", {"config.py": f"KEY = '{FAKE_AWS_KEY}'"})
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.events(EventType.RETRY_SCHEDULED), [])
        self.assertIsNone(self.store.latest("config.py"))
        self.assertNotIn(FAKE_AWS_KEY, self.log_path.read_text("utf-8"))
        self.assertTrue(self.log.verify().ok)

    def test_protected_path_requests_human_approval(self) -> None:
        approver = ScriptedApprover()
        summary = self.run_one("developer", {"Dockerfile": "FROM python:3.11"}, approver)
        self.assertEqual(summary.state, RunState.SUCCEEDED)
        self.assertEqual(len(approver.requests), 1)
        self.assertIn("Dockerfile", approver.requests[0].reason)

    def test_no_autonomy_role_never_runs(self) -> None:
        summary = self.run_one("deployer", {"out": "x"})
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.events(EventType.RETRY_SCHEDULED), [])
