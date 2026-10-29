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
| Claims | Narrow and defensible: governed, reviewed, tested output with a human release decision; **not** "production-ready". An external review found two bugs the gates missed (README "Known gap") |

## 3. Status (2026-10-30): development complete; all four .NET scenarios recorded

All handoff items 1-9 are implemented and test-proven: 344 fast tests and 11 slow ones, with ruff and
mypy clean. For what exists and why, see `docs/ARCHITECTURE.md` and
`docs/ENGINEERING_SUMMARY.md`, which also lists the 20 defects found by real runs and environment probes.

| Item | State |
|---|---|
| 1 Housekeeping | Done |
| 2 YAML policy engine | Done: security, compliance (personal data), supply chain, change control, autonomy |
| 3 LLM layer | Done: Gemini, record/replay (n-th repeat recorded separately, prune), scripted, bounded retry, quota-aware errors |
| 4 Agents | Done: Requirements, Impact, Architect (+ plan), Developer, Tester, Docs, Release |
| 5 Tools | Done: verify, scanner, workspace writer, scrubbed runner, git, publish |
| 6 Demo and scenarios | Done: greenfield, brownfield, ambiguous (**recorded on .NET**; ambiguous ends in a bounded failure), greenfield-python (recorded), 8 failure/control demos, `run_demo.py new` (interactive intake + discovery Q&A, .NET by default); unedited output in `docs/evidence/` |
| Stacks | Done: `warden/stacks.py` profiles `dotnet-aspnetcore` (default for new projects; verified with real dotnet build/test) and `python-fastapi` |
| Scope guard | Done: policy `scope` stops anything outside the approved plan for a human |
| 7 Run report | Done: metrics and report from the audit log, `python -m warden.report` |
| 8 Docs | Done: README, ARCHITECTURE, SCENARIOS, TESTING, ENGINEERING_SUMMARY, RECORDING |
| 9 CI | Done: runs on every push to GitHub (lint, types, tests, scripted failure demos); passing |

## 4. Remaining work

1. Run `run_demo.py new` once interactively on the .NET stack for a real-model showcase of the
   discovery Q&A (it records itself).
2. Known small issues, deliberately not fixed yet because either change would alter the recorded
   `harden` path: (a) bundles shown in prompts use ``` fences, and docs that contain ``` close them
   early (one wasted docs call in `harden`); use a longer fence when content contains ```.
   (b) The .NET `EnsureCreated` rule should exempt code guarded by `IsInMemory()`.
3. Optional improvements: container sandbox for verify; deleting files in the generated repo;
   a test-isolation hint for generated .NET tests (the ambiguous run's tests raced on SQLite);
   approval escalation to a second approver (needs new event types: ask first).

Constraint for whoever continues: **changing any agent prompt, schema, input or verify output
format invalidates recordings** for the affected calls. Re-record afterwards.

## 5. Known limitations

See `docs/ENGINEERING_SUMMARY.md#limitations-stated-honestly`.

## 6. Assessment alignment

| Brief requirement | Status | Evidence |
|---|---|---|
| 1 Requirement understanding | Done | Requirements agent; recorded .NET runs; ambiguous scenario asks blocking questions and folds the answers in; `run_demo.py new` discovery Q&A |
| 2 Task decomposition | Done | Architect tasks with owners, dependencies, traceability; .NET greenfield: 13 tasks |
| 3 Brownfield codebase reasoning | Done | C# scanner + grounded impact agent; recorded .NET brownfield run ([evidence](evidence/brownfield/)) |
| 4 DAG, gates, parallel + joins | Done | `dag.py`, orchestrator tests |
| 4 Lineage | Done | `caused_by` + content hashes; report traces the release to every human decision |
| 4 Human approval checkpoints | Done | Graph, policy and agent escalation; fail-closed approver |
| 4 Retry, fallback, rollback, safe-stop | Done | Tests + `run_demo.py failures` |
| 4 Policy: security, compliance, change control | Done | `policies/default.yaml`, `test_policy.py` |
| 4 Observability and metrics | Done | `report.py`: success rate, retry/rollback frequency, MTTR, latency |
| 4 Dynamic re-planning | Done | `replan()`, repair loops, `replan-on-change` demo |
| 5 Engineering output | Done | Generated ASP.NET Core repo: 31/31 xUnit tests, README and docs ([evidence](evidence/greenfield/repo/)) |
| 6 Validation and risk control | Done | Verify gate, policy, risk table in the engineering summary |
| 7 Controlled autonomy | Done | Autonomy per role; release decided by a human |
| 8 Engineering summary | Done | `docs/ENGINEERING_SUMMARY.md` |
| Deliverables: prototype, architecture, 3 scenarios, setup, testing, limitations | Done | `README.md`, `docs/`; all 3 scenarios recorded on .NET (ambiguous ends in a bounded, safe failure) |
