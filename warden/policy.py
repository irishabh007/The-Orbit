"""Policy guardrails: the interface plus a YAML rule engine (``policies/default.yaml``).

Checked twice per attempt: before the agent runs (``check_pre``) and on its staged
outputs before anything is committed (``check_post``). A denial is never retried:
retrying a policy violation just burns budget.

``YamlPolicy`` rules:
* ``secrets``          post: deny if any output (or the summary, which is logged) matches
                       a secret pattern. Reasons name the pattern and line, never the match.
* ``compliance``       post: deny personal data (e.g. SSNs, payment cards with a Luhn check)
                       in outputs; generated code and fixtures must not carry real PII.
* ``dependencies``     post: deny dependency manifests (requirements, pyproject, *.csproj) that
                       list a package outside its allowlist (pip / NuGet), use pip options,
                       direct URLs, or cannot be parsed.
* ``protected_paths``  post: escalate to human approval when an output path matches.
* ``scope``            post: files a role writes must be in the approved plan it consumed
                       (design components; brownfield impact targets) or on a scaffolding
                       list; anything else escalates to a human (or is denied in deny mode).
* ``autonomy``         per agent role: ``full`` (auto-commit), ``supervised`` (commit needs
                       human approval) or ``none`` (denied before running).
* ``approvals``        who may decide which gate: ``roles`` (role -> people), ``gates`` (node ->
                       roles that may approve it; unlisted gates need any role) and ``separate``
                       (gate pairs one person may not both approve: separation of duties).
                       Without this section any approver is accepted. Identities are taken as
                       given: authenticating them is the host's job (SSO, a code-review system).

A policy file may start with ``extends: <file>``: it inherits that file's sections and
replaces the ones it defines.

Outputs named ``*.bundle`` (see ``warden.bundle``) are expanded and every rule sees
each inner file (reported as ``code.bundle:app/x.py``); a malformed bundle is denied.

Loading fails closed: an unknown key, bad regex or unknown level raises ``PolicyError``
so a typo can never silently switch a rule off. Denials beat escalations, and all
findings are reported together.
"""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from xml.etree import ElementTree

import yaml  # the one third-party dependency allowed in core (see CLAUDE.md rule 7)

from warden.agent import AgentContext, AgentResult
from warden.bundle import BundleError, is_bundle, unpack
from warden.dag import NodeSpec


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool = True
    requires_approval: bool = False  # escalate to a human even if the node is "auto"
    rule: str = ""
    reason: str = ""


class Policy(Protocol):
    def check_pre(self, node: NodeSpec, ctx: AgentContext) -> PolicyDecision: ...

    # ``inputs``: committed content of the node's consumed artifacts (e.g. the approved design),
    # so a policy can judge outputs against what was approved upstream
    def check_post(self, node: NodeSpec, result: AgentResult,
                   inputs: Mapping[str, str] | None = None) -> PolicyDecision: ...

    # may ``approver`` approve this gate? ``decided``: gate -> who approved it earlier in the run
    def check_approval(self, node: NodeSpec, approver: str, decided: Mapping[str, str]) -> PolicyDecision: ...


class AllowAllPolicy:
    def check_pre(self, node: NodeSpec, ctx: AgentContext) -> PolicyDecision:
        return PolicyDecision()

    def check_post(self, node: NodeSpec, result: AgentResult,
                   inputs: Mapping[str, str] | None = None) -> PolicyDecision:
        return PolicyDecision()

    def check_approval(self, node: NodeSpec, approver: str, decided: Mapping[str, str]) -> PolicyDecision:
        return PolicyDecision()


# --------------------------------------------------------------------------- YAML engine
class PolicyError(ValueError):
    """The policy file is invalid."""


AUTONOMY_LEVELS = ("full", "supervised", "none")
_TOP_KEYS = {"version", "extends", "secrets", "compliance", "dependencies", "protected_paths", "autonomy",
             "scope", "approvals"}
_REQ_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_COMMENT = re.compile(r"(^|\s)#.*")


@dataclass(frozen=True)
class _File:
    label: str  # shown in reasons: "spec.json" or "code.bundle:app/main.py"
    path: str  # normalized path used for glob matching
    text: str


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


