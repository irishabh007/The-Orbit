"""Command line for the demo scenarios. See ``python run_demo.py --help``."""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from demo.console import read_idea
from demo.failures import CASES, run_case
from demo.newproject import new_project_scenario
from demo.runner import (
    DEFAULT_POLICY,
    REQUESTS,
    ROOT,
    BaselineMissing,
    Scenario,
    load_scenario,
    run_scenario,
    scenario_approver,
)
from warden.llm import LLMClient, LLMError, RecordReplayClient, RetryingClient
from warden.models import RunState
from warden.stacks import DEFAULT_NEW_PROJECT_STACK, STACKS, get_stack

RECORDINGS = ROOT / "recordings"


def _login() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # no login name (some containers): fall back to a neutral name
        return "reviewer"


def _parser() -> argparse.ArgumentParser:
    scenarios = sorted(p.stem for p in REQUESTS.glob("*.md"))
    p = argparse.ArgumentParser(prog="run_demo.py", description="Run a Warden demo scenario end to end.")
    p.add_argument("scenario", choices=[*scenarios, "failures", "new"],
                   help="'failures': scripted failure demos (no key needed); "
                        "'new': build your own idea interactively (needs GEMINI_API_KEY)")
    p.add_argument("--case", choices=sorted(CASES), help="with 'failures': run only this case")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true",
                      help="call Gemini for any prompt not yet recorded and save it (needs GEMINI_API_KEY)")
    mode.add_argument("--live", action="store_true", help="always call Gemini, record nothing")
    p.add_argument("--auto-approve", action="store_true",
                   help="grant every approval gate automatically (demo only; default asks you)")
    p.add_argument("--approver", default=_login(),
                   help="identity recorded for your approvals (default: your login name)")
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY,
                   help="policy file (default: policies/default.yaml; policies/governed.yaml also "
                        "restricts who may approve which gate)")
    p.add_argument("--out", type=Path, default=ROOT / "runs", help="output directory (default: runs/)")
    new = p.add_argument_group("new project (with 'new')")
    new.add_argument("--request-file", type=Path, help="read the idea from this file instead of typing it")
    new.add_argument("--name", help="short project name (asked if omitted)")
    new.add_argument("--stack", default=DEFAULT_NEW_PROJECT_STACK, choices=sorted(STACKS),
                     help=f"technology of the generated project (default: {DEFAULT_NEW_PROJECT_STACK})")
    p.add_argument("--recordings", type=Path, default=RECORDINGS, help=argparse.SUPPRESS)
    p.add_argument("--prune", action="store_true",
                   help="replay only: after a successful run, delete recordings this run did not use")
    return p


def _gemini() -> LLMClient:
    # adapter: imported only when a real model is wanted, so replay runs without httpx
    from warden.llm.gemini import GeminiClient

    return RetryingClient(GeminiClient.from_env(ROOT / ".env", timeout=600.0))  # a big C# bundle can take ~300s


class CliError(Exception):
    """A user-facing problem: printed and turned into exit code 2."""


def _model(args: argparse.Namespace, name: str, *, record_by_default: bool = False) -> tuple[LLMClient, str]:
    """The model client for the chosen mode, plus a label for the report title.

    A new project records by default (so the session can be replayed later); it falls back
    to replay when there is no key but a recording exists.
    """
    recordings = args.recordings / name
    try:
        if args.live:
            return _gemini(), "live Gemini"
        if args.record or record_by_default:
            return RecordReplayClient(recordings, _gemini(), mode="auto"), "recorded with Gemini, new prompts live"
    except LLMError as exc:
        if not (record_by_default and recordings.is_dir()):
            raise CliError(f"Cannot use Gemini: {exc}\nPut GEMINI_API_KEY in .env (see README).") from exc
    if not recordings.is_dir():
        raise CliError(f"No recordings for {name!r} in {recordings}. Record them once with:\n"
                       f"  python run_demo.py {args.scenario} --record --auto-approve")
    return (RecordReplayClient(recordings, mode="replay"),
            "offline replay of recorded model answers; timings exclude model latency")


def _new_scenario(args: argparse.Namespace, input_fn: Callable[[str], str] = input,
                  out: TextIO = sys.stdout) -> Scenario:
    print("New project: describe it, answer the questions, approve the spec and the plan; then Warden "
          "builds, tests and documents it. Nothing outside the approved plan lands without your approval.\n",
          file=out)
    if args.request_file:
        idea = args.request_file.read_text(encoding="utf-8")
    else:
        idea = read_idea(input_fn, out)
    name = args.name or ""
    while not name.strip():
        name = input_fn("Short project name (e.g. link-shortener): ")
    return new_project_scenario(name, idea, get_stack(args.stack))


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
        if args.scenario == "new":
            scenario = _new_scenario(args)
            llm, mode = _model(args, scenario.name, record_by_default=True)
        else:
            try:
                scenario = load_scenario(args.scenario)
            except BaselineMissing as exc:
                raise CliError(str(exc)) from exc
            llm, mode = _model(args, args.scenario)
        _explain_approvals(scenario, args.auto_approve)
        approver = scenario_approver(scenario, auto=args.auto_approve, name=args.approver)
        result = run_scenario(scenario, args.out / scenario.name, llm, approver, mode=mode,
                              policy_path=args.policy)
        succeeded = result.summary.state == RunState.SUCCEEDED
        if args.prune:
            _prune(llm, succeeded)
        return 0 if succeeded else 1
    except CliError as exc:
        print(exc, file=sys.stderr)
        return 2
