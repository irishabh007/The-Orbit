"""Snapshot a finished run as a brownfield scenario's existing app.

    python -m demo.baseline runs/greenfield                                   # for brownfield
    python -m demo.baseline runs/brownfield url-shortener-dotnet-analytics    # for harden
"""

from __future__ import annotations

import sys
from pathlib import Path

from demo.runner import make_baseline


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (1, 2):
        print("usage: python -m demo.baseline <run dir, e.g. runs/greenfield> [snapshot name]", file=sys.stderr)
        return 2
    run_dir = Path(args[0])
    if not (run_dir / "artifacts").is_dir():
        print(f"{run_dir} is not a finished run (no artifacts/ folder)", file=sys.stderr)
        return 2
    print(f"Baseline written: {make_baseline(run_dir, *args[1:])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
