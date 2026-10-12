"""Command line for the demo scenarios. See ``python run_demo.py --help``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from demo.console import ConsoleApprover
from demo.runner import REQUESTS, ROOT, load_scenario, run_scenario
from warden.approval import ApprovalDecision, Approver, ScriptedApprover
from warden.llm import LLMClient, LLMError, RecordReplayClient, RetryingClient
from warden.models import RunState

RECORDINGS = ROOT / "recordings"


def _parser() -> argparse.ArgumentParser:
    scenarios = sorted(p.stem for p in REQUESTS.glob("*.md"))
    p = argparse.ArgumentParser(prog="run_demo.py", description="Run a Warden demo scenario end to end.")
    p.add_argument("scenario", choices=scenarios)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true",
                      help="call Gemini for any prompt not yet recorded and save it (needs GEMINI_API_KEY)")
    mode.add_argument("--live", action="store_true", help="always call Gemini, record nothing")
    p.add_argument("--auto-approve", action="store_true",
                   help="grant every approval gate automatically (demo only; default asks you)")
    p.add_argument("--approver", default="reviewer", help="name recorded for your approvals")
    p.add_argument("--out", type=Path, default=ROOT / "runs", help="output directory (default: runs/)")
    return p


def _gemini() -> LLMClient:
    # adapter: imported only when a real model is wanted, so replay runs without httpx
    from warden.llm.gemini import GeminiClient

    return RetryingClient(GeminiClient.from_env(ROOT / ".env", timeout=300.0))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    recordings = RECORDINGS / args.scenario
    try:
        if args.live:
            llm: LLMClient = _gemini()
        elif args.record:
            llm = RecordReplayClient(recordings, _gemini(), mode="auto")
        else:
            if not recordings.is_dir():
                print(f"No recordings for {args.scenario!r} in {recordings}. Record them once with:\n"
                      f"  python run_demo.py {args.scenario} --record --auto-approve", file=sys.stderr)
                return 2
            llm = RecordReplayClient(recordings, mode="replay")
    except LLMError as exc:
        print(f"Cannot use Gemini: {exc}", file=sys.stderr)
        return 2
    approver: Approver
    if args.auto_approve:
        print("NOTE: --auto-approve grants every approval gate automatically (demo mode).")
        approver = ScriptedApprover(default=ApprovalDecision("approve", "auto-approve", "--auto-approve"))
    else:
        approver = ConsoleApprover(args.approver)
    result = run_scenario(load_scenario(args.scenario), args.out / args.scenario, llm, approver)
    return 0 if result.summary.state == RunState.SUCCEEDED else 1
