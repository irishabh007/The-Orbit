"""Run report and reliability metrics, computed only from the hash-chained event log.

Nothing here is self-reported by agents: every number is derived from audited events,
so a reviewer can recompute it. Metrics (all runs in the log):

* run success rate                 runs that finished SUCCEEDED / runs
* attempt success rate             agent attempts that ended SUCCEEDED / attempts
* retry / fallback / rollback      counts and rates; repair loops (failure routed upstream)
* MTTR                             mean time from a node's first failure in an incident to
                                   its next success (unrecovered incidents counted apart)
* end-to-end latency               RUN_STARTED -> RUN_FINISHED per run
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from warden.dag import Graph
from warden.eventlog import Event, EventLog, VerifyResult
from warden.llm.pricing import estimate_cost
from warden.models import EventType

_E = EventType
_TIMELINE_TO = {"RUNNING", "SUCCEEDED", "FAILED", "ROLLED_BACK", "AWAITING_APPROVAL"}


def _ts(event: Event) -> datetime:
    return datetime.fromisoformat(event.ts)


def _seconds(a: Event, b: Event) -> float:
    return round((_ts(b) - _ts(a)).total_seconds(), 3)


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def compute_metrics(events: Sequence[Event]) -> dict[str, Any]:
    by_type: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_type[e.type].append(e)
    transitions = by_type[_E.NODE_TRANSITION.value]

    runs = []
    for start in by_type[_E.RUN_STARTED.value]:
        end = next((e for e in by_type[_E.RUN_FINISHED.value] if e.run_id == start.run_id and e.seq > start.seq), None)
        runs.append({"run_id": start.run_id, "state": end.data.get("state") if end else "UNFINISHED",
                     "reason": end.data.get("reason", "") if end else "", "steps": end.data.get("steps") if end else None,
                     "latency_s": _seconds(start, end) if end else None})

    attempts = [e for e in transitions if e.data.get("to") == "RUNNING"]
    succeeded = [e for e in transitions if e.data.get("to") == "SUCCEEDED" and not e.data.get("adopted")]
    failed = [e for e in transitions if e.data.get("to") == "FAILED"]
    retries = by_type[_E.RETRY_SCHEDULED.value]
    fallbacks = [e for e in retries if e.data.get("fallback_agent")]
    rollbacks = by_type[_E.ROLLBACK.value]
    repair_loops = [e for e in by_type[_E.REPLAN.value] if e.data.get("feedback_to")]

    recovery_times, unrecovered = _incidents(transitions)
    nodes: dict[str, dict[str, Any]] = {}
    for e in transitions:
        n = nodes.setdefault(e.node_id or "?", {"attempts": 0, "failures": 0, "state": None, "duration_ms": None})
        n["state"] = e.data.get("to")
        n["attempts"] += e.data.get("to") == "RUNNING"
        n["failures"] += e.data.get("to") == "FAILED"
        if e.data.get("to") == "SUCCEEDED" and "duration_ms" in e.data:
            n["duration_ms"] = e.data["duration_ms"]

    requested = {e.seq: e for e in by_type[_E.APPROVAL_REQUESTED.value]}
    approvals = []
    for d in by_type[_E.APPROVAL_DECIDED.value]:
        req = next((requested[c] for c in d.caused_by if c in requested), None)
        approvals.append({"node": d.node_id, "verdict": d.data.get("verdict"), "by": d.actor,
                          "comment": d.data.get("comment", ""), "reason": req.data.get("reason", "") if req else "",
                          "wait_s": _seconds(req, d) if req else None})

    checks = by_type[_E.POLICY_CHECK.value]
    rules: dict[str, int] = defaultdict(int)
    for c in checks:
        if not c.data.get("allow", True) or c.data.get("requires_approval"):
            for rule in str(c.data.get("rule") or "unnamed").split(","):
                rules[rule] += 1

    return {
        "runs": runs,
        "run_success_rate": _rate(sum(r["state"] == "SUCCEEDED" for r in runs), len(runs)),
        "node_attempts": len(attempts),
        "attempt_success_rate": _rate(len(succeeded), len(attempts)),
        "failures": len(failed),
        "retries": len(retries),
        "retry_rate": _rate(len(retries), len(attempts)),
        "fallbacks": len(fallbacks),
        "rollbacks": len(rollbacks),
        "rollback_rate": _rate(len(rollbacks), len(runs)),
        "repair_loops": len(repair_loops),
        "recoveries": len(recovery_times),
        "unrecovered": unrecovered,
        "mttr_s": round(mean(recovery_times), 3) if recovery_times else None,
        "approvals": approvals,
        "policy": {"denied": sum(not c.data.get("allow", True) for c in checks),
                   "escalated": sum(bool(c.data.get("requires_approval")) for c in checks),
                   "rules": dict(sorted(rules.items()))},
        "nodes": nodes,
        "events": len(events),
        "model_usage": _model_usage([e for e in by_type[_E.NOTE.value] if "model_usage" in e.data]),
    }


def _call_cost(call: dict[str, Any]) -> float | None:
    return estimate_cost(str(call.get("model")), call.get("input_tokens") or 0, call.get("output_tokens") or 0,
                         call.get("thinking_tokens") or 0)


def _usage_totals(calls: Sequence[dict[str, Any]]) -> dict[str, Any]:
    costs = [_call_cost(c) for c in calls]
    priced = [c for c in costs if c is not None]
    return {
        "calls": len(calls),
        "input_tokens": sum(c.get("input_tokens") or 0 for c in calls),
        "output_tokens": sum(c.get("output_tokens") or 0 for c in calls),
        "thinking_tokens": sum(c.get("thinking_tokens") or 0 for c in calls),
        # a node's cost is unknown if any of its calls is unpriced; the run total sums what is priced
        "cost_usd": round(sum(priced), 4) if len(priced) == len(costs) else None,
        "priced_cost_usd": round(sum(priced), 4),
        "unpriced_calls": len(costs) - len(priced),
    }


def _model_usage(notes: Sequence[Event]) -> dict[str, Any]:
    """Tokens and estimated cost, total and per node, from the scheduler's ``model_usage`` notes."""
    per_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in notes:
        per_node[event.node_id or "?"].extend(event.data.get("model_usage", []))
    every = [c for calls in per_node.values() for c in calls]
    total = _usage_totals(every)
    total["cost_usd"] = total.pop("priced_cost_usd")
    return {
        **total,
        "models": sorted({str(c.get("model")) for c in every}),
        "calls_without_thinking_count": sum(c.get("thinking_tokens") is None for c in every),
        "by_node": {node: {k: v for k, v in _usage_totals(calls).items() if k != "priced_cost_usd"}
                    for node, calls in per_node.items()},
    }


