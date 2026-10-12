"""Minimal ``.env`` reader (stdlib only). Values are returned, never logged or exported."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def read_env_value(name: str, env_file: str | Path = ".env",
                   environ: Mapping[str, str] | None = None) -> str | None:
    """Return ``name`` from the process environment, else from ``env_file``, else None."""
    env = os.environ if environ is None else environ
    if env.get(name):
        return env[name]
    path = Path(env_file)
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        if key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value or None
    return None
