"""Snapshot a finished greenfield run as the brownfield scenario's existing app.

    python -m demo.baseline runs/greenfield
"""

from __future__ import annotations

import sys
from pathlib import Path

from demo.runner import make_baseline


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m demo.baseline <run dir, e.g. runs/greenfield>", file=sys.stderr)
        return 2
    run_dir = Path(args[0])
    if not (run_dir / "artifacts").is_dir():
        print(f"{run_dir} is not a finished run (no artifacts/ folder)", file=sys.stderr)
        return 2
    print(f"Baseline written: {make_baseline(run_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
