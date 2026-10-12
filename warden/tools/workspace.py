"""Write bundle files into a workspace directory, and nowhere else."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from warden.artifacts import ArtifactStore
from warden.bundle import BundleError, path_problem, unpack


class WorkspaceError(ValueError):
    """A write would leave the workspace, touch .git, or the bundles conflict."""


def write_files(root: str | Path, files: Mapping[str, str]) -> list[str]:
    """Write ``files`` (relative POSIX path -> text) under ``root`` with LF endings."""
    base = Path(root).resolve()
    for path in files:  # validate everything before writing anything
        problem = path_problem(path)
        if problem:
            raise WorkspaceError(problem)
        if path.split("/")[0] == ".git":
            raise WorkspaceError(f"{path!r}: writing into .git is not allowed")
        target = (base / path).resolve()
        if not target.is_relative_to(base):  # e.g. through a symlinked directory
            raise WorkspaceError(f"{path!r}: resolves outside the workspace")
    written = []
    for path, text in files.items():
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))  # bytes: no platform newline translation
        written.append(path)
    return written


def collect_bundles(store: ArtifactStore, names: Iterable[str]) -> dict[str, str]:
    """Merge the latest versions of several bundles; the same path in two bundles is an error."""
    merged: dict[str, str] = {}
    owner: dict[str, str] = {}
    for name in names:
        art = store.latest(name)
        if art is None:
            raise WorkspaceError(f"bundle {name} does not exist")
        try:
            files = unpack(store.read_text(art))
        except BundleError as exc:
            raise WorkspaceError(f"{name}: {exc}") from exc
        for path, text in files.items():
            if path in merged:
                raise WorkspaceError(f"{path} is in both {owner[path]} and {name}")
            merged[path], owner[path] = text, name
    return merged
