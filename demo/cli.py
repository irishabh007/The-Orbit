"""Command line for the demo scenarios. See ``python run_demo.py --help``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from demo.failures import CASES, run_case
from demo.runner import (
    REQUESTS,
    ROOT,
    Scenario,
    load_scenario,
    run_scenario,
    scenario_approver,
)
from warden.llm import LLMClient, LLMError, RecordReplayClient, RetryingClient
from warden.models import RunState

RECORDINGS = ROOT / "recordings"


def _parser() -> argparse.ArgumentParser:
    scenarios = sorted(p.stem for p in REQUESTS.glob("*.md"))
    p = argparse.ArgumentParser(prog="run_demo.py", description="Run a Warden demo scenario end to end.")
    p.add_argument("scenario", choices=[*scenarios, "failures"],
                   help="'failures' runs the failure demos (scripted model answers, no key needed)")
    p.add_argument("--case", choices=sorted(CASES), help="with 'failures': run only this case")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true",
                      help="call Gemini for any prompt not yet recorded and save it (needs GEMINI_API_KEY)")
    mode.add_argument("--live", action="store_true", help="always call Gemini, record nothing")
    p.add_argument("--auto-approve", action="store_true",
                   help="grant every approval gate automatically (demo only; default asks you)")
    p.add_argument("--approver", default="reviewer", help="name recorded for your approvals")
    p.add_argument("--out", type=Path, default=ROOT / "runs", help="output directory (default: runs/)")
    p.add_argument("--prune", action="store_true",
                   help="replay only: after a successful run, delete recordings this run did not use")
    return p


def _gemini() -> LLMClient:
    # adapter: imported only when a real model is wanted, so replay runs without httpx
    from warden.llm.gemini import GeminiClient

    return RetryingClient(GeminiClient.from_env(ROOT / ".env", timeout=300.0))


class CliError(Exception):
    """A user-facing problem: printed and turned into exit code 2."""


def _model(args: argparse.Namespace) -> tuple[LLMClient, str]:
    """The model client for the chosen mode, plus a label for the report title."""
    recordings = RECORDINGS / args.scenario
    try:
        if args.live:
            return _gemini(), "live Gemini"
        if args.record:
            return RecordReplayClient(recordings, _gemini(), mode="auto"), "recorded with Gemini, new prompts live"
    except LLMError as exc:
        raise CliError(f"Cannot use Gemini: {exc}") from exc
    if not recordings.is_dir():
        raise CliError(f"No recordings for {args.scenario!r} in {recordings}. Record them once with:\n"
                       f"  python run_demo.py {args.scenario} --record --auto-approve")
    return (RecordReplayClient(recordings, mode="replay"),
            "offline replay of recorded model answers; timings exclude model latency")


def _explain_approvals(scenario: Scenario, auto: bool) -> None:
    if auto:
        extra = "; clarifying questions get the scripted answers in demo/answers/." if scenario.answers else "."
        print("NOTE: --auto-approve grants every approval gate automatically (demo mode)" + extra)
    elif scenario.answers:
        print("At the requirements gate, answer the questions with [c]hanges. Example answers:\n"
              + scenario.answers)


def _prune(llm: LLMClient, succeeded: bool) -> int:
    if not isinstance(llm, RecordReplayClient) or llm.mode != "replay":
        raise CliError("--prune only applies to a replay run")
    if not succeeded:
        raise CliError("Not pruning: the replay did not succeed.")
    removed = llm.prune()
    print(f"Pruned {len(removed)} unused recording(s); kept {len(llm.used)}.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.scenario == "failures":
        for name in [args.case] if args.case else list(CASES):
            run_case(name, args.out / "failures" / name)
        return 0  # each case demonstrates a failure; reaching here means all behaved as shown
    try:
        if args.prune and (args.live or args.record):  # validate before any model call
            raise CliError("--prune only applies to a replay run (not with --live or --record)")
        llm, mode = _model(args)
        scenario = load_scenario(args.scenario)
        _explain_approvals(scenario, args.auto_approve)
        approver = scenario_approver(scenario, auto=args.auto_approve, name=args.approver)
        result = run_scenario(scenario, args.out / args.scenario, llm, approver, mode=mode)
        succeeded = result.summary.state == RunState.SUCCEEDED
        if args.prune:
            _prune(llm, succeeded)
        return 0 if succeeded else 1
    except CliError as exc:
        print(exc, file=sys.stderr)
        return 2
