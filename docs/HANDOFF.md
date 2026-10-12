# Warden: project handoff

Put this file at `docs/HANDOFF.md` in the repo. Read `CLAUDE.md` (architecture and rules) first.

## 1. The assignment (technical assessment, 24-hour deadline)

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
| Language | Python 3.11+; **standard library only in `warden/` core** |
| Machine | Windows, Intel i5, 8 GB RAM: no Docker, no local models |
| LLM | One `LLMClient` interface: Gemini free tier adapter (httpx), **record/replay** backend so reviewers run offline with no key, scripted fallback for failure demos |
| Validation of LLM output | Pydantic schemas, retry on malformed output |
| Rollback/features | git-based, per-feature commits (artifact-level rollback already implemented) |
| Packaging | Plain `python -m venv` + `pip install -r requirements.txt` + one `run_demo.py` (`greenfield`, `brownfield`, `ambiguous`, `all`, `--auto-approve`). Docker optional, last |
| CI | GitHub Actions: fresh Ubuntu install, tests, demos (proof the README works) |
| Claims | Narrow and defensible: "production-grade structure and practices", not "any requirement, fully production-ready" |

## 3. Status

**Done and tested (68 tests):**
- `models.py` states and transition table; `eventlog.py` hash chain + lineage; `artifacts.py` versioning, staleness, restore, drop
- `dag.py`, `agent.py`, `policy.py` (interface + allow-all), `approval.py` (interface + scripted + reject-all)
- `orchestrator.py`: parallel scheduling with joins, retry -> fallback -> rollback, approvals,
  safe-stop (kill switch + step budget), replan, human rollback with hold/release, `adopt()` for
  brownfield entry, persistence/resume

**Known gaps:** ruff/mypy have never been run (fix any findings first); no real policy rules; no
LLM layer; no real agents; no CLI; no report/metrics; no docs beyond this.

## 4. Remaining work, in priority order

Each item is done only when a test demonstrates it.

1. **Housekeeping.** Run `pytest -q`, `ruff check .`, `mypy warden`; fix findings. Commit.
2. **YAML policy engine** (`warden/policy.py`, `policies/default.yaml`): rules for secrets in
   output, dependency allowlist, protected paths requiring approval, and max autonomy per node kind.
   Pre and post checks; denials are never retried.
3. **LLM layer** (`warden/llm/`): `LLMClient` protocol; Gemini adapter via httpx (key from `.env`);
   record/replay backend keyed by hash of prompt; scripted backend. Never log keys.
4. **Agents** (`warden/agents/`): Requirements (vague text -> numbered requirements, acceptance
   criteria, assumptions, clarifying questions), Architect (needs approval), Planner, Developer,
   Tester, Docs, Release reviewer (human makes the final call). Strict Pydantic output schemas.
5. **Tools:** sandboxed file writer (workspace only, protected paths), git adapter (commit per
   feature, revert), test runner, ruff/mypy gate, secret scanner, AST codebase scanner (modules,
   endpoints, imports) for brownfield impact analysis.
6. **Demo entry point** `run_demo.py` and the three scenarios:
   - Greenfield: URL shortener (`POST /shorten`, `GET /{code}`, `GET /analytics/{code}`; custom
     alias, expiry, SSRF protection, rate limiting, health endpoint)
   - Brownfield: scan the generated repo, enter mid-graph, add a feature (e.g. link expiry or rate
     limiting), show impact analysis and an approval gate
   - Ambiguous: "make links safer and give marketing better insights" -> clarifying questions ->
     human answers -> replan
   - Failure demos: retry -> fallback -> rollback, a policy block, a safe-stop
7. **Run report** (Markdown/HTML) per scenario: Mermaid DAG, timeline, approvals, lineage, and
   metrics computed from the event log (success rate, retries, rollbacks, MTTR, latency).
8. **Docs:** README with a 5-command quick start at the top, architecture overview, testing
   approach, ADRs for key decisions, limitations (honest), final engineering summary.
9. **CI** workflow, then a fresh-clone test on a clean folder. Docker only if time remains.

If time runs short, protect in this order: DAG orchestrator with gates/parallelism/approvals;
a retry-rollback-replan failure demo; the three scenarios with reports; README and architecture
doc with honest limitations. Merge Planner into Architect before cutting anything else.

## 5. Known limitations to state honestly

- Event log is tamper-evident, not tamper-proof (anchor the head hash externally to close this)
- In-process scheduler, one machine; no distributed workers
- LLM output is nondeterministic; replay makes demos reproducible, not the model
- Re-planning invalidates direct consumers lazily; in-flight results are discarded if inputs change
