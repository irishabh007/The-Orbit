"""Static codebase scanner for brownfield impact analysis (stdlib ``ast``; nothing executes).

Produces ``codebase.json``: modules with their classes, functions and imports, the
internal import graph and its reverse (who depends on a module: the blast radius
of changing it), HTTP endpoints found from route decorators, and which test files
cover which modules. The impact agent must ground its analysis in this map.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Mapping
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


# --------------------------------------------------------------------------- C#
# Regex-based (no C# parser in the standard library): good enough to ground impact analysis
# in real files, routes and dependencies; not a compiler. Patterns use bounded repeats.
_CS_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]{1,200})\s*[;{]", re.MULTILINE)
_CS_USING = re.compile(r"^\s*(?:global\s+)?using\s+(?!static\b)(?:\w{1,100}\s*=\s*)?([\w.]{1,200})\s*;", re.MULTILINE)
_CS_TYPE = re.compile(r"\b(?:class|record|interface|struct|enum)\s+(\w{1,100})")
_CS_METHOD = re.compile(r"\bpublic\s+(?:static\s+|async\s+|virtual\s+|override\s+){0,4}[\w<>\[\],.?]{1,100}\s+(\w{1,100})\s*\(")
_CS_MAP = re.compile(r"\.Map(Get|Post|Put|Patch|Delete)\(\s*\"([^\"\n]{0,300})\"")
_CS_ROUTE = re.compile(r"\[Route\(\s*\"([^\"\n]{0,300})\"\s*\)\]")
_CS_HTTP = re.compile(r"\[Http(Get|Post|Put|Patch|Delete)(?:\(\s*\"([^\"\n]{0,300})\"\s*\))?\]")


def _join_route(prefix: str, sub: str) -> str:
    parts = [p.strip("/") for p in (prefix, sub) if p and p.strip("/")]
    return "/" + "/".join(parts)


def _cs_endpoints(text: str, path: str) -> list[dict[str, str]]:
    found = [{"method": m.upper(), "path": _join_route("", route), "handler": "minimal-api", "module": path}
             for m, route in _CS_MAP.findall(text)]
    prefix_match = _CS_ROUTE.search(text)
    prefix = prefix_match.group(1) if prefix_match else ""
    for match in _CS_HTTP.finditer(text):
        handler = _CS_METHOD.search(text, match.end())
        found.append({"method": match.group(1).upper(), "path": _join_route(prefix, match.group(2) or ""),
                      "handler": handler.group(1) if handler else "?", "module": path})
    return found


def scan_csharp(files: Mapping[str, str]) -> dict[str, Any]:
    cs = {p: t for p, t in files.items() if p.endswith(".cs")}
    declares: dict[str, set[str]] = {}
    for path, text in cs.items():
        for ns in _CS_NAMESPACE.findall(text):
            declares.setdefault(ns, set()).add(path)
    modules: list[dict[str, Any]] = []
    endpoints: list[dict[str, str]] = []
    for path in sorted(cs):
        text = cs[path]
        usings = _CS_USING.findall(text)
        internal = sorted({f for ns in usings for f in declares.get(ns, set()) if f != path})
        modules.append({
            "path": path,
            "classes": sorted(set(_CS_TYPE.findall(text))),
            "functions": sorted(set(_CS_METHOD.findall(text))),
            "imports_internal": internal,
            "imports_external": sorted({ns for ns in usings if ns not in declares}),
            "lines": text.count("\n") + 1,
        })
        endpoints.extend(_cs_endpoints(text, path))
    return {
        "modules": modules,
        "endpoints": sorted(endpoints, key=lambda e: (e["module"], e["path"], e["method"])),
        "dependents": _dependents(modules),
        "tests": {m["path"]: [d for d in m["imports_internal"] if not _is_test(d)]
                  for m in modules if _is_test(m["path"])},
        "other_files": sorted(p for p in files if not p.endswith(".cs")),
        "parse_errors": [],
    }


class ScanAgent:
    """Deterministic tool agent: consumed bundles -> ``codebase.json``."""

    name = "scan"

    def __init__(self, scan: Callable[[Mapping[str, str]], dict[str, Any]] = scan_files) -> None:
        self.scan = scan

    def run(self, ctx: AgentContext) -> AgentResult:
        data = self.scan(collect_bundles(ctx.store, ctx.consumes))
        summary = (f"{len(data['modules'])} modules, {len(data['endpoints'])} endpoints, "
                   f"{len(data['tests'])} test files, {len(data['parse_errors'])} parse errors")
        return AgentResult({OUTPUT: json.dumps(data, indent=2, sort_keys=True)}, kind="analysis", summary=summary)