def _patterns(entries: Any, where: str, *, allow_luhn: bool = False) -> dict[str, tuple[re.Pattern[str], bool]]:
    """Parse a list of {name, regex[, luhn]} entries; fail closed on anything malformed."""
    if not isinstance(entries, list):
        raise PolicyError(f"{where}: expected a list")
    allowed_keys = {"name", "regex", "luhn"} if allow_luhn else {"name", "regex"}
    out: dict[str, tuple[re.Pattern[str], bool]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not {"name", "regex"} <= set(entry) <= allowed_keys:
            raise PolicyError(f"{where}: each entry needs 'name' and 'regex'"
                              + (" (optional 'luhn')" if allow_luhn else " only"))
        name, luhn = str(entry["name"]), entry.get("luhn", False)
        if not isinstance(luhn, bool):
            raise PolicyError(f"{where}: {name}: 'luhn' must be true or false")
        try:
            out[name] = (re.compile(str(entry["regex"])), luhn)
        except re.error as exc:
            raise PolicyError(f"{where}: {name}: invalid regex: {exc}") from exc
    return out


@dataclass(frozen=True)
class ScopeRule:
    roles: frozenset[str]
    plans: tuple[str, ...]  # approved input artifacts that define what may be written
    always_allowed: tuple[str, ...]  # scaffolding globs (manifests, project files, __init__.py)
    mode: str  # "escalate" (human decides) or "deny"


def _scope_rule(raw: dict[str, Any]) -> ScopeRule | None:
    if not raw:
        return None
    mode = raw.get("mode", "escalate")
    if mode not in ("escalate", "deny"):
        raise PolicyError(f"scope.mode: expected 'escalate' or 'deny', got {mode!r}")
    return ScopeRule(frozenset(_str_list(raw.get("roles", []), "scope.roles")),
                     tuple(_str_list(raw.get("plans", []), "scope.plans")),
                     tuple(_normalize_path(g) for g in _str_list(raw.get("always_allowed", []), "scope.always_allowed")),
                     mode)


def _planned_paths(plan_json: str) -> set[str]:
    """File paths an approved plan allows: design component modules, impact targets."""
    try:
        plan = json.loads(plan_json)
    except ValueError:
        return set()
    entries = [c.get("module", "") for c in plan.get("components", []) if isinstance(c, dict)]
    entries += [a.get("target", "") for a in plan.get("affected", []) if isinstance(a, dict)]
    return {_normalize_path(str(e).strip()) for e in entries if e and " " not in str(e).strip()}


def _in_plan(path: str, planned: set[str]) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) or ("*" in p and fnmatch.fnmatchcase(path, p))
               for p in planned)


def _luhn_ok(candidate: str) -> bool:
    """Payment-card checksum: cuts false positives from arbitrary long digit strings."""
    digits = [int(c) for c in candidate if c.isdigit()]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(reversed(digits)))
    return len(digits) >= 13 and total % 10 == 0


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


@dataclass(frozen=True)
class ApprovalRule:
    holders: dict[str, frozenset[str]]  # role -> people
    gates: dict[str, frozenset[str]]  # gate (node id) -> roles that may approve it
    separate: tuple[frozenset[str], ...]  # gate pairs that need two different people


