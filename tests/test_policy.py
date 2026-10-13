import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from warden.agent import AgentContext, AgentResult, FunctionAgent
from warden.approval import ScriptedApprover
from warden.artifacts import ArtifactStore
from warden.bundle import pack
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


FAKE_SSN = "123-" + "45-6789"  # split so this file never matches a scanner
VALID_CARD = "4111 1111 " + "1111 1111"  # well-known test card number; passes the Luhn check
INVALID_CARD = "4111 1111 " + "1111 1112"  # same shape, fails Luhn


class ComplianceRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = YamlPolicy.from_dict(rules(compliance={"pii_patterns": [
            {"name": "us-ssn", "regex": r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"},
            {"name": "payment-card", "regex": r"\b[3-6]\d{3}([ -]?)\d{4}\1\d{4}\1\d{4}\b", "luhn": True},
        ]}))

    def post(self, text: str) -> Any:
        return self.policy.check_post(node(produces=("tests.bundle",)),
                                      AgentResult({"tests.bundle": pack({"tests/fixtures.py": f"X = '{text}'\n"})}))

    def test_personal_data_in_fixtures_is_denied_without_echoing_it(self) -> None:
        for value in (FAKE_SSN, VALID_CARD):
            with self.subTest(value=value):
                decision = self.post(value)
                self.assertFalse(decision.allow)
                self.assertIn("compliance", decision.rule)
                self.assertIn("tests.bundle:tests/fixtures.py line 1", decision.reason)
                self.assertNotIn(value, decision.reason)

    def test_luhn_check_avoids_false_positives(self) -> None:
        self.assertTrue(self.post(INVALID_CARD).allow)
        self.assertTrue(self.post("1700000000000").allow)  # epoch millis, not a card

    def test_luhn_flag_must_be_boolean(self) -> None:
        with self.assertRaisesRegex(PolicyError, "luhn"):
            YamlPolicy.from_dict(rules(compliance={"pii_patterns": [{"name": "x", "regex": "x", "luhn": "yes"}]}))


CSPROJ = """<Project Sdk="Microsoft.NET.Sdk.Web">
  <ItemGroup>
    <PackageReference Include="Microsoft.EntityFrameworkCore.Sqlite" Version="10.0.0" />
    {extra}
  </ItemGroup>
</Project>
"""


class NugetDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = YamlPolicy.from_dict(rules(dependencies={
            "manifests": ["*.csproj"], "allowed": [], "nuget_allowed": ["Microsoft.EntityFrameworkCore.Sqlite"]}))

    def check(self, content: str) -> Any:
        name = "src/App/App.csproj"
        return self.policy.check_post(node(produces=("code.bundle",)), AgentResult({"code.bundle": pack({name: content})}))

    def test_allowlisted_package_references_pass(self) -> None:
        self.assertTrue(self.check(CSPROJ.format(extra="")).allow)

    def test_unlisted_package_is_denied(self) -> None:
        decision = self.check(CSPROJ.format(extra='<PackageReference Include="Leftpad.Net" Version="1.0.0" />'))
        self.assertFalse(decision.allow)
        self.assertIn("Leftpad.Net", decision.reason)

    def test_unparseable_project_file_is_denied(self) -> None:
        self.assertFalse(self.check("<Project><oops").allow)


DESIGN = json.dumps({"components": [
    {"name": "api", "responsibility": "HTTP", "module": "app/main.py"},
    {"name": "store", "responsibility": "DB", "module": "app/storage/"}]})
IMPACT = json.dumps({"affected": [{"target": "app/analytics.py", "kind": "module", "change": "add", "reason": "new"}]})


class ScopeRuleTests(unittest.TestCase):
    """Nothing outside the approved plan lands without a human saying yes."""

    def setUp(self) -> None:
        self.policy = YamlPolicy.from_dict(rules(scope={
            "roles": ["developer"], "plans": ["design.json", "impact.json"],
            "always_allowed": ["requirements.txt", "*__init__.py", "*.csproj"]}))

    def post(self, files: dict[str, str], inputs: dict[str, str] | None = None, agent: str = "developer") -> Any:
        return self.policy.check_post(node(agent, ("code.bundle",)), AgentResult({"code.bundle": pack(files)}),
                                      inputs={"design.json": DESIGN} if inputs is None else inputs)

    def test_files_in_the_approved_design_pass(self) -> None:
        decision = self.post({"app/main.py": "x", "app/storage/db.py": "y", "requirements.txt": "fastapi\n",
                              "app/__init__.py": ""})
        self.assertEqual((decision.allow, decision.requires_approval), (True, False))

    def test_unplanned_file_is_stopped_for_approval_with_the_reason(self) -> None:
        decision = self.post({"app/main.py": "x", "app/payments.py": "charge()"})
        self.assertTrue(decision.allow)
        self.assertTrue(decision.requires_approval)
        self.assertIn("scope", decision.rule)
        self.assertIn("app/payments.py", decision.reason)
        self.assertIn("not in the approved plan", decision.reason)

    def test_brownfield_impact_targets_are_in_scope(self) -> None:
        decision = self.post({"app/analytics.py": "x"}, inputs={"design.json": DESIGN, "impact.json": IMPACT})
        self.assertFalse(decision.requires_approval)

    def test_other_roles_and_nodes_without_a_plan_are_not_scoped(self) -> None:
        self.assertFalse(self.post({"tests/test_x.py": "x"}, agent="tester").requires_approval)
        self.assertFalse(self.post({"anything.py": "x"}, inputs={}).requires_approval)

    def test_deny_mode_rejects_instead(self) -> None:
        strict = YamlPolicy.from_dict(rules(scope={"roles": ["developer"], "plans": ["design.json"], "mode": "deny"}))
        decision = strict.check_post(node("developer", ("code.bundle",)),
                                     AgentResult({"code.bundle": pack({"app/payments.py": "x"})}),
                                     inputs={"design.json": DESIGN})
        self.assertFalse(decision.allow)

    def test_orchestrator_supplies_the_approved_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp) / "a")
            store.put("design.json", DESIGN)
            graph = Graph([NodeSpec("dev", "developer", (), ("design.json",), ("code.bundle",))])
            agent = FunctionAgent("developer", lambda c: AgentResult(
                {"code.bundle": pack({"app/main.py": "x", "app/payments.py": "y"})}))
            approver = ScriptedApprover()
            with EventLog(Path(tmp) / "e.jsonl") as log:
                Orchestrator(graph, {"developer": agent}, store, log, "r", policy=self.policy,
                             approver=approver).run()
        self.assertEqual(len(approver.requests), 1)
        self.assertIn("app/payments.py", approver.requests[0].reason)