def _incidents(transitions: Sequence[Event]) -> tuple[list[float], int]:
    """Per node: an incident opens at its first FAILED and closes at its next SUCCEEDED."""
    open_since: dict[str, Event] = {}
    times: list[float] = []
    for e in transitions:
        node, to = e.node_id or "?", e.data.get("to")
        if to == "FAILED" and node not in open_since:
            open_since[node] = e
        elif to == "SUCCEEDED" and node in open_since:
            times.append(_seconds(open_since.pop(node), e))
    return times, len(open_since)


# ---------------------------------------------------------------------- rendering
def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.0%}" if suffix == "%" else f"{value:g}{suffix}"
    return f"{value}{suffix}"


def _cell(text: Any, limit: int = 140) -> str:
    one_line = " ".join(str(text).split()).replace("|", "\\|")
    return one_line if len(one_line) <= limit else one_line[: limit - 3] + "..."


def _lineage(events: Sequence[Event], seq: int) -> list[Event]:
    by_seq = {e.seq: e for e in events}
    seen: set[int] = set()
    stack = [seq]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in by_seq:
            continue
        seen.add(cur)
        stack.extend(by_seq[cur].caused_by)
    return [by_seq[s] for s in sorted(seen)]


Anchor = tuple[str, int, str]  # (where it was published, e.g. a git commit; event seq; event hash)


def render_report(events: Sequence[Event], metrics: dict[str, Any], *, graph: Graph | None = None,
                  title: str = "Warden run report", audit: VerifyResult | None = None,
                  anchors: Sequence[Anchor] = ()) -> str:
    if not events:
        return f"# {title}\n\nNo events.\n"
    sections = [
        _summary(metrics),
        _reliability(metrics),
        _usage(metrics),
        _workflow(metrics, graph),
        _timeline(events),
        _approvals(metrics),
        _policy(events),
        _decision_lineage(events),
        _audit(events, audit, anchors),
    ]
    return "\n".join([f"# {title}", "", *(line for section in sections for line in section)]) + "\n"


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
            *("| " + " | ".join(str(c) for c in row) + " |" for row in rows)]


def _summary(m: dict[str, Any]) -> list[str]:
    rows = [(r["run_id"], r["state"], _fmt(r["latency_s"], "s"), _fmt(r["steps"]), _cell(r["reason"] or "-"))
            for r in m["runs"]]
    return ["## Summary", "", *_table(("Run", "State", "End-to-end latency", "Agent executions", "Reason"), rows)]


