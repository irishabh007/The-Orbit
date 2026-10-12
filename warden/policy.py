"""Policy guardrails: the interface plus a YAML rule engine (``policies/default.yaml``).

Checked twice per attempt: before the agent runs (``check_pre``) and on its staged
outputs before anything is committed (``check_post``). A denial is never retried:
retrying a policy violation just burns budget.

``YamlPolicy`` rules:
* ``secrets``          post: deny if any output (or the summary, which is logged) matches
                       a secret pattern. Reasons name the pattern and line, never the match.
* ``dependencies``     post: deny dependency manifests that list a package outside the
                       allowlist, use pip options, direct URLs, or cannot be parsed.
* ``protected_paths``  post: escalate to human approval when an output path matches.
* ``autonomy``         per agent role: ``full`` (auto-commit), ``supervised`` (commit needs
                       human approval) or ``none`` (denied before running).

Loading fails closed: an unknown key, bad regex or unknown level raises ``PolicyError``
so a typo can never silently switch a rule off. Denials beat escalations, and all
findings are reported together.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml  # the one third-party dependency allowed in core (see CLAUDE.md rule 7)

from warden.agent import AgentContext, AgentResult
from warden.dag import NodeSpec


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool = True
    requires_approval: bool = False  # escalate to a human even if the node is "auto"
    rule: str = ""
    reason: str = ""


class Policy(Protocol):
    def check_pre(self, node: NodeSpec, ctx: AgentContext) -> PolicyDecision: ...

    def check_post(self, node: NodeSpec, result: AgentResult) -> PolicyDecision: ...


class AllowAllPolicy:
    def check_pre(self, node: NodeSpec, ctx: AgentContext) -> PolicyDecision:
        return PolicyDecision()

    def check_post(self, node: NodeSpec, result: AgentResult) -> PolicyDecision:
        return PolicyDecision()


# --------------------------------------------------------------------------- YAML engine
class PolicyError(ValueError):
    """The policy file is invalid."""


AUTONOMY_LEVELS = ("full", "supervised", "none")
_TOP_KEYS = {"version", "secrets", "dependencies", "protected_paths", "autonomy"}
_REQ_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_COMMENT = re.compile(r"(^|\s)#.*")


@dataclass(frozen=True)
class _Finding:
    rule: str
    reason: str
    deny: bool  # False = escalate to approval


def _normalize_path(name: str) -> str:
    path = name.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _normalize_pkg(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()  # PEP 503


def _as_text(content: str | bytes) -> str:
    return content if isinstance(content, str) else content.decode("utf-8", errors="replace")


def _section(raw: dict[str, Any], key: str, allowed: set[str]) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise PolicyError(f"{key}: expected a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise PolicyError(f"{key}: unknown key(s) {sorted(unknown)}")
    return value


def _str_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError(f"{where}: expected a list of strings")
    return value


class YamlPolicy:
    def __init__(
        self,
        secret_patterns: dict[str, re.Pattern[str]],
        manifests: list[str],
        allowed_packages: set[str],
        protected_paths: list[str],
        default_autonomy: str,
        autonomy_roles: dict[str, str],
    ) -> None:
        self.secret_patterns = secret_patterns
        self.manifests = manifests
        self.allowed_packages = allowed_packages
        self.protected_paths = protected_paths
        self.default_autonomy = default_autonomy
        self.autonomy_roles = autonomy_roles

    # ---- loading -------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path) -> YamlPolicy:
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PolicyError(f"{path}: invalid YAML: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Any) -> YamlPolicy:
        if not isinstance(raw, dict):
            raise PolicyError("policy must be a mapping")
        unknown = set(raw) - _TOP_KEYS
        if unknown:
            raise PolicyError(f"unknown key(s) {sorted(unknown)}")
        if raw.get("version") != 1:
            raise PolicyError(f"unsupported policy version: {raw.get('version')!r} (expected 1)")

        patterns: dict[str, re.Pattern[str]] = {}
        for entry in _section(raw, "secrets", {"patterns"}).get("patterns", []):
            if not isinstance(entry, dict) or set(entry) != {"name", "regex"}:
                raise PolicyError("secrets.patterns: each entry needs exactly 'name' and 'regex'")
            name, regex = str(entry["name"]), str(entry["regex"])
            try:
                patterns[name] = re.compile(regex)
            except re.error as exc:
                raise PolicyError(f"secrets.patterns: {name}: invalid regex: {exc}") from exc

        deps = _section(raw, "dependencies", {"manifests", "allowed"})
        manifests = _str_list(deps.get("manifests", []), "dependencies.manifests")
        allowed = {_normalize_pkg(p) for p in _str_list(deps.get("allowed", []), "dependencies.allowed")}
        protected = [_normalize_path(p) for p in _str_list(raw.get("protected_paths", []), "protected_paths")]

        autonomy = _section(raw, "autonomy", {"default", "roles"})
        default = autonomy.get("default", "supervised")  # fail closed if unspecified
        roles = autonomy.get("roles", {})
        if not isinstance(roles, dict):
            raise PolicyError("autonomy.roles: expected a mapping")
        for where, level in [("autonomy.default", default), *((f"autonomy.roles.{r}", lv) for r, lv in roles.items())]:
            if level not in AUTONOMY_LEVELS:
                raise PolicyError(f"{where}: unknown autonomy level {level!r}, expected one of {AUTONOMY_LEVELS}")

        return cls(patterns, manifests, allowed, protected, default, {str(k): v for k, v in roles.items()})

    # ---- checks --------------------------------------------------------
    def autonomy(self, role: str) -> str:
        return self.autonomy_roles.get(role, self.default_autonomy)

    def check_pre(self, node: NodeSpec, ctx: AgentContext) -> PolicyDecision:
        level = self.autonomy(node.agent)
        if level == "none":
            return PolicyDecision(False, rule="autonomy",
                                  reason=f"role {node.agent!r} has autonomy 'none': agents may not run it")
        return PolicyDecision()

    def check_post(self, node: NodeSpec, result: AgentResult) -> PolicyDecision:
        findings = [
            *self._secrets(result),
            *self._dependencies(result),
            *self._protected(result),
        ]
        level = self.autonomy(node.agent)
        if level != "full":
            findings.append(_Finding("autonomy", f"role {node.agent!r} has autonomy {level!r}", deny=False))
        return self._combine(findings)

    @staticmethod
    def _combine(findings: list[_Finding]) -> PolicyDecision:
        denials = [f for f in findings if f.deny]
        chosen = denials or findings
        if not chosen:
            return PolicyDecision()
        rules = ",".join(dict.fromkeys(f.rule for f in chosen))
        reason = "; ".join(f.reason for f in chosen)
        if denials:
            return PolicyDecision(False, rule=rules, reason=reason)
        return PolicyDecision(True, requires_approval=True, rule=rules, reason=reason)

    def _secrets(self, result: AgentResult) -> Iterable[_Finding]:
        sources = [(name, _as_text(content)) for name, content in result.outputs.items()]
        sources.append(("summary", result.summary))
        for source, text in sources:
            for pname, pattern in self.secret_patterns.items():
                match = pattern.search(text)
                if match:  # report location only: the matched text must never reach the log
                    line = text.count("\n", 0, match.start()) + 1
                    yield _Finding("secrets", f"possible secret ({pname}) in {source} line {line}", deny=True)

    def _dependencies(self, result: AgentResult) -> Iterable[_Finding]:
        for name, content in result.outputs.items():
            base = _normalize_path(name).rsplit("/", 1)[-1]
            if not any(fnmatch.fnmatchcase(base, m) for m in self.manifests):
                continue
            text = _as_text(content)
            if base == "pyproject.toml":
                problems = self._pyproject_problems(text)
            else:
                lines = (_COMMENT.sub("", raw).strip() for raw in text.splitlines())
                problems = [p for line in lines if line and (p := self._requirement_problem(line))]
            for problem in problems:
                yield _Finding("dependencies", f"{name}: {problem}", deny=True)

    def _pyproject_problems(self, text: str) -> list[str]:
        try:
            project = tomllib.loads(text).get("project", {})
        except tomllib.TOMLDecodeError as exc:
            return [f"unparseable TOML ({exc})"]
        specs: list[Any] = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            specs.extend(group)
        return [p for s in specs if (p := self._requirement_problem(str(s)))]

    def _requirement_problem(self, line: str) -> str | None:
        if line.startswith("-"):
            return f"pip option not allowed: {line.split()[0]}"
        spec = line.split(";", 1)[0]
        match = _REQ_NAME.match(spec)
        if match is None:
            return f"unparseable requirement: {line!r}"
        if "@" in spec or "://" in spec:
            return f"direct reference not allowed: {match.group()}"
        if _normalize_pkg(match.group()) not in self.allowed_packages:
            return f"package not in allowlist: {match.group()}"
        return None

    def _protected(self, result: AgentResult) -> Iterable[_Finding]:
        for name in result.outputs:
            path = _normalize_path(name)
            hit = next((p for p in self.protected_paths if fnmatch.fnmatchcase(path, p)), None)
            if hit is not None:
                yield _Finding("protected_paths", f"{path} matches protected path {hit!r}", deny=False)
