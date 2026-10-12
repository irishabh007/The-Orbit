"""CLAUDE.md rule 7: standard library only in warden/ core (PyYAML for policy loading).

Third-party packages are allowed only in named adapter modules, so importing the
core never requires them.
"""

import ast
import sys
import unittest
from pathlib import Path

WARDEN = Path(__file__).resolve().parent.parent / "warden"

CORE_ALLOWED = {"warden", "yaml"}
ADAPTER_ALLOWED = {
    "llm/gemini.py": {"httpx"},
}


def top_level_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


class DependencyRuleTests(unittest.TestCase):
    def test_core_imports_only_stdlib_and_allowed_packages(self) -> None:
        for path in sorted(WARDEN.rglob("*.py")):
            rel = path.relative_to(WARDEN).as_posix()
            allowed = CORE_ALLOWED | ADAPTER_ALLOWED.get(rel, set())
            third_party = top_level_imports(path) - set(sys.stdlib_module_names) - allowed
            with self.subTest(module=rel):
                self.assertEqual(third_party, set())

    def test_adapter_allowlist_points_at_real_files(self) -> None:
        for rel in ADAPTER_ALLOWED:
            self.assertTrue((WARDEN / rel).is_file(), rel)
