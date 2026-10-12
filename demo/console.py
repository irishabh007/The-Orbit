"""Console I/O for the demo: live event feed, interactive approver, LLM call log."""

from __future__ import annotations

import json
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


def read_idea(input_fn: Callable[[str], str] = input, out: TextIO = sys.stdout) -> str:
    """Multi-line idea from the terminal, finished by an empty line."""
    print("Describe what you want to build, as vaguely or precisely as you like.\n"
          "Finish with an empty line.", file=out, flush=True)
    lines: list[str] = []
    while True:
        line = input_fn("> " if not lines else "  ")
        if not line.strip():
            if lines:
                return "\n".join(lines).strip()
            print("Please describe the idea (at least one line).", file=out, flush=True)
            continue
        lines.append(line.rstrip())


def render_spec(spec_json: str) -> str:
    """A requirements spec as a human reads it."""
    try:
        s = json.loads(spec_json)
    except ValueError:
        return spec_json
    out = [str(s.get("title", "Specification")), f"Problem: {s.get('problem_statement', '')}", "", "Requirements:"]
    for r in s.get("requirements", []):
        out.append(f"  {r.get('id')} [{r.get('priority')}, {r.get('category')}] {r.get('text')}")
        out += [f"      - {c}" for c in r.get("acceptance_criteria", [])]
    for label, key in (("Assumptions", "assumptions"), ("Out of scope", "out_of_scope")):
        if s.get(key):
            out += ["", f"{label}:", *(f"  - {item}" for item in s[key])]
    if s.get("open_questions"):
        out += ["", "Open questions:"]
        out += [f"  {q.get('id')} ({'blocking' if q.get('blocking') else 'optional'}) {q.get('question')}"
                f"  -> default: {q.get('proposed_default')}" for q in s["open_questions"]]
    return "\n".join(out)


def render_design(design_json: str) -> str:
    """A design and task plan as a human reads it."""
    try:
        d = json.loads(design_json)
    except ValueError:
        return design_json
    out = [f"Overview: {d.get('overview', '')}", "", "Components:"]
    out += [f"  - {c.get('name')}: {c.get('module')} - {c.get('responsibility')}" for c in d.get("components", [])]
    out += ["", "Endpoints:"]
    out += [f"  - {e.get('method')} {e.get('path')} -> {', '.join(map(str, e.get('status_codes', [])))}"
            f"  ({', '.join(e.get('requirement_ids', []))})" for e in d.get("api", [])]
    out += ["", "Decisions:"]
    out += [f"  - {x.get('id')} {x.get('title')}: {x.get('choice')} (why: {x.get('rationale')}; "
            f"rejected: {', '.join(x.get('alternatives', [])) or '-'})" for x in d.get("decisions", [])]
    out += ["", "Tasks:"]
    out += [f"  - {t.get('id')} [{t.get('owner')}] {t.get('title')}  ({', '.join(t.get('requirement_ids', []))};"
            f" after: {', '.join(t.get('depends_on', [])) or '-'})" for t in d.get("tasks", [])]
    if d.get("risks"):
        out += ["", "Risks:", *(f"  - {r.get('risk')} -> {r.get('mitigation')}" for r in d["risks"])]
    return "\n".join(out)


class ConsoleApprover:
    """Asks a human at the terminal. Fail-closed: there is no default answer.

    At the requirements gate it first runs *discovery*: each open question is asked on its
    own with its proposed default (Enter accepts it), the answers go back to the requirements
    agent, and the revised spec returns here. After ``discovery_rounds`` rounds the human
    reviews the spec as it stands. The default (2 rounds, so at most 3 drafts) stays inside the
    requirements node's retry budget, so discovery can never use up the run.
    """

    def __init__(self, name: str, *, input_fn: Callable[[str], str] = input, out: TextIO = sys.stdout,
                 discovery_rounds: int = 2) -> None:
        self.name = name
        self._input = input_fn
        self._out = out
        self._discovery_rounds = discovery_rounds
        self._rounds_used: dict[str, int] = {}

    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        p = self._out
        print("\n" + "=" * 78, file=p)
        print(f"APPROVAL NEEDED  node: {request.node_id}   run: {request.run_id}", file=p)
        print(f"Why:       {request.reason}", file=p)
        print(f"Artifacts: {', '.join(request.artifacts)}", file=p)
        print(f"Summary:   {request.summary}", file=p)
        print("=" * 78, file=p, flush=True)
        staged = dict(request.preview)
        if "spec.json" in staged:
            questions = _open_questions(staged["spec.json"])
            if questions and self._rounds_used.get(request.node_id, 0) < self._discovery_rounds:
                self._rounds_used[request.node_id] = self._rounds_used.get(request.node_id, 0) + 1
                return self._discovery(questions)
            print(render_spec(staged["spec.json"]), file=p)
            print("\nApproving locks this spec: any later change to it needs your approval again."
                  + (" Remaining questions will use their proposed defaults." if questions else ""), file=p)
        if "design.json" in staged:
            print(render_design(staged["design.json"]), file=p)
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

    def _discovery(self, questions: list[dict[str, Any]]) -> ApprovalDecision:
        p = self._out
        print("\nThe requirements agent has questions. Answer each one, or press Enter to accept the "
              "proposed default.", file=p)
        answers = []
        for q in questions:
            kind = "blocking" if q.get("blocking") else "optional"
            print(f"\n{q.get('id')} ({kind}) {q.get('question')}\n   why it matters: {q.get('why_it_matters')}"
                  f"\n   default: {q.get('proposed_default')}", file=p, flush=True)
            answer = self._input("   your answer > ").strip()
            answers.append(f"{q.get('id')} ({q.get('question')}): "
                           + (answer or f"accepted the proposed default: {q.get('proposed_default')}"))
        notes = self._input("\nAnything else to add? (Enter to skip) > ").strip()
        comment = "Answers to the clarifying questions:\n" + "\n".join(answers)
        if notes:
            comment += f"\nAdditional notes: {notes}"
        print("\nSending your answers to the requirements agent for a revised spec...", file=p, flush=True)
        return ApprovalDecision("changes", self.name, comment)


def _open_questions(spec_json: str) -> list[dict[str, Any]]:
    try:
        questions = json.loads(spec_json).get("open_questions", [])
    except (ValueError, AttributeError):
        return []
    return [q for q in questions if isinstance(q, dict)]


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
