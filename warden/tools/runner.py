"""Run external commands (pytest, ruff, git) with a timeout and a scrubbed environment.

Generated code is untrusted: it runs with an allowlisted environment, so API keys and
other secrets in the parent process are invisible to it. This is a guardrail, not a
sandbox: the code still runs with the user's file-system and network permissions.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# Only what interpreters and git need to start; everything else (keys, tokens) is dropped.
_KEEP = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
         "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "TZ"}


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    output: str  # stdout and stderr interleaved
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


Runner = Callable[[Sequence[str], Path, float], CommandResult]


def safe_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _KEEP}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.update(extra or {})
    return env


def run_command(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
    t0 = time.monotonic()
    args = tuple(argv)

    def elapsed() -> int:
        return int((time.monotonic() - t0) * 1000)

    try:
        proc = subprocess.run(args, cwd=cwd, env=safe_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout, check=False,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        partial = exc.output.decode("utf-8", "replace") if isinstance(exc.output, bytes) else (exc.output or "")
        return CommandResult(args, -1, partial + f"\n[timed out after {timeout}s]", elapsed(), timed_out=True)
    except OSError as exc:  # command not found, permission denied
        return CommandResult(args, 127, f"could not start {args[0]!r}: {exc}", elapsed())
    return CommandResult(args, proc.returncode, proc.stdout or "", elapsed())
