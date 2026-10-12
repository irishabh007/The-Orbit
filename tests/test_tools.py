import json
import os
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

import pytest

from warden.agent import AgentContext, AgentResult
from warden.artifacts import ArtifactStore
from warden.bundle import pack
from warden.tools import (
    Check,
    CommandResult,
    GitRepo,
    VerifyAgent,
    WorkspaceError,
    collect_bundles,
    publish,
    run_command,
    safe_env,
    verify_gate,
    write_files,
)


class FakeRunner:
    """Stands in for subprocesses; records what would have run and where."""

    def __init__(self, *codes: int, output: str = "") -> None:
        self.codes = list(codes)
        self.output = output
        self.calls: list[tuple[tuple[str, ...], Path, list[str]]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        files = sorted(p.relative_to(cwd).as_posix() for p in cwd.rglob("*") if p.is_file())
        self.calls.append((tuple(argv), cwd, files))
        code = self.codes.pop(0) if self.codes else 0
        return CommandResult(tuple(argv), code, self.output, 5)


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class WorkspaceTests(TmpCase):
    def test_writes_files_with_lf_line_endings(self) -> None:
        written = write_files(self.root / "ws", {"app/main.py": "a = 1\nb = 2\n", "README.md": "# x\n"})
        self.assertEqual(sorted(written), ["README.md", "app/main.py"])
        self.assertEqual((self.root / "ws" / "app" / "main.py").read_bytes(), b"a = 1\nb = 2\n")

    def test_refuses_unsafe_and_git_paths(self) -> None:
        for bad in ("../escape.py", "/abs.py", ".git/hooks/pre-commit", "a\\b.py"):
            with self.subTest(path=bad), self.assertRaises(WorkspaceError):
                write_files(self.root, {bad: "x"})
        self.assertFalse((self.root.parent / "escape.py").exists())

    def test_collect_merges_bundles_and_rejects_overlaps(self) -> None:
        store = ArtifactStore(self.root / "store")
        store.put("code.bundle", pack({"app/main.py": "x"}))
        store.put("tests.bundle", pack({"tests/test_a.py": "y"}))
        store.put("dup.bundle", pack({"app/main.py": "z"}))
        self.assertEqual(set(collect_bundles(store, ["code.bundle", "tests.bundle"])),
                         {"app/main.py", "tests/test_a.py"})
        with self.assertRaisesRegex(WorkspaceError, "app/main.py"):
            collect_bundles(store, ["code.bundle", "dup.bundle"])
        with self.assertRaises(WorkspaceError):
            collect_bundles(store, ["missing.bundle"])


class RunnerTests(unittest.TestCase):
    def test_safe_env_drops_secrets_but_keeps_essentials(self) -> None:
        fake = {"PATH": "/bin", "GEMINI_API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "s", "SYSTEMROOT": "C:\\W"}
        with mock.patch.dict(os.environ, fake, clear=True):
            env = safe_env()
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")


class VerifyAgentTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = ArtifactStore(self.root / "store")
        self.store.put("code.bundle", pack({"app/main.py": "x = 1\n", "requirements.txt": "fastapi\n"}))
        self.store.put("tests.bundle", pack({"tests/test_main.py": "def test_x():\n    pass\n"}))
        self.ctx = AgentContext("run1", "verify", 1, self.store, ("code.bundle", "tests.bundle"))
        self.checks = (Check("tests", ("python", "-m", "pytest")), Check("lint", ("python", "-m", "ruff")))

    def report(self, result: AgentResult) -> dict:  # type: ignore[type-arg]
        return json.loads(result.outputs["verify_report.json"])

    def test_runs_every_check_in_a_scratch_copy_of_the_bundles(self) -> None:
        runner = FakeRunner(0, 0)
        result = VerifyAgent(self.checks, runner=runner).run(self.ctx)
        self.assertTrue(self.report(result)["passed"])
        self.assertEqual([c[0][-1] for c in runner.calls], ["pytest", "ruff"])
        self.assertIn("tests/test_main.py", runner.calls[0][2])
        self.assertFalse(runner.calls[0][1].exists())  # scratch dir removed afterwards
        self.assertTrue(verify_gate(result).ok)
        self.assertIn("2/2 checks passed", result.summary)

    def test_failure_is_reported_and_closes_the_gate_with_output(self) -> None:
        runner = FakeRunner(1, 0, output="FAILED tests/test_main.py::test_x - assert 1 == 2")
        result = VerifyAgent(self.checks, runner=runner).run(self.ctx)
        report = self.report(result)
        self.assertFalse(report["passed"])
        self.assertEqual([c["passed"] for c in report["checks"]], [False, True])
        gate = verify_gate(result)
        self.assertFalse(gate.ok)
        self.assertIn("tests failed (exit 1)", gate.reason)
        self.assertIn("assert 1 == 2", gate.reason)

    def test_long_output_is_truncated_to_the_tail(self) -> None:
        runner = FakeRunner(1, output="x" * 10_000 + "THE END")
        report = self.report(VerifyAgent(self.checks[:1], runner=runner).run(self.ctx))
        tail = report["checks"][0]["output_tail"]
        self.assertTrue(tail.endswith("THE END"))
        self.assertLess(len(tail), 5000)


@pytest.mark.slow
class RealToolsTests(TmpCase):
    """Real subprocesses: run with `pytest -m slow`."""

    def test_run_command_captures_output_and_times_out(self) -> None:
        ok = run_command([sys.executable, "-c", "print('hi')"], self.root, 30)
        self.assertEqual((ok.returncode, ok.output.strip(), ok.ok), (0, "hi", True))
        slow = run_command([sys.executable, "-c", "import time; time.sleep(5)"], self.root, 0.5)
        self.assertTrue(slow.timed_out)
        self.assertFalse(slow.ok)
        missing = run_command(["definitely-not-a-command-xyz"], self.root, 5)
        self.assertFalse(missing.ok)

    def test_child_process_does_not_see_secrets(self) -> None:
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "sekret-value"}):
            res = run_command([sys.executable, "-c", "import os; print(os.environ.get('GEMINI_API_KEY'))"],
                              self.root, 30)
        self.assertEqual(res.output.strip(), "None")

    def test_verify_runs_real_pytest_and_ruff(self) -> None:
        store = ArtifactStore(self.root / "store")
        store.put("code.bundle", pack({"calc.py": "def add(a: int, b: int) -> int:\n    return a + b\n"}))
        store.put("tests.bundle", pack({"tests/test_calc.py":
                                        "from calc import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n"}))
        ctx = AgentContext("run1", "verify", 1, store, ("code.bundle", "tests.bundle"))
        result = VerifyAgent().run(ctx)
        self.assertTrue(verify_gate(result).ok, verify_gate(result).reason)
        store.put("code.bundle", pack({"calc.py": "def add(a: int, b: int) -> int:\n    return a - b\n"}))
        failed = VerifyAgent().run(ctx)
        self.assertIn("tests failed", verify_gate(failed).reason)

    def test_publish_commits_per_feature_and_revert_rolls_back(self) -> None:
        store = ArtifactStore(self.root / "store")
        store.put("code.bundle", pack({"app/main.py": "v = 1\n"}))
        repo = GitRepo(self.root / "generated")
        first = publish(store, ["code.bundle"], repo, "feat: shorten links")
        self.assertIsNotNone(first.commit)
        self.assertEqual(first.files, ["app/main.py"])
        store.put("code.bundle", pack({"app/main.py": "v = 2\n"}))
        second = publish(store, ["code.bundle"], repo, "feat: add expiry")
        self.assertEqual([s for _, s in repo.log()], ["feat: add expiry", "feat: shorten links"])
        self.assertIsNone(publish(store, ["code.bundle"], repo, "no-op").commit)  # nothing changed
        assert second.commit is not None
        repo.revert(second.commit)
        self.assertEqual((repo.root / "app" / "main.py").read_text("utf-8"), "v = 1\n")
        self.assertFalse((repo.root / ".warden").exists())  # generated repo has no Warden files
