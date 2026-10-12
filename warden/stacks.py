"""Stack profiles: everything that differs between technology stacks for generated projects.

The orchestrator, policy engine and graphs are stack-agnostic. A profile supplies the
stack-specific parts: prompt overrides per role, file-ownership rules, an optional
in-agent syntax check, the verify commands, and the codebase scanner.

* ``python-fastapi``     FastAPI + sqlite3 + pytest. Overrides no prompts, so prompts are
                         byte-identical to the original agents (recorded runs depend on it).
* ``dotnet-aspnetcore``  ASP.NET Core minimal APIs + EF Core/SQLite + xUnit. Verify runs
                         ``dotnet build`` then ``dotnet test`` (which must report tests run).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from warden.tools.scanner import scan_csharp, scan_files
from warden.tools.verify import DEFAULT_CHECKS, Check, dotnet_test_digest


class StackError(ValueError):
    """Unknown stack profile."""


Rules = Callable[[Sequence[str]], list[str]]


@dataclass(frozen=True)
class StackProfile:
    name: str
    description: str
    developer_rules: Rules  # problems with the developer's file list
    tester_rules: Rules  # problems with the tester's file list
    verify_checks: tuple[Check, ...]
    scan: Callable[[Mapping[str, str]], dict[str, Any]]
    syntax_check: Callable[[str, str], str | None] | None = None  # (path, content) -> problem
    prompts: dict[str, dict[str, str]] = field(default_factory=dict)  # role -> {"role"/"task": text}


# ------------------------------------------------------------------ python-fastapi
def _python_developer_rules(paths: Sequence[str]) -> list[str]:
    problems = [f"{p}: tests belong to the tester" for p in sorted(paths) if p.startswith("tests/")]
    if "requirements.txt" not in paths:
        problems.append("requirements.txt is missing")
    if not any(p.endswith(".py") for p in paths):
        problems.append("no Python source files")
    return problems


def _python_tester_rules(paths: Sequence[str]) -> list[str]:
    problems = [f"{p}: tests must live under tests/" for p in paths if not p.startswith("tests/")]
    if not any(p.rsplit("/", 1)[-1].startswith("test_") and p.endswith(".py") for p in paths):
        problems.append("no tests/test_*.py file")
    return problems


def _python_syntax(path: str, content: str) -> str | None:
    """``compile`` only parses: nothing generated is executed here."""
    if not path.endswith(".py"):
        return None
    try:
        compile(content, path, "exec", dont_inherit=True)
    except SyntaxError as exc:
        return f"{path}:{exc.lineno}: syntax error: {exc.msg}"
    return None


PYTHON = StackProfile(
    name="python-fastapi",
    description="Python 3.11, FastAPI, sqlite3, pytest",
    developer_rules=_python_developer_rules,
    tester_rules=_python_tester_rules,
    verify_checks=DEFAULT_CHECKS,
    scan=scan_files,
    syntax_check=_python_syntax,
)

# ------------------------------------------------------------------ dotnet-aspnetcore
DOTNET_CONSTRAINTS = (
    "C# on .NET 10, ASP.NET Core minimal APIs, Entity Framework Core with SQLite for storage, xUnit with "
    "Microsoft.AspNetCore.Mvc.Testing (WebApplicationFactory) for tests. Layout: the API project is "
    "src/App/App.csproj with Program.cs ending in `public partial class Program { }` so tests can host it; "
    "the test project is tests/Tests.csproj with a ProjectReference to ../src/App/App.csproj. Nullable "
    "reference types enabled. Configuration from appsettings.json and environment variables; never "
    "hard-code secrets. NuGet packages allowed: Microsoft.AspNetCore.OpenApi, Microsoft.EntityFrameworkCore, "
    "Microsoft.EntityFrameworkCore.Sqlite, Microsoft.EntityFrameworkCore.InMemory, "
    "Microsoft.EntityFrameworkCore.Design, Microsoft.Data.Sqlite (API); Microsoft.NET.Test.Sdk, xunit, "
    "xunit.runner.visualstudio, coverlet.collector, Microsoft.AspNetCore.Mvc.Testing (tests)."
)

_DOTNET_ARCHITECT_TASK = f"""
Design the system for `spec.json` and plan the work. Technology constraints: {DOTNET_CONSTRAINTS}
- components: one per C# file or project, with its file path (for example src/App/Services/LinkService.cs).
- api: every endpoint with method, path, status codes and the requirement ids it serves.
- data_model; decisions (D1, ...) with rationale and rejected alternatives; risks with mitigations.
- tasks (T1, ...): small, each owned by developer, tester or docs, citing requirement ids, sequenced with
  depends_on. Every 'must' requirement needs at least one task.