def _reliability(m: dict[str, Any]) -> list[str]:
    rows = [
        ("Run success rate", _fmt(m["run_success_rate"], "%")),
        ("Agent attempts", _fmt(m["node_attempts"])),
        ("Attempt success rate", _fmt(m["attempt_success_rate"], "%")),
        ("Failed attempts", _fmt(m["failures"])),
        ("Retries (rate per attempt)", f"{m['retries']} ({_fmt(m['retry_rate'], '%')})"),
        ("Fallbacks to a backup agent", _fmt(m["fallbacks"])),
        ("Rollbacks (per run)", f"{m['rollbacks']} ({_fmt(m['rollback_rate'])})"),
        ("Repair loops (failure routed upstream)", _fmt(m["repair_loops"])),
        ("Incidents recovered / unrecovered", f"{m['recoveries']} / {m['unrecovered']}"),
        ("MTTR (first failure -> recovery)", _fmt(m["mttr_s"], "s")),
        ("Policy denials / escalations", f"{m['policy']['denied']} / {m['policy']['escalated']}"),
        ("Audit events", _fmt(m["events"])),
    ]
    return ["", "## Reliability metrics", "", *_table(("Metric", "Value"), rows)]


def _usage(m: dict[str, Any]) -> list[str]:
    u = m["model_usage"]
    out = ["", "## Model usage and cost", ""]
    if not u["calls"]:
        return [*out, "No model calls were made (scripted or tool-only run)."]
    rows = [(node, b["calls"], f"{b['input_tokens']:,}", f"{b['output_tokens']:,}", f"{b['thinking_tokens']:,}",
             "n/a" if b["cost_usd"] is None else f"${b['cost_usd']:.4f}") for node, b in u["by_node"].items()]
    rows.append(("**total**", u["calls"], f"{u['input_tokens']:,}", f"{u['output_tokens']:,}",
                 f"{u['thinking_tokens']:,}", f"${u['cost_usd']:.4f}"))
    out += _table(("Node", "Calls", "Input tokens", "Output tokens", "Thinking tokens", "Est. cost"), rows)
    notes = [f"Models: {', '.join(u['models'])}. Cost estimated at list prices in `warden/llm/pricing.py`."]
    if u["unpriced_calls"]:
        notes.append(f"{u['unpriced_calls']} call(s) used a model without a listed price and are not in the total.")
    if u["calls_without_thinking_count"]:
        notes.append(f"{u['calls_without_thinking_count']} call(s) have no thinking-token count (recorded before "
                     "it was captured), so their cost is understated.")
    return [*out, "", *notes]


def _workflow(m: dict[str, Any], graph: Graph | None) -> list[str]:
    out = ["", "## Workflow", ""]
    if graph is not None:
        states = {n: str(info["state"]) for n, info in m["nodes"].items()}
        out += ["Final state of every node (🔒 = human approval required).", "",
                "```mermaid", graph.to_mermaid(states), "```"]
    else:
        out.append("_Graph not supplied; see the node table below._")
    rows = [(node, info["state"], info["attempts"], info["failures"], _fmt(info["duration_ms"], " ms"))
            for node, info in m["nodes"].items()]
    return [*out, "", *_table(("Node", "Final state", "Attempts", "Failures", "Last duration"), rows)]


def _timeline(events: Sequence[Event]) -> list[str]:
    t0 = _ts(events[0])
    rows = [(f"+{(_ts(e) - t0).total_seconds():.1f}s", e.node_id or "-", row[0], _cell(row[1]))
            for e in events if (row := _timeline_row(e))]
    return ["", "## Timeline", "", *_table(("t", "Node", "Event", "Detail"), rows)]


def _approvals(m: dict[str, Any]) -> list[str]:
    if not m["approvals"]:
        return ["", "## Human approvals", "", "No approval gates were reached."]
    rows = [(a["node"], a["verdict"], a["by"], _fmt(a["wait_s"], "s"), _cell(a["reason"]), _cell(a["comment"] or "-"))
            for a in m["approvals"]]
    return ["", "## Human approvals", "", *_table(("Node", "Verdict", "By", "Waited", "Why asked", "Comment"), rows)]


def _policy(events: Sequence[Event]) -> list[str]:
    flagged = [e for e in events if e.type == _E.POLICY_CHECK.value
               and (not e.data.get("allow", True) or e.data.get("requires_approval"))]
    if not flagged:
        return ["", "## Policy decisions", "", "Every policy check passed without escalation."]
    rows = [(e.node_id, e.data.get("phase"), "DENY" if not e.data.get("allow", True) else "escalate",
             e.data.get("rule") or "-", _cell(e.data.get("reason", ""))) for e in flagged]
    return ["", "## Policy decisions", "", *_table(("Node", "Phase", "Decision", "Rule", "Reason"), rows)]


