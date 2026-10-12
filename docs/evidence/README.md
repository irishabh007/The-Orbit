# Evidence: three recorded .NET runs

Unedited output of the recorded runs, copied from `runs/<scenario>/`. The model was
gemini-3.8-flash and the product stack is ASP.NET Core minimal APIs, EF Core + SQLite and xUnit.
Approvals were granted with `--auto-approve` (demo mode). Everything here can be regenerated
offline from `recordings/`:

```bash
python run_demo.py greenfield --auto-approve   # likewise brownfield, ambiguous
```

| | [greenfield](greenfield/) | [brownfield](brownfield/) | [ambiguous](ambiguous/) |
|---|---|---|---|
| Request | URL shortener from a written brief | Daily click analytics on the existing app | "Make links safer and give marketing better insights" |
| Outcome | **SUCCEEDED**: 31/31 tests, released | **SUCCEEDED**: released | **FAILED** safely: repair budget spent, nothing released |
| Verify, attempt by attempt | 3 → 3 → 27 → 30 → 31 of 31 | build error → pass | build error → 1 → build error → 1 → 18 → build error → 0 of 26 |
| Repair loops (budget) | 4 (4) | 1 (4) | 6 (6) |
| Model calls / cost of the recorded path | 14 / $1.12 | 10 / $0.58 | 23 / $1.78 |
| Audit events (chain verified) | 190 ✓, anchored in the commit | 113 ✓, anchored in the commit | 257 ✓ |

Costs are computed from the token counts in the recordings (`warden/llm/pricing.py`). They
cover the recorded path only, not abandoned recording attempts. Greenfield's MTTR in `metrics.json`
(about 17 min) is dominated by one slow model call during an overloaded period.

## What to look at

- **`report.md`**: metrics, the Mermaid graph, timeline, approvals with reasons, decision lineage,
  model usage and cost. Start here.
- **`events.jsonl`**: the hash-chained audit log. Every state change, policy check, approval and
  retry is an event, and `caused_by` links each decision to what triggered it.
- **`spec.json` → `design.json`**: requirements with acceptance criteria and clarifying questions,
  then a design with decisions, risks and tasks traced to requirement IDs.
- **`verify_report.json`**: what `dotnet build` / `dotnet test` reported on the attempt that passed.
- **`release_review.json`**: the release agent's checklist, with evidence, for the human's decision.
- **greenfield [`repo/`](greenfield/repo/)**: the generated project as committed. It is a normal
  .NET solution with no dependency on Warden (`dotnet test tests`).
- **brownfield [`impact.json`](brownfield/impact.json) and [`change.diff`](brownfield/change.diff)**:
  the grounded impact analysis the human signed off, and the resulting change to the existing code.
- **`commit-message.txt`**: the generated repo's commit. It lists the approvals and ends with
  `Warden-Audit-Head: <seq> <hash>`, which ties the code to the exact audit-log state.

## Check the audit trail yourself

```python
from warden.eventlog import EventLog
log = EventLog("docs/evidence/greenfield/events.jsonl")
print(log.verify())                       # VerifyResult(ok=True, count=190, ...)
print(log.matches_anchor(189, "d34444a409f5a9ac9d1dcb731087abbf83a589e2dc8bf25e39c29bd207e6f866"))  # True
```

Editing any line breaks the chain. Rewriting the whole log with fresh hashes still fails
`matches_anchor`, because the anchor was recorded in git before the rewrite.

## Why the ambiguous run is kept as a failure

The vague brief turned into a much broader spec (SSRF protection, rate limiting, analytics). The
agents converged slowly, and the last repair attempt regressed (the test host registered one
configuration key twice). Warden did what it is meant to do: the loop budget held, release never
ran, and the log shows every attempt and the feedback each agent received. Re-rolling the run until
it passed would have hidden that, so it stays as recorded.
