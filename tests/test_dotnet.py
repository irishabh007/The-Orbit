"""The .NET stack end to end: scripted answers through the real orchestrator and policy.

Fast tests fake the verify commands; the slow tests run the real `dotnet build` and
`dotnet test` (skipped when no .NET SDK is installed).
"""

import io
import json
import shutil
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

import pytest

from demo.fixtures import DOTNET_ANSWERS, DOTNET_CODE, DOTNET_PROGRAM, DOTNET_TESTS, js
from demo.runner import Scenario, run_scenario
from warden.agent import AgentContext
from warden.approval import ScriptedApprover
from warden.artifacts import ArtifactStore
from warden.bundle import pack
from warden.llm import ScriptedClient
from warden.models import RunState
from warden.stacks import DOTNET
from warden.tools import Check, CommandResult, VerifyAgent, verify_gate

HAS_DOTNET = shutil.which("dotnet") is not None


def files(bundle: dict) -> dict[str, str]:  # type: ignore[type-arg]
    return {f["path"]: f["content"] for f in bundle["files"]}


class DotnetPipelineTests(unittest.TestCase):
    def test_dotnet_scenario_runs_through_orchestrator_and_policy(self) -> None:
        def runner(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
            return CommandResult(tuple(argv), 0, "Passed!  - Failed: 0, Passed: 2, Skipped: 0, Total: 2", 1)

        clients = {role: ScriptedClient([], default=js(answer)) for role, answer in DOTNET_ANSWERS.items()}
        approver = ScriptedApprover()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_scenario(Scenario("dotnet-check", "Build a URL shortener API.", stack=DOTNET),
                                  Path(tmp) / "run", clients, approver,
                                  verifier=VerifyAgent(DOTNET.verify_checks, runner=runner),
                                  publish_repo=False, out=io.StringIO())
        self.assertEqual(result.summary.state, RunState.SUCCEEDED, result.summary.reason)
        self.assertIn("ASP.NET Core", clients["developer"].calls[0].prompt)
        # *.csproj is a protected path: both project files went to a human
        gated = {r.node_id for r in approver.requests}
        self.assertTrue({"developer", "tester"} <= gated)


@pytest.mark.slow
@unittest.skipUnless(HAS_DOTNET, "needs the .NET SDK")
class RealDotnetVerifyTests(unittest.TestCase):
    """Runs `dotnet build` + `dotnet test` for real (NuGet restore needs network the first time)."""

    def verify(self, code: dict[str, str], tests: dict[str, str]) -> dict:  # type: ignore[type-arg]
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            store.put("code.bundle", pack(code))
            store.put("tests.bundle", pack(tests))
            ctx = AgentContext("run1", "verify", 1, store, ("code.bundle", "tests.bundle"))
            result = VerifyAgent(DOTNET.verify_checks, timeout=600).run(ctx)
        return {"report": json.loads(result.outputs["verify_report.json"]), "gate": verify_gate(result)}

    def test_scripted_api_builds_and_its_tests_pass(self) -> None:
        out = self.verify(files(DOTNET_CODE), files(DOTNET_TESTS))
        self.assertTrue(out["gate"].ok, out["gate"].reason)

    def test_compile_error_is_reported_back_with_file_and_line(self) -> None:
        broken = {**files(DOTNET_CODE), "src/App/Program.cs": DOTNET_PROGRAM.replace("app.Run();", "app.Run(")}
        out = self.verify(broken, files(DOTNET_TESTS))
        self.assertFalse(out["gate"].ok)
        self.assertIn("build failed", out["gate"].reason)
        self.assertIn("Program.cs", out["gate"].reason)


class ZeroTestsGuardTests(unittest.TestCase):
    def test_dotnet_tests_check_requires_tests_to_have_run(self) -> None:
        tests_check: Check = DOTNET.verify_checks[-1]
        self.assertFalse(tests_check.passed(CommandResult(("dotnet", "test"), 0, "", 1)))
