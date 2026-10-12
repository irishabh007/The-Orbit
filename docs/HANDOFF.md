# Warden: project handoff

Internal handoff for whoever continues this work (human or agent). Read `CLAUDE.md` first.
Reviewer-facing documentation is in `README.md` and `docs/`.

## 1. The assignment (technical assessment; the brief allows 2-3 days)

Build a runnable prototype of an **agentic SDLC orchestrator**. The sample subject is a URL
shortener, but the **orchestrator is the deliverable** (the brief calls it the critical
differentiator). Target effort split: about 60% orchestrator, 25% generated shortener, 15% docs
and scenarios.

The orchestration must show:
- explicit dependency graph with entry/exit gates; sequential and parallel paths with joins
- cross-stage context and decision lineage
- human approval checkpoints for high-impact actions
- bounded retries, fallback, rollback, safe-stop
- policy guardrails (security, compliance, change control)
- audit-grade observability; metrics: success rate, retry/rollback frequency, MTTR, end-to-end latency
- dynamic re-planning when upstream outputs change

Deliverables: runnable prototype, architecture overview, **three scenarios** (greenfield, brownfield,
ambiguous; each showing decomposition, orchestration, validation), setup instructions, testing
approach, limitations and trade-offs, final engineering summary (plan, artifacts, risks, assumptions).

Owner's extra goals: turn any vague requirement into a solid spec; a human can change a requirement
or roll back a feature at any time; generated code must be easy for a human to continue; the
orchestrator can start from any point (brownfield).

## 2. Decisions already made

| Topic | Decision |
|---|---|
| Name | **Warden**; package `warden`; repo `warden-sdlc-orchestrator` |
| Language | Python 3.11+; **standard library only in `warden/` core** (exception: PyYAML for policy files) |
| Machine | Windows, Intel i5, 8 GB RAM: no Docker, no local models |
| LLM | One `LLMClient` interface: Gemini adapter (httpx, default `gemini-3.8-flash`), **record/replay** backend so reviewers run offline with no key, scripted fallback for failure demos |
| Validation of LLM output | Pydantic schemas, retry on malformed output |
| Rollback/features | git-based, per-feature commits (artifact-level rollback already implemented) |
| Packaging | Plain `python -m venv` + `pip install -r requirements-demo.txt -r requirements-dev.txt` + `run_demo.py <greenfield|brownfield|ambiguous|failures>` (`--auto-approve`, `--record`, `--live`, `--prune`). No Docker |
| CI | GitHub Actions on Ubuntu + Windows: fresh install, lint, types, tests, demos |
| Claims | Narrow and defensible: "production-grade structure and practices", not "any requirement, fully production-ready" |

## 3. Status (2026-10-12): development complete

All handoff items 1-9 are implemented and test-proven: 244 fast tests and 8 slow ones, with ruff and
mypy clean, across 34 commits. For what exists and why, see `docs/ARCHITECTURE.md` and
`docs/ENGINEERING_SUMMARY.md`, which also lists the 13 defects found by real-model runs.

| Item | State |
|---|---|
| 1 Housekeeping | Done |
| 2 YAML policy engine | Done: security, compliance (personal data), supply chain, change control, autonomy |
| 3 LLM layer | Done: Gemini, record/replay (n-th repeat recorded separately, prune), scripted, bounded retry, quota-aware errors |
| 4 Agents | Done: Requirements, Impact, Architect (+ plan), Developer, Tester, Docs, Release |
| 5 Tools | Done: verify, scanner, workspace writer, scrubbed runner, git, publish |
| 6 Demo and scenarios | Done: greenfield (**recorded**), brownfield and ambiguous (**built; not recorded**), 6 failure/control demos |
| 7 Run report | Done: metrics and report from the audit log, `python -m warden.report` |
| 8 Docs | Done: README, ARCHITECTURE, SCENARIOS, TESTING, ENGINEERING_SUMMARY, RECORDING |
| 9 CI | Written (`.github/workflows/ci.yml`); **not yet run**: no GitHub remote configured |

## 4. Remaining work

1. **Record brownfield and ambiguous** with a paid key (owner's plan), following
   `docs/RECORDING.md`. Free tier: 20 calls per day per project.
2. **Push to GitHub and check CI.** The recorded-replay step is informational: recordings were made
   on Windows, and tool output in repair prompts may differ on Linux or with newer packages. If it
   misses, consider `-p no:warnings` for verify's pytest and re-record (this invalidates repair-loop
   recordings).
3. Optional improvements: tokens and cost per model call in the event log (a cost metric in the
   report); container sandbox for verify; anchoring the log head hash externally; deleting files
   in the generated repo.

Constraint for whoever continues: **changing any agent prompt, schema, input or verify output
format invalidates recordings** for the affected calls. Re-record afterwards.

## 5. Known limitations

See `docs/ENGINEERING_SUMMARY.md#limitations-stated-honestly`.

## 6. Assessment alignment

| Brief requirement | Status | Evidence |
|---|---|---|
| 1 Requirement understanding | Done | Requirements agent; real run: 9 requirements, 4 questions; ambiguous scenario (scripted answers) |
| 2 Task decomposition | Done | Architect tasks with owners, dependencies, traceability; real run: 12 tasks |
| 3 Brownfield codebase reasoning | Done (recording pending) | Scanner + grounded impact agent; `BrownfieldScenarioTests` |
| 4 DAG, gates, parallel + joins | Done | `dag.py`, orchestrator tests |
| 4 Lineage | Done | `caused_by` + content hashes; report traces the release to every human decision |
| 4 Human approval checkpoints | Done | Graph, policy and agent escalation; fail-closed approver |
| 4 Retry, fallback, rollback, safe-stop | Done | Tests + `run_demo.py failures` |
| 4 Policy: security, compliance, change control | Done | `policies/default.yaml`, `test_policy.py` |
| 4 Observability and metrics | Done | `report.py`: success rate, retry/rollback frequency, MTTR, latency |
| 4 Dynamic re-planning | Done | `replan()`, repair loops, `replan-on-change` demo |
| 5 Engineering output | Done | Generated repo: 18 files, 30 passing tests, README and docs |
| 6 Validation and risk control | Done | Verify gate, policy, risk table in the engineering summary |
| 7 Controlled autonomy | Done | Autonomy per role; release decided by a human |
| 8 Engineering summary | Done | `docs/ENGINEERING_SUMMARY.md` |
| Deliverables: prototype, architecture, 3 scenarios, setup, testing, limitations | Done | `README.md`, `docs/` (2 of 3 scenarios still to record) |
