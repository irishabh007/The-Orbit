# Reviewer walkthrough (about 10 minutes)

A guided tour: what to run, where to look, and which evaluation criterion each step addresses.
Everything runs offline, with no key needed. Setup is the 2-line quick start in the
[README](../README.md).

## 1. Watch a governed run (2 min)

```bash
python run_demo.py greenfield --auto-approve
```

The live feed shows each event as the orchestrator writes it. Look for:

- **Decomposition:** `requirements` turns the brief into 9 requirements and asks 2 blocking
  questions, which escalate to a human. `architect` plans 12 traced tasks.
- **Parallel paths and a join:** `developer`, `tester` and `docs` run at the same time; `verify`
  waits for code and tests; `release` waits for verify and docs.
- **Policy escalation:** `developer` is escalated because it writes `requirements.txt`, a protected
  path.
- **Non-linear recovery:** `verify` fails twice. Each time the failing test output goes back to the
  developer and tester (`REPLAN`), and the third verification passes.
- **Human final call:** `release` waits for approval.

*Criteria: effectiveness of agentic orchestration; depth of decomposition and execution.*

## 2. Read the evidence (3 min)

Open `runs/greenfield/report.md` (VS Code: Ctrl+Shift+V renders the Mermaid diagram).

| Section | What it proves |
|---|---|
| Reliability metrics | Computed from the audit log alone: 100% run success, 14 agent attempts (79% succeeded), 2 repair loops, 2 incidents recovered, MTTR |
| Model usage and cost | 12 model calls, tokens and estimated cost per agent |
| Workflow | The dependency graph with each node's final state; 🔒 marks human gates |
| Timeline | Every transition, retry, policy decision and approval |
| Human approvals | Who decided, why they were asked, how long they took |
| Decision lineage | The release decision traced through 77 causally linked events back to all 6 human decisions and every artifact version |
| Audit integrity | Hash chain verified, and the head hash anchored in the generated repo's git commit |

Then look at the output itself: `runs/greenfield/repo` is a plain git repository (FastAPI app, 30
tests, README, docs) with no dependency on Warden. `git log` shows one commit listing the
requirements, the approvals, and the `Warden-Audit-Head` trailer.

*Criteria: realism and quality of outputs; audit-grade observability.*

## 3. Break things on purpose (2 min)

```bash
python run_demo.py failures
```

There are eight scripted cases, all running through the real orchestrator, policy and gates:

| Case | Criterion |
|---|---|
| `policy-block`: hard-coded AWS key denied, never retried, never logged | Security guardrails |
| `human-reject`: rejected design, nothing downstream runs | Controlled autonomy |
| `retry-fallback-recover` / `retry-fallback-rollback` | Bounded retry, fallback, rollback |
| `safe-stop`: step budget exhausted | Safe-stop |
| `replan-on-change`: request changed after the run; only affected nodes redo work | Dynamic re-planning |
| `human-rollback`: a shipped feature rolled back, held, released, rebuilt | Safe change management |
| `resume-midway`: start from existing approved artifacts | Brownfield entry at any point |

Each case writes its own `runs/failures/<case>/report.md`.

## 4. Be the human (1 min)

```bash
python run_demo.py greenfield
```

At each gate you can:

- **a** approve
- **n** approve with a note (kept in the audit log, the report and the commit message)
- **c** request changes: your comment goes back to the agent, which redoes the work
- **r** reject, with a reason
- **v** view the staged spec, design or file list first

Approving every gate follows the recorded path. A different decision takes the run down a path that has no
recording yet, so replay stops with a clear message (`--record` with a key explores it live).

*Criterion: humans own oversight, approvals and final quality.*

## 5. Build your own idea (optional, needs a key, ~10 min)

```bash
python run_demo.py new
```

Type a vague idea, answer the questions one at a time, and approve the spec and the plan. Warden
then builds an ASP.NET Core API with xUnit tests and verifies it with `dotnet build`/`dotnet test`.
Anything outside the plan you approved comes back to you first.

*Criteria: requirement understanding; controlled autonomy; realism of outputs.*

## 6. Check the engineering (2 min)

```bash
pytest -q && pytest -q -m slow && ruff check . && mypy warden demo run_demo.py
```

These run 297 fast tests plus 11 subprocess tests (including real `dotnet build`/`dotnet test`), including the recorded run replayed end to end
with the real generated tests executing. Then skim:

- [ARCHITECTURE.md](ARCHITECTURE.md): state machine, per-attempt control flow, bounded failure
  handling, key decisions with alternatives.
- [ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md): risks with how each is validated, assumptions,
  honest limitations, and **15 defects found only by real runs and environment probes**, each fixed
  with a regression test.

*Criteria: architecture quality; validation and risk rigor; defensible decisions; engineering
judgment.*

## Questions this answers quickly

| Question | Where |
|---|---|
| How do you stop the agent doing something dangerous? | Policy + autonomy per role + approvals: ARCHITECTURE "Governance" |
| What happens when the model is wrong? | Schema, grounding, verify, repair loops, bounded retries: ARCHITECTURE "Control flow" |
| Can I trust the numbers? | They come from the hash-chained log; `python -m warden.report runs/greenfield/events.jsonl` recomputes them |
| Can a human undo things? | `human-rollback` demo; git revert per feature |
| How does it handle existing code? | Brownfield: scanner, grounded impact analysis, human sign-off, SCENARIOS §2 |
