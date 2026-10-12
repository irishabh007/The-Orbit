"""Static codebase scanner for brownfield impact analysis (stdlib ``ast``; nothing executes).

Produces ``codebase.json``: modules with their classes, functions and imports, the
internal import graph and its reverse (who depends on a module: the blast radius
of changing it), HTTP endpoints found from route decorators, and which test files
cover which modules. The impact agent must ground its analysis in this map.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from typing import Any

from warden.agent import AgentContext, AgentResult
from warden.tools.workspace import collect_bundles

OUTPUT = "codebase.json"
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _module_name(path: str) -> str:
    name = path[:-3].replace("/", ".")
    return name.removesuffix(".__init__")


def _imports(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def _endpoints(tree: ast.Module, path: str) -> list[dict[str, str]]:
    found = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in _HTTP_METHODS and dec.args
                    and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str)):
                found.append({"method": dec.func.attr.upper(), "path": dec.args[0].value,
                              "handler": node.name, "module": path})
    return found


def scan_files(files: Mapping[str, str]) -> dict[str, Any]:
    py = {p: t for p, t in files.items() if p.endswith(".py")}
    by_name = {_module_name(p): p for p in py}
    modules: list[dict[str, Any]] = []
    endpoints: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    for path in sorted(py):
        try:
            tree = ast.parse(py[path], filename=path)
        except SyntaxError as exc:
            errors.append({"path": path, "line": exc.lineno, "error": exc.msg})
            continue
        internal, external = set(), set()
        for name in _imports(tree):
            target = next((by_name[c] for c in _prefixes(name) if c in by_name), None)
            if target is not None and target != path:
                internal.add(target)
            elif target is None:
                external.add(name.split(".")[0])
        modules.append({
            "path": path,
            "classes": sorted(n.name for n in tree.body if isinstance(n, ast.ClassDef)),
            "functions": sorted(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
            "imports_internal": sorted(internal),
            "imports_external": sorted(external),
            "lines": py[path].count("\n") + 1,
        })
        endpoints.extend(_endpoints(tree, path))
    return {
        "modules": modules,
        "endpoints": sorted(endpoints, key=lambda e: (e["module"], e["path"], e["method"])),
        "dependents": _dependents(modules),
        "tests": {m["path"]: [d for d in m["imports_internal"] if not _is_test(d)]
                  for m in modules if _is_test(m["path"])},
        "other_files": sorted(p for p in files if not p.endswith(".py")),
        "parse_errors": errors,
    }


def _prefixes(name: str) -> list[str]:
    """``app.service.x`` -> [``app.service.x``, ``app.service``, ``app``] (longest match first)."""
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]


def _is_test(path: str) -> bool:
    return path.startswith("tests/") or path.rsplit("/", 1)[-1].startswith("test_")


def _dependents(modules: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Reverse transitive import graph over application modules (tests excluded)."""
    direct: dict[str, set[str]] = {m["path"]: set() for m in modules}
    for m in modules:
        if _is_test(m["path"]):
            continue
        for dep in m["imports_internal"]:
            direct.setdefault(dep, set()).add(m["path"])
    result = {}
    for path in sorted(direct):
        if _is_test(path):
            continue
        seen: set[str] = set()
        stack = list(direct[path])
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(direct.get(cur, ()))
        result[path] = sorted(seen)
    return result


class ScanAgent:
    """Deterministic tool agent: consumed bundles -> ``codebase.json``."""

    name = "scan"

    def run(self, ctx: AgentContext) -> AgentResult:
        data = scan_files(collect_bundles(ctx.store, ctx.consumes))
        summary = (f"{len(data['modules'])} modules, {len(data['endpoints'])} endpoints, "
                   f"{len(data['tests'])} test files, {len(data['parse_errors'])} parse errors")
        return AgentResult({OUTPUT: json.dumps(data, indent=2, sort_keys=True)}, kind="analysis", summary=summary)
