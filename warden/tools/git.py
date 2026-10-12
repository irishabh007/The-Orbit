"""Git adapter for the generated repo: one commit per approved feature, revert to roll back.

The generated project is a normal git repository with no Warden files in it
(CLAUDE.md rule 5); Warden's own state lives in its artifact store and event log.
"""

from __future__ import annotations

from pathlib import Path

from warden.tools.runner import Runner, run_command

AUTHOR_NAME, AUTHOR_EMAIL = "Warden", "warden@localhost"
ANCHOR_TRAILER = "Warden-Audit-Head"  # "<seq> <sha256>" of the audit log head at commit time
_UNIT_SEP, _RECORD_SEP = "\x1f", "\x1e"


class GitError(RuntimeError):
    """A git command failed."""


class GitRepo:
    def __init__(self, root: str | Path, *, runner: Runner = run_command, timeout: float = 60.0) -> None:
        self.root = Path(root)
        self._runner = runner
        self._timeout = timeout

    def _git(self, *args: str) -> str:
        argv = ("git", "-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
                "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args)
        res = self._runner(argv, self.root, self._timeout)
        if not res.ok:
            raise GitError(f"git {' '.join(args)} failed ({res.returncode}): {res.output.strip()}")
        return res.output

    def init(self) -> None:
        if not (self.root / ".git").exists():
            self.root.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q", "-b", "main")

    def head(self) -> str | None:
        try:
            return self._git("rev-parse", "HEAD").strip()
        except GitError:
            return None  # no commits yet

    def commit_all(self, message: str) -> str | None:
        """Stage everything and commit. Returns the new sha, or None if nothing changed."""
        self._git("add", "-A")
        if not self._git("status", "--porcelain").strip():
            return None
        self._git("commit", "-q", "-m", message)
        return self.head()

    def revert(self, sha: str) -> str | None:
        """Undo one feature commit with a new commit (history is kept, as in the event log)."""
        self._git("revert", "--no-edit", sha)
        return self.head()

    def audit_anchors(self) -> list[tuple[str, int, str]]:
        """(commit, event seq, event hash) for every commit carrying an audit-head trailer."""
        out = self._git("log", "--format=%H%x1f%B%x1e")  # unit/record separators: safe in any message
        anchors = []
        for record in out.split(_RECORD_SEP):
            if _UNIT_SEP not in record:
                continue
            commit, body = record.strip().split(_UNIT_SEP, 1)
            for line in body.splitlines():
                if line.startswith(f"{ANCHOR_TRAILER}: "):
                    seq, digest = line.split(": ", 1)[1].split()
                    anchors.append((commit, int(seq), digest))
        return anchors

    def log(self, limit: int = 20) -> list[tuple[str, str]]:
        out = self._git("log", f"-{limit}", "--format=%H%x09%s")
        return [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]  # type: ignore[misc]