class BundleExpansionTests(unittest.TestCase):
    """Bundles (many files in one artifact) are checked file by file."""

    def setUp(self) -> None:
        self.policy = YamlPolicy.from_dict(rules(
            secrets={"patterns": [{"name": "aws-access-key-id", "regex": "AKIA[0-9A-Z]{16}"}]},
            dependencies={"manifests": ["requirements*.txt"], "allowed": ["fastapi"]},
            protected_paths=["migrations/*"]))

    def post(self, files: dict[str, str]) -> Any:
        return self.policy.check_post(node(produces=("code.bundle",)), AgentResult({"code.bundle": pack(files)}))

    def test_secret_inside_bundle_names_the_inner_file(self) -> None:
        decision = self.post({"app/a.py": "ok\n", "app/cfg.py": f"x = 1\nKEY = '{FAKE_AWS_KEY}'\n"})
        self.assertFalse(decision.allow)
        self.assertIn("code.bundle:app/cfg.py line 2", decision.reason)
        self.assertNotIn(FAKE_AWS_KEY, decision.reason)

    def test_manifest_inside_bundle_is_checked(self) -> None:
        self.assertFalse(self.post({"requirements.txt": "fastapi\nleftpad\n"}).allow)
        self.assertTrue(self.post({"requirements.txt": "fastapi\n"}).allow)

    def test_protected_path_inside_bundle_escalates(self) -> None:
        decision = self.post({"migrations/001.sql": "create table x();"})
        self.assertTrue(decision.requires_approval)
        self.assertIn("migrations/001.sql", decision.reason)

    def test_malformed_bundle_is_denied(self) -> None:
        decision = self.policy.check_post(node(produces=("code.bundle",)), AgentResult({"code.bundle": "{oops"}))
        self.assertFalse(decision.allow)
        self.assertIn("bundle", decision.rule)


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

    def test_catches_personal_data_in_test_fixtures(self) -> None:
        self.assertFalse(self.post("tester", {"tests/data.py": f"SSN = '{FAKE_SSN}'\n"}).allow)
        self.assertFalse(self.post("tester", {"tests/data.py": f"CARD = '{VALID_CARD}'\n"}).allow)

    def test_default_policy_scopes_the_developer_to_the_approved_design(self) -> None:
        design = json.dumps({"components": [{"name": "api", "responsibility": "x", "module": "app/main.py"}]})
        decision = self.policy.check_post(
            node("developer", ("code.bundle",)),
            AgentResult({"code.bundle": pack({"app/main.py": "x", "app/backdoor.py": "y"})}),
            inputs={"design.json": design})
        self.assertTrue(decision.requires_approval)
        self.assertIn("app/backdoor.py", decision.reason)

    def test_dotnet_project_and_settings_files_need_approval(self) -> None:
        for path in ("src/App/App.csproj", "src/App/appsettings.json", "src/App/Migrations/0001_Init.cs"):
            with self.subTest(path=path):
                self.assertTrue(self.post("developer", {path: "<Project />"}).requires_approval)

    def test_default_nuget_allowlist_covers_the_dotnet_stack(self) -> None:
        packages = "".join(f'<PackageReference Include="{p}" Version="1.0.0" />' for p in (
            "Microsoft.EntityFrameworkCore.Sqlite", "Microsoft.AspNetCore.Mvc.Testing", "xunit",
            "xunit.runner.visualstudio", "Microsoft.NET.Test.Sdk", "coverlet.collector"))
        decision = self.post("developer", {"tests/Tests.csproj": f"<Project><ItemGroup>{packages}</ItemGroup></Project>"})
        self.assertTrue(decision.allow, decision.reason)

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


