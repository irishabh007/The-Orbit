"""Exit gate for the consistency review: blocking findings go back to whoever owns them.

The reviewer writes ``review.json``. Minor findings are kept for the release reviewer and the
human; blocking ones close the gate, and only their owners (developer, tester, docs) redo work.
"""

from __future__ import annotations

import json

from warden.agent import AgentResult
from warden.dag import GateResult

REVIEW = "review.json"


def review_gate(result: AgentResult) -> GateResult:
    try:
        findings = json.loads(result.outputs[REVIEW])["findings"]
    except (KeyError, TypeError, ValueError):
        return GateResult(False, f"missing or unreadable {REVIEW}")
    blocking = [f for f in findings if f.get("severity") == "blocking"]
    if not blocking:
        return GateResult(True)
    lines = [f"- [{f['owner']}] claim: {f['claim']} | reality: {f['reality']} | evidence: {f['evidence']} "
             f"| fix: {f['fix']}" for f in blocking]
    owners = tuple(sorted({f["owner"] for f in blocking}))
    return GateResult(False, f"consistency review: {len(blocking)} blocking finding(s)\n" + "\n".join(lines),
                      replan=owners)
