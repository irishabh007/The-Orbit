"""File bundles: many generated files carried as one artifact.

A node declares its outputs up front, but a developer agent cannot know every file
name in advance. So code/test/doc agents emit one ``*.bundle`` artifact: canonical
JSON ``{"files": {path: text}}``. Policy checks expand bundles file by file, and
only safe relative POSIX paths are allowed, so a bundle can never write outside
the workspace when it is materialized.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

BUNDLE_SUFFIX = ".bundle"
_DRIVE = re.compile(r"^[A-Za-z]:")


class BundleError(ValueError):
    """The bundle is malformed or contains an unsafe path."""


def is_bundle(name: str) -> bool:
    return name.endswith(BUNDLE_SUFFIX)


def path_problem(path: str) -> str | None:
    """Why ``path`` is unsafe for a workspace file, or None if it is fine."""
    if not path:
        return "empty path"
    if "\\" in path:
        return f"{path!r}: use '/' separators"
    if path.startswith("/") or _DRIVE.match(path):
        return f"{path!r}: must be relative"
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return f"{path!r}: empty, '.' or '..' segment"
    return None


def pack(files: Mapping[str, str]) -> str:
    for path in files:
        problem = path_problem(path)
        if problem:
            raise BundleError(problem)
    return json.dumps({"files": dict(sorted(files.items()))}, indent=2, sort_keys=True)


def unpack(content: str | bytes) -> dict[str, str]:
    try:
        raw = json.loads(content)
    except ValueError as exc:
        raise BundleError(f"not valid JSON: {exc}") from exc
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, dict):
        raise BundleError("expected an object with a 'files' mapping")
    for path, text in files.items():
        if not isinstance(text, str):
            raise BundleError(f"{path!r}: content must be text")
        problem = path_problem(path)
        if problem:
            raise BundleError(problem)
    return dict(files)
