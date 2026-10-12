"""CLAUDE.md rule 7: standard library only in warden/ core (PyYAML for policy loading).

Third-party packages are allowed only in named adapter modules, so importing the
core never requires them.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

WARDEN = Path(__file__).resolve().parent.parent / "warden"

CORE_ALLOWED = {"warden", "yaml"}
ADAPTER_ALLOWED = {
    "llm/gemini.py": {"httpx"},
    "agents/": {"pydantic"},  # a trailing "/" covers the whole package
}


def allowed_for(rel: str) -> set[str]:
    return set().union(*(pkgs for key, pkgs in ADAPTER_ALLOWED.items()
                         if rel == key or (key.endswith("/") and rel.startswith(key))))


def imported_modules(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def top_level_imports(path: Path) -> set[str]:
    return {m.split(".")[0] for m in imported_modules(path)}


class VerifyEnvironmentTests(unittest.TestCase):
    def test_every_allowlisted_package_is_installed_for_verify(self) -> None:
        # verify runs generated tests in Warden's interpreter: anything the policy lets a
        # generated project depend on must be in the demo requirements, or tests error out
        import yaml

        root = WARDEN.parent
        allowed = yaml.safe_load((root / "policies" / "default.yaml").read_text("utf-8"))["dependencies"]["allowed"]
        listed: set[str] = set()
        for name in ("requirements.txt", "requirements-dev.txt", "requirements-demo.txt"):
            for line in (root / name).read_text("utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith("-"):
                    listed.add(re.split(r"[<>=!~\[ ;]", line, maxsplit=1)[0].lower().replace("_", "-"))
        missing = sorted(p for p in allowed if p.lower().replace("_", "-") not in listed)
        self.assertEqual(missing, [])


class DependencyRuleTests(unittest.TestCase):
    def test_core_imports_only_stdlib_and_allowed_packages(self) -> None:
        for path in sorted(WARDEN.rglob("*.py")):
            rel = path.relative_to(WARDEN).as_posix()
            allowed = CORE_ALLOWED | allowed_for(rel)
            third_party = top_level_imports(path) - set(sys.stdlib_module_names) - allowed
            with self.subTest(module=rel):
                self.assertEqual(third_party, set())

    def test_adapter_allowlist_points_at_real_files(self) -> None:
        for rel in ADAPTER_ALLOWED:
            self.assertTrue((WARDEN / rel).is_dir() if rel.endswith("/") else (WARDEN / rel).is_file(), rel)

    def test_core_modules_do_not_import_adapters(self) -> None:
        # core must stay importable without httpx/pydantic installed
        for path in sorted(WARDEN.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertFalse(any(m.startswith(("warden.agents", "warden.llm.gemini"))
                                     for m in imported_modules(path)))
