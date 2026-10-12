"""Crash-safe file replacement shared by the state file, artifact index and recordings.

Write to a unique temp file next to the target, then ``os.replace`` it: readers see
the old or the new content, never half a file. On Windows the replace fails with
``PermissionError`` while another process (antivirus, search indexer, cloud sync)
briefly has the target open, so we retry a bounded number of times with backoff.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

_ATTEMPTS = 8
_FIRST_DELAY = 0.01  # seconds; doubles each time (about 2.5s in total at most)


def atomic_write(path: str | Path, data: str | bytes, *,
                 sleep: Callable[[float], None] = time.sleep) -> None:
    target = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        for attempt in range(_ATTEMPTS):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == _ATTEMPTS - 1:
                    raise
                sleep(_FIRST_DELAY * 2**attempt)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
