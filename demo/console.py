"""Console I/O for the demo: live event feed, interactive approver, LLM call log."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from warden.approval import ApprovalDecision, ApprovalRequest
from warden.eventlog import Event, EventLog
from warden.llm import LLMClient, LLMRequest, LLMResponse
from warden.models import EventType

# first letter of the answer -> (verdict, follow-up question for the comment; "" = no comment)
_CHOICES = {
    "a": ("approve", ""),
    "n": ("approve", "Note to record with your approval: "),
    "c": ("changes", "What should change? (sent to the agent, which redoes the work): "),
    "r": ("reject", "Reason for rejecting: "),
}
_MENU = """  [a] approve
  [n] approve with a note    (recorded in the audit log, report and commit; does not block)
  [c] request changes        (your comment goes back to the agent, which redoes the work)
  [r] reject                 (stops this path; a reason is required)
  [v] view outputs           (see what you are approving first)"""


class ConsoleEventLog(EventLog):
    """An EventLog that also prints a one-line summary of each event as it is written."""

    def __init__(self, *args: Any, out: TextIO = sys.stdout, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._out = out
        self._print_lock = threading.Lock()

    def append(self, *args: Any, **kwargs: Any) -> Event:
        event = super().append(*args, **kwargs)
        line = describe(event)
        if line:
            with self._print_lock:
                print(line, file=self._out, flush=True)
        return event


def describe(e: Event) -> str:
    d = e.data
    node = f"{e.node_id or '':<12}"
    if e.type == EventType.NODE_TRANSITION.value:
        reason = f"  ({_short(d['reason'])})" if d.get("reason") else ""
        return f"[{e.seq:>4}] {node} {d['from']} -> {d['to']}{reason}"
    if e.type == EventType.POLICY_CHECK.value and (not d.get("allow") or d.get("requires_approval")):
        verdict = "DENY" if not d.get("allow") else "escalate"
        return f"[{e.seq:>4}] {node} policy {verdict}: {_short(d.get('reason', ''))}"
    if e.type == EventType.APPROVAL_DECIDED.value:
        return f"[{e.seq:>4}] {node} {e.actor}: {d['verdict']} {_short(d.get('comment', ''))}".rstrip()
    if e.type in (EventType.RETRY_SCHEDULED.value, EventType.REPLAN.value, EventType.ROLLBACK.value,
                  EventType.SAFE_STOP.value, EventType.RUN_STARTED.value, EventType.RUN_FINISHED.value):
        detail = d.get("reason") or d.get("state") or ""
        return f"[{e.seq:>4}] {node} {e.type} {_short(str(detail))}".rstrip()
    return ""


def _short(text: str, limit: int = 110) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first if len(first) <= limit else first[: limit - 3] + "..."


class ConsoleApprover:
    """Asks a human at the terminal. Fail-closed: there is no default answer."""

    def __init__(self, name: str, *, input_fn: Callable[[str], str] = input, out: TextIO = sys.stdout) -> None:
        self.name = name
        self._input = input_fn
        self._out = out

    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        p = self._out
        print("\n" + "=" * 78, file=p)
        print(f"APPROVAL NEEDED  node: {request.node_id}   run: {request.run_id}", file=p)
        print(f"Why:       {request.reason}", file=p)
        print(f"Artifacts: {', '.join(request.artifacts)}", file=p)
        print(f"Summary:   {request.summary}", file=p)
        print("=" * 78, file=p, flush=True)
        print(_MENU, file=p, flush=True)
        while True:
            answer = self._input("Your choice [a/n/c/r/v]: ").strip().lower()[:1]
            if answer in _CHOICES:
                break
            if answer == "v":
                for name, excerpt in request.preview or (("(none)", "No preview available for this step."),):
                    print(f"\n--- {name} ---\n{excerpt}", file=p)
                print(file=p, flush=True)
                continue
            print("Please choose a, n, c, r or v.", file=p, flush=True)
        verdict, question = _CHOICES[answer]
        comment = ""
        while question and not comment:
            comment = self._input(question).strip()
        return ApprovalDecision(verdict, self.name, comment)  # type: ignore[arg-type]


class LoggedClient:
    """Prints one line per model call (role, backend, latency) so long calls are visible."""

    def __init__(self, inner: LLMClient, role: str, out: TextIO = sys.stdout) -> None:
        self.inner = inner
        self.name = inner.name
        self.role = role
        self._out = out

    def complete(self, request: LLMRequest) -> LLMResponse:
        t0 = time.monotonic()
        response = self.inner.complete(request)
        tokens = f", {response.output_tokens} tokens out" if response.output_tokens else ""
        print(f"       llm  {self.role:<12} {response.backend} {time.monotonic() - t0:.1f}s{tokens}",
              file=self._out, flush=True)
        return response


def wrap_roles(clients: dict[str, LLMClient], out: TextIO) -> dict[str, LLMClient]:
    return {role: LoggedClient(c, role, out) for role, c in clients.items()}


def roles_for(client: LLMClient, roles: Sequence[str]) -> dict[str, LLMClient]:
    return dict.fromkeys(roles, client)