def _decision_lineage(events: Sequence[Event]) -> list[str]:
    out = ["", "## Decision lineage", ""]
    final = next((e for e in reversed(events) if e.type == _E.NODE_TRANSITION.value
                  and e.data.get("to") == "SUCCEEDED"), None)
    if final is None:
        return [*out, "No node succeeded, so there is no committed decision to trace."]
    chain = _lineage(events, final.seq)
    humans = [e for e in chain if e.actor.startswith("human:")]
    intro = (f"The last committed decision (event {final.seq}: `{final.node_id}` SUCCEEDED) traces back "
             f"through {len(chain)} causally linked events, including {len(humans)} human decision(s):")
    out += [intro, ""]
    for e in humans:
        comment = f": {_cell(e.data.get('comment'), 100)}" if e.data.get("comment") else ""
        out.append(f"- event {e.seq}: {e.actor} {e.data.get('verdict', '')} on `{e.node_id}`{comment}")
    artifacts = [e.data["artifact"] for e in chain if e.type == _E.ARTIFACT_CREATED.value]
    if artifacts:
        out += ["", "Artifacts in the chain: " + ", ".join(f"`{a}`" for a in artifacts)]
    return out


def _audit(events: Sequence[Event], audit: VerifyResult | None, anchors: Sequence[Anchor]) -> list[str]:
    out = ["", "## Audit integrity", ""]
    if audit is None:
        out.append("Chain not verified for this report.")
    elif audit.ok:
        out.append(f"Hash chain **verified**: {audit.count} events, head hash `{events[-1].hash[:16]}...`. "
                   "Editing, deleting or reordering any event breaks verification.")
    else:
        out.append(f"**Hash chain BROKEN** at event {audit.bad_seq}: {audit.error}")
    if anchors:
        by_seq = {e.seq: e.hash for e in events}
        mismatch = "**MISMATCH**: log differs from what was published"
        rows = [(f"`{commit[:10]}`", seq, "matches" if by_seq.get(seq) == digest else mismatch)
                for commit, seq, digest in anchors]
        out += ["", ("External anchors: audit head hashes recorded in git commits. "
                     "A rewritten log cannot reproduce them."), "", *_table(("Commit", "Event", "Result"), rows)]
    return out


def _timeline_row(e: Event) -> tuple[str, str] | None:
    d = e.data
    if e.type == _E.NODE_TRANSITION.value and d.get("to") in _TIMELINE_TO:
        return f"{d.get('from')} -> {d.get('to')}", str(d.get("reason") or d.get("agent") or "")
    if e.type == _E.APPROVAL_DECIDED.value:
        return f"approval: {d.get('verdict')}", f"{e.actor} {d.get('comment', '')}"
    if e.type == _E.POLICY_CHECK.value and (not d.get("allow", True) or d.get("requires_approval")):
        return "policy " + ("DENY" if not d.get("allow", True) else "escalate"), str(d.get("reason", ""))
    if e.type in (_E.RETRY_SCHEDULED.value, _E.ROLLBACK.value, _E.SAFE_STOP.value):
        return e.type, str(d.get("reason") or d.get("fallback_agent") or "")
    if e.type == _E.REPLAN.value:
        return "REPLAN", str(d.get("reason") or f"invalidated {d.get('invalidated')}")
    if e.type in (_E.RUN_STARTED.value, _E.RUN_FINISHED.value):
        return e.type, str(d.get("state") or "")
    return None


def write_report(log_path: str | Path, *, graph: Graph | None = None, title: str | None = None,
                 out: str | Path | None = None, anchors: Sequence[Anchor] = ()) -> Path:
    """Read and verify an event log, then write ``report.md`` and ``metrics.json`` beside it."""
    log_path = Path(log_path)
    with EventLog(log_path) as log:
        events, audit = log.read_all(), log.verify()
    metrics = compute_metrics(events)
    target = Path(out) if out else log_path.with_name("report.md")
    target.write_text(render_report(events, metrics, graph=graph, title=title or f"Warden run report: "
                                    f"{', '.join(r['run_id'] for r in metrics['runs']) or log_path.name}",
                                    audit=audit, anchors=anchors), encoding="utf-8")
    target.with_name("metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m warden.report",
                                     description="Render a run report and metrics from an event log.")
    parser.add_argument("log", type=Path, help="events.jsonl")
    parser.add_argument("-o", "--out", type=Path, help="report path (default: report.md next to the log)")
    args = parser.parse_args(argv)
    print(write_report(args.log, out=args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