APPROVALS = {
    "roles": {"tech-lead": ["alice"], "release-manager": ["bob", "alice"], "product-owner": ["carol"]},
    "gates": {"architect": ["tech-lead"], "release": ["release-manager"]},
    "separate": [["architect", "release"]],
}


class ApprovalRuleTests(unittest.TestCase):
    """Review finding: any string could approve any gate, and one person could approve them all."""

    def setUp(self) -> None:
        self.policy = YamlPolicy.from_dict(rules(approvals=APPROVALS))

    def check(self, gate: str, who: str, decided: dict[str, str] | None = None) -> Any:
        return self.policy.check_approval(NodeSpec(gate, gate), who, decided or {})

    def test_only_holders_of_the_gate_role_may_approve(self) -> None:
        self.assertTrue(self.check("architect", "alice").allow)
        denied = self.check("architect", "carol")
        self.assertFalse(denied.allow)
        self.assertEqual(denied.rule, "approvals")
        self.assertIn("tech-lead", denied.reason)

    def test_unlisted_gates_accept_any_known_approver_but_not_strangers(self) -> None:
        self.assertTrue(self.check("requirements", "carol").allow)
        self.assertFalse(self.check("requirements", "mallory").allow)  # holds no role at all

    def test_separation_of_duties(self) -> None:
        # alice may approve releases, but not a release whose design she approved herself
        self.assertFalse(self.check("release", "alice", {"architect": "alice"}).allow)
        self.assertTrue(self.check("release", "bob", {"architect": "alice"}).allow)

    def test_no_approvals_section_means_no_restriction(self) -> None:
        self.assertTrue(YamlPolicy.from_dict(rules()).check_approval(NodeSpec("x", "x"), "anyone", {}).allow)

    def test_malformed_rules_fail_closed(self) -> None:
        for bad in ({"gates": {"release": ["no-such-role"]}, "roles": {}},
                    {"roles": {"lead": "alice"}},
                    {"separate": [["architect"]]},
                    {"who": {}}):
            with self.subTest(bad=bad), self.assertRaises(PolicyError):
                YamlPolicy.from_dict(rules(approvals=bad))

    def test_governed_policy_extends_the_default_and_enforces_approvals(self) -> None:
        governed = YamlPolicy.from_file(DEFAULT_POLICY.parent / "governed.yaml")
        default = YamlPolicy.from_file(DEFAULT_POLICY)
        self.assertEqual(governed.protected_paths, default.protected_paths)  # inherited
        self.assertFalse(governed.check_approval(NodeSpec("release", "release"), "auto-approve", {}).allow)


class ApprovalEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.store = ArtifactStore(root / "artifacts")
        self.log = EventLog(root / "events.jsonl")
        self.addCleanup(self.log.close)

    def run_graph(self, decisions: dict[str, str]) -> Any:
        from warden.approval import ApprovalDecision
        graph = Graph([NodeSpec("architect", "architect", produces=("design",), requires_approval=True),
                       NodeSpec("release", "release", ("architect",), ("design",), ("ok",), requires_approval=True)])
        agents = {n: FunctionAgent(n, lambda ctx, n=n: AgentResult({"design" if n == "architect" else "ok": n}))
                  for n in ("architect", "release")}
        approver = ScriptedApprover({gate: [ApprovalDecision("approve", who)] for gate, who in decisions.items()})
        return Orchestrator(graph, agents, self.store, self.log, "run1",
                            policy=YamlPolicy.from_dict(rules(approvals=APPROVALS)), approver=approver).run()

    def test_authorized_and_separate_approvers_succeed(self) -> None:
        self.assertEqual(self.run_graph({"architect": "alice", "release": "bob"}).state, RunState.SUCCEEDED)

    def test_unauthorized_approval_fails_closed_and_commits_nothing(self) -> None:
        summary = self.run_graph({"architect": "carol", "release": "bob"})
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertIn("tech-lead", summary.reason)
        self.assertIsNone(self.store.latest("design"))
        self.assertEqual([e for e in self.log.read_all() if e.type == EventType.RETRY_SCHEDULED.value], [])

    def test_one_person_cannot_approve_both_design_and_release(self) -> None:
        summary = self.run_graph({"architect": "alice", "release": "alice"})
        self.assertEqual(summary.state, RunState.FAILED)
        self.assertIsNone(self.store.latest("ok"))
        self.assertIn("separation of duties", summary.reason)