def _approval_rule(raw: dict[str, Any]) -> ApprovalRule | None:
    if not raw:
        return None
    roles = raw.get("roles", {})
    gates = raw.get("gates", {})
    if not isinstance(roles, dict) or not isinstance(gates, dict):
        raise PolicyError("approvals.roles and approvals.gates: expected mappings")
    holders = {str(r): frozenset(_str_list(people, f"approvals.roles.{r}")) for r, people in roles.items()}
    allowed = {str(g): frozenset(_str_list(rs, f"approvals.gates.{g}")) for g, rs in gates.items()}
    for gate, rs in allowed.items():
        if unknown := sorted(rs - holders.keys()):
            raise PolicyError(f"approvals.gates.{gate}: unknown role(s) {unknown}")
    pairs = raw.get("separate", [])
    if not isinstance(pairs, list) or any(len(set(_str_list(pr, "approvals.separate"))) != 2 for pr in pairs):
        raise PolicyError("approvals.separate: expected a list of [gate, gate] pairs")
    return ApprovalRule(holders, allowed, tuple(frozenset(pr) for pr in pairs))


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
        pii_patterns: dict[str, tuple[re.Pattern[str], bool]] | None = None,  # name -> (regex, luhn)
        nuget_packages: set[str] | None = None,  # allowlist for *.csproj PackageReference (normalized)
        scope: ScopeRule | None = None,
        approvals: ApprovalRule | None = None,
    ) -> None:
        self.scope = scope
        self.approvals = approvals
        self.secret_patterns = secret_patterns
        self.pii_patterns = pii_patterns or {}
        self.nuget_packages = nuget_packages or set()
        self.manifests = manifests
        self.allowed_packages = allowed_packages
        self.protected_paths = protected_paths
        self.default_autonomy = default_autonomy
        self.autonomy_roles = autonomy_roles

    # ---- loading -------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path) -> YamlPolicy:
        return cls.from_dict(cls._read(Path(path)))

    @classmethod
    def _read(cls, path: Path, seen: tuple[Path, ...] = ()) -> Any:
        """The file's rules, on top of the file it ``extends`` (its sections replace the base's)."""
        if path.resolve() in seen:
            raise PolicyError(f"{path}: circular 'extends'")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PolicyError(f"{path}: cannot load: {exc}") from exc
        if not isinstance(raw, dict) or "extends" not in raw:
            return raw
        base = cls._read(path.parent / str(raw["extends"]), (*seen, path.resolve()))
        if not isinstance(base, dict):
            raise PolicyError(f"{path}: the policy it extends must be a mapping")
        return {**base, **{k: v for k, v in raw.items() if k != "extends"}}

    @classmethod
    def from_dict(cls, raw: Any) -> YamlPolicy:
        if not isinstance(raw, dict):
            raise PolicyError("policy must be a mapping")
        unknown = set(raw) - _TOP_KEYS
        if unknown:
            raise PolicyError(f"unknown key(s) {sorted(unknown)}")
        if raw.get("version") != 1:
            raise PolicyError(f"unsupported policy version: {raw.get('version')!r} (expected 1)")

        secrets = _patterns(_section(raw, "secrets", {"patterns"}).get("patterns", []), "secrets.patterns")
        patterns = {name: regex for name, (regex, _) in secrets.items()}
        pii = _patterns(_section(raw, "compliance", {"pii_patterns"}).get("pii_patterns", []),
                        "compliance.pii_patterns", allow_luhn=True)

        deps = _section(raw, "dependencies", {"manifests", "allowed", "nuget_allowed"})
        manifests = _str_list(deps.get("manifests", []), "dependencies.manifests")
        allowed = {_normalize_pkg(p) for p in _str_list(deps.get("allowed", []), "dependencies.allowed")}
        nuget = {_normalize_pkg(p) for p in _str_list(deps.get("nuget_allowed", []), "dependencies.nuget_allowed")}
        protected = [_normalize_path(p) for p in _str_list(raw.get("protected_paths", []), "protected_paths")]

        scope = _scope_rule(_section(raw, "scope", {"roles", "plans", "always_allowed", "mode"}))
        autonomy = _section(raw, "autonomy", {"default", "roles"})
        default = autonomy.get("default", "supervised")  # fail closed if unspecified
        roles = autonomy.get("roles", {})
        if not isinstance(roles, dict):
            raise PolicyError("autonomy.roles: expected a mapping")
        for where, level in [("autonomy.default", default), *((f"autonomy.roles.{r}", lv) for r, lv in roles.items())]:
            if level not in AUTONOMY_LEVELS:
                raise PolicyError(f"{where}: unknown autonomy level {level!r}, expected one of {AUTONOMY_LEVELS}")

        approvals = _approval_rule(_section(raw, "approvals", {"roles", "gates", "separate"}))
        return cls(patterns, manifests, allowed, protected, default, {str(k): v for k, v in roles.items()},
                   pii_patterns=pii, nuget_packages=nuget, scope=scope, approvals=approvals)

    # ---- checks --------------------------------------------------------
    def autonomy(self, role: str) -> str:
        return self.autonomy_roles.get(role, self.default_autonomy)

    def check_approval(self, node: NodeSpec, approver: str, decided: Mapping[str, str]) -> PolicyDecision:
        rule = self.approvals
        if rule is None:
            return PolicyDecision()
        held = {role for role, people in rule.holders.items() if approver in people}
        needed = rule.gates.get(node.id)
        if needed is not None and not held & needed:
            return PolicyDecision(False, rule="approvals",
                                  reason=f"{approver!r} may not approve {node.id!r}: needs role {sorted(needed)}")
        if needed is None and not held:
            return PolicyDecision(False, rule="approvals", reason=f"{approver!r} holds no approver role")
        for pair in rule.separate:
            if node.id in pair:
                (other,) = pair - {node.id}
                if decided.get(other) == approver:
                    return PolicyDecision(False, rule="approvals",
                                          reason=f"separation of duties: {approver!r} already approved "
                                                 f"{other!r}, so may not also approve {node.id!r}")
        return PolicyDecision()

    def check_pre(self, node: NodeSpec, ctx: AgentContext) -> PolicyDecision:
        level = self.autonomy(node.agent)
        if level == "none":
            return PolicyDecision(False, rule="autonomy",
                                  reason=f"role {node.agent!r} has autonomy 'none': agents may not run it")
        return PolicyDecision()

    def check_post(self, node: NodeSpec, result: AgentResult,
                   inputs: Mapping[str, str] | None = None) -> PolicyDecision:
        files, findings = self._expand(result)
        findings += [
            *self._secrets(files, result.summary),
            *self._compliance(files, result.summary),
            *self._dependencies(files),
            *self._protected(files),
            *self._out_of_scope(node, files, inputs or {}),
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

    @staticmethod
    def _expand(result: AgentResult) -> tuple[list[_File], list[_Finding]]:
        files: list[_File] = []
        findings: list[_Finding] = []
        for name, content in result.outputs.items():
            if not is_bundle(name):
                files.append(_File(name, _normalize_path(name), _as_text(content)))
                continue
            try:
                inner = unpack(content)
            except BundleError as exc:
                findings.append(_Finding("bundle", f"{name}: malformed bundle ({exc})", deny=True))
                continue
            files.extend(_File(f"{name}:{path}", path, text) for path, text in inner.items())
        return files, findings

    def _secrets(self, files: list[_File], summary: str) -> Iterable[_Finding]:
        sources = [(f.label, f.text) for f in files]
        sources.append(("summary", summary))
        for source, text in sources:
            for pname, pattern in self.secret_patterns.items():
                match = pattern.search(text)
                if match:  # report location only: the matched text must never reach the log
                    line = text.count("\n", 0, match.start()) + 1
                    yield _Finding("secrets", f"possible secret ({pname}) in {source} line {line}", deny=True)

    def _compliance(self, files: list[_File], summary: str) -> Iterable[_Finding]:
        sources = [(f.label, f.text) for f in files]
        sources.append(("summary", summary))
        for source, text in sources:
            for pname, (pattern, luhn) in self.pii_patterns.items():
                match = next((m for m in pattern.finditer(text) if not luhn or _luhn_ok(m.group())), None)
                if match:  # location only: personal data must never reach the log either
                    line = text.count("\n", 0, match.start()) + 1
                    yield _Finding("compliance", f"possible personal data ({pname}) in {source} line {line}",
                                   deny=True)

    def _dependencies(self, files: list[_File]) -> Iterable[_Finding]:
        for f in files:
            base = f.path.rsplit("/", 1)[-1]
            if not any(fnmatch.fnmatchcase(base, m) for m in self.manifests):
                continue
            if base == "pyproject.toml":
                problems = self._pyproject_problems(f.text)
            elif base.endswith(".csproj"):
                problems = self._csproj_problems(f.text)
            else:
                lines = (_COMMENT.sub("", raw).strip() for raw in f.text.splitlines())
                problems = [p for line in lines if line and (p := self._requirement_problem(line))]
            for problem in problems:
                yield _Finding("dependencies", f"{f.label}: {problem}", deny=True)

    def _csproj_problems(self, text: str) -> list[str]:
        """NuGet PackageReference entries in an MSBuild project must be on the NuGet allowlist."""
        try:
            root = ElementTree.fromstring(text)  # stdlib expat: no external entities are fetched
        except ElementTree.ParseError as exc:
            return [f"unparseable project file ({exc})"]
        problems = []
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] != "PackageReference":
                continue
            package = element.get("Include") or element.get("Update") or ""
            if _normalize_pkg(package) not in self.nuget_packages:
                problems.append(f"NuGet package not in allowlist: {package or '(unnamed)'}")
        return problems

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

    def _out_of_scope(self, node: NodeSpec, files: list[_File], inputs: Mapping[str, str]) -> Iterable[_Finding]:
        """Generated files must be in the approved plan (design components, brownfield impact)."""
        rule = self.scope
        if rule is None or node.agent not in rule.roles:
            return
        plans = [name for name in rule.plans if name in inputs]
        if not plans:
            return  # nothing approved to compare against (e.g. a node without a design input)
        planned = {p for name in plans for p in _planned_paths(inputs[name])}
        outside = sorted(f.path for f in files if ":" in f.label
                         and not _in_plan(f.path, planned) and not any(fnmatch.fnmatchcase(f.path, g)
                                                                         for g in rule.always_allowed))
        if outside:
            shown = ", ".join(outside[:8]) + (f" (+{len(outside) - 8} more)" if len(outside) > 8 else "")
            yield _Finding("scope", f"out of scope: {shown} not in the approved plan ({', '.join(plans)})",
                           deny=rule.mode == "deny")

    def _protected(self, files: list[_File]) -> Iterable[_Finding]:
        for f in files:
            hit = next((p for p in self.protected_paths if fnmatch.fnmatchcase(f.path, p)), None)
            if hit is not None:
                yield _Finding("protected_paths", f"{f.label} matches protected path {hit!r}", deny=False)