- Tests and docs are written in parallel with the code from this design (contract-first), so pin file
  paths, type and method names, request/response shapes and the API precisely.
"""

_DOTNET_DEVELOPER_TASK = f"""
Implement the design in `design.json` for the requirements in `spec.json`. Constraints: {DOTNET_CONSTRAINTS}
- Return every file of the API project in full (no placeholders, no TODOs), including src/App/App.csproj
  with its PackageReference items and exact versions.
- Follow the file paths, types and API in the design exactly: tests are written in parallel against it.
- Do not write tests or docs; other agents own those.
"""

_DOTNET_TESTER_TASK = f"""
Write tests for the requirements in `spec.json` against the API and types in `design.json`. The code is
being written in parallel, so rely only on that contract. Constraints: {DOTNET_CONSTRAINTS}
- Files under tests/ only, including tests/Tests.csproj (xUnit, Microsoft.NET.Test.Sdk,
  xunit.runner.visualstudio, Microsoft.AspNetCore.Mvc.Testing, ProjectReference ../src/App/App.csproj).
  Name tests after requirement ids (for example R1_Shorten_ReturnsCreatedWithCode).
- Use WebApplicationFactory<Program> and isolate state (for example a unique SQLite file per test class).
- No network access and no sleeps.
"""


def _dotnet_developer_rules(paths: Sequence[str]) -> list[str]:
    problems = [f"{p}: tests belong to the tester" for p in sorted(paths) if p.startswith("tests/")]
    if "src/App/App.csproj" not in paths:
        problems.append("src/App/App.csproj is missing (the API project file)")
    if not any(p.endswith(".cs") for p in paths):
        problems.append("no C# source files")
    return problems


def _dotnet_tester_rules(paths: Sequence[str]) -> list[str]:
    problems = [f"{p}: tests must live under tests/" for p in paths if not p.startswith("tests/")]
    if "tests/Tests.csproj" not in paths:
        problems.append("tests/Tests.csproj is missing (the test project file)")
    if not any(p.endswith(".cs") for p in paths):
        problems.append("no C# test files")
    return problems


DOTNET = StackProfile(
    name="dotnet-aspnetcore",
    description=".NET 10, ASP.NET Core minimal APIs, EF Core + SQLite, xUnit",
    developer_rules=_dotnet_developer_rules,
    tester_rules=_dotnet_tester_rules,
    verify_checks=(
        # building the test project builds the API project it references; errors only = signal
        Check("build", ("dotnet", "build", "tests", "--nologo", "-v", "q", "-clp:ErrorsOnly")),
        # `dotnet test` exits 0 when nothing ran, so require evidence that tests actually ran
        # TRX gives each failure's message and stack trace; the digest groups them for the agents
        Check("tests", ("dotnet", "test", "tests", "--no-build", "--nologo", "-v", "q",
                        "--logger", "trx;LogFileName=results.trx"),
              requires=r"Total:\s*[1-9]", digest=dotnet_test_digest),
    ),
    scan=scan_csharp,
    syntax_check=None,  # no C# parser in the standard library; `dotnet build` reports syntax errors
    prompts={
        "architect": {"task": _DOTNET_ARCHITECT_TASK},
        "developer": {
            "role": ("You are a senior .NET engineer. You write production-quality, readable C# a human can "
                     "maintain: small classes, clear names, input validation, nullable reference types, and "
                     "no secrets in code."),
            "task": _DOTNET_DEVELOPER_TASK,
        },
        "tester": {
            "role": ("You are a test engineer. You write fast, deterministic xUnit tests that verify behavior "
                     "through the public HTTP API, one test per acceptance criterion where practical."),
            "task": _DOTNET_TESTER_TASK,
        },
    },
)

STACKS = {profile.name: profile for profile in (PYTHON, DOTNET)}
DEFAULT_NEW_PROJECT_STACK = DOTNET.name


def get_stack(name: str) -> StackProfile:
    try:
        return STACKS[name]
    except KeyError:
        raise StackError(f"unknown stack {name!r}; available: {', '.join(sorted(STACKS))}") from None
