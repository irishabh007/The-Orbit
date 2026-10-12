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
| Language | Python 3.11+; **standard library only in `warden/` core** (exception: PyYAML for policy files) |
| Machine | Windows, Intel i5, 8 GB RAM: no Docker, no local models |
| LLM | One `LLMClient` interface: Gemini free tier adapter (httpx), **record/replay** backend so reviewers run offline with no key, scripted fallback for failure demos |
| Validation of LLM output | Pydantic schemas, retry on malformed output |
| Rollback/features | git-based, per-feature commits (artifact-level rollback already implemented) |
| Packaging | Plain `python -m venv` + `pip install -r requirements.txt` + one `run_demo.py` (`greenfield`, `brownfield`, `ambiguous`, `all`, `--auto-approve`). Docker optional, last |
| CI | GitHub Actions: fresh Ubuntu install, tests, demos (proof the README works) |
| Claims | Narrow and defensible: "production-grade structure and practices", not "any requirement, fully production-ready" |

## 3. Status

**Done and tested (123 tests):**
- `models.py` states and transition table; `eventlog.py` hash chain + lineage; `artifacts.py` versioning, staleness, restore, drop
- `dag.py`, `agent.py`, `approval.py` (interface + scripted + reject-all)
- `policy.py` + `policies/default.yaml`: YAML engine with secrets (deny, never echoed to the log),
  dependency allowlist (deny), protected paths (escalate to approval), autonomy per agent role
  (`full` / `supervised` / `none`); fails closed on invalid policy files
- `llm/`: `LLMClient` protocol; `ScriptedClient` (answers or exceptions, thread-safe);
  `RecordReplayClient` (one JSON file per request hash; replay misses fail closed, errors never
  recorded); `GeminiClient` (httpx, key in header, scrubbed from errors and repr, transient vs
  permanent errors, no internal retries). Only `llm/gemini.py` may import httpx (test-enforced)
- `agents/`: Requirements, Architect (+ planning), Developer, Tester, Docs, Release reviewer.
  JSON-mode prompts validated by strict Pydantic schemas plus cross-artifact checks (tasks cite real
  requirement ids, every must requirement has a task, no task cycles, role-scoped file paths);
  one repair round with the errors fed back, then `AgentOutputError` -> orchestrator retry.
  `sdlc_graph()`: requirements -> architect (approval) -> developer | tester | docs -> release
  (approval). Blocking clarifying questions escalate to a human (`AgentResult.escalate`; agents can
  only add oversight); answers come back as 'changes' feedback
- `bundle.py`: code/tests/docs travel as one `*.bundle` artifact (safe relative paths only); the
  policy engine expands bundles so secrets/dependency/protected-path rules see every file
- `orchestrator.py`: parallel scheduling with joins, retry -> fallback -> rollback, approvals,
  safe-stop (kill switch + step budget), replan, human rollback with hold/release, `adopt()` for
  brownfield entry, persistence/resume

**Known gaps:** agents never run against a real model yet
(no recordings); generated files are not yet written to disk or executed (item 5); no CLI; no report/metrics; no docs beyond this.

## 4. Remaining work, in priority order

Each item is done only when a test demonstrates it.

1. ~~**Housekeeping.**~~ Done. Run `pytest -q`, `ruff check .`, `mypy warden`; fix findings. Commit.
2. ~~**YAML policy engine**~~ Done. (`warden/policy.py`, `policies/default.yaml`): rules for secrets in
   output, dependency allowlist, protected paths requiring approval, and max autonomy per node kind.
   Pre and post checks; denials are never retried.
3. ~~**LLM layer**~~ Done. (`warden/llm/`): `LLMClient` protocol; Gemini adapter via httpx (key from `.env`);
   record/replay backend keyed by hash of prompt; scripted backend. Never log keys.
4. ~~**Agents**~~ Done (`warden/agents/`): Requirements (vague text -> numbered requirements, acceptance
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
- Policy secret scanning is regex-based: it catches common key formats and literal credential
  assignments, not every possible secret. Dependency checks cover `requirements*.txt` and
  `pyproject.toml` manifests, not imports in code (the AST scanner in item 5 can close that)
- Test suite runs at about 2.1s on the dev laptop (Windows), just over the 2s budget (pydantic import
  ~0.2s, orchestrator state saves ~1.2s); per-file I/O
  (state saves on every transition) dominates. `EventLog` keeps its append handle open, so callers
  must `close()` it (or use `with`) when done
- Recordings are keyed on the request only, not the model: a recording made with one Gemini model
  replays under another (the recording file names the model that produced it)
- LLM calls are not yet in the event log (tokens, latency, backend); deferred to the improvement pass
- Tester writes tests from the design in parallel with the Developer (contract-first). Mismatches are
  only caught when the verify step runs both (item 5), which must route failures back to the developer

## 6. Assessment alignment (update with every item)

Traceability from the brief (paraphrased; the PDF is internal and stays out of the repo)
to this repo. Status: **done** (test-proven), **partial**, **todo**. Note: the brief allows
2-3 days of work; section 1's 24-hour figure was the original plan.

| Brief requirement | Status | Where / evidence | Next |
|---|---|---|---|
| 1. Requirement understanding (intent, ambiguity, normalized problem) | partial | `RequirementsAgent`: numbered requirements, acceptance criteria, assumptions, blocking questions escalate; `test_agents.py` | Real-model recordings; ambiguous scenario (item 6) |
| 2. Task decomposition (tasks, dependencies, sequencing) | done | `dag.py`; `ArchitectAgent` tasks with owners, `depends_on`, requirement traceability, cycle + coverage checks | Shown in run report (item 7) |
| 3. Brownfield codebase reasoning (impacted modules/APIs/data flows) | partial | `Orchestrator.adopt()` mid-graph entry | AST scanner + impact analysis (items 5, 6) |
| 4a. Explicit DAG with entry/exit gates | done | `dag.py`; `test_dag.py`, gate tests in `test_orchestrator.py` | |
| 4b. Sequential + parallel paths with synchronization | done | barrier and join tests in `test_orchestrator.py` | |
| 4c. Cross-stage context and decision lineage | done | artifact input hashes, `caused_by` lineage tests | |
| 4d. Human approval for high-impact actions | done | `approval.py`; node, policy and agent escalation tests | Interactive CLI approver (item 6) |
| 4e. Bounded retries, fallback, rollback, safe-stop | done | `FailureHandlingTests`, `ReplanRollbackAndSafetyTests` | Failure demo (item 6) |
| 4f. Policy guardrails: security | done | secrets + dependency allowlist, `test_policy.py` | Secret scanner tool on files (item 5) |
| 4f. Policy guardrails: change control | done | protected paths + autonomy levels, `test_policy.py` | |
| 4f. Policy guardrails: compliance | **gap** | only indirect (tamper-evident audit log, recorded approvals) | Decide an explicit compliance rule (e.g. PII in code/fixtures, license allowlist, required release sign-off) |
| 4g. Audit-grade observability and traceability | partial | hash-chained `eventlog.py`, `verify()` | Run report with timeline + lineage (item 7); LLM calls in the log (deferred) |
| 4h. Metrics: success rate, retry/rollback frequency, MTTR, end-to-end latency | todo | raw data already in events (`duration_ms`, retries, rollbacks) | Compute in run report (item 7) |
| 4i. Dynamic re-planning on upstream change | done | `replan()`, stale-input discard tests | Ambiguous scenario shows it (item 6) |
| 4j. Controlled agent autonomy | done | autonomy `full`/`supervised`/`none` per role | |
| 5. Production-quality generated code, API/schema, tests, docs | partial | Developer/Tester/Docs agents emit validated bundles; Architect emits the API/schema | Write to disk, run tests + lint, real-model output (items 5-6) |
| 6. Validation and risk control (risks, failure scenarios, guardrails) | partial | gates, policy, fail-closed defaults | Risks/trade-offs write-up (item 8) |
| 7. Humans own oversight, approvals, final quality | done | fail-closed approver; approval on architect + release in the graph; Release agent only recommends | |
| 8. Final engineering summary | todo | | item 8 |
| Deliverable: runnable end-to-end prototype | todo | | `run_demo.py` (item 6), CI proof (item 9) |
| Deliverable: architecture overview | partial | `CLAUDE.md` architecture list | README + architecture doc (item 8) |
| Deliverable: three scenarios with decomposition, orchestration, validation | todo | | item 6 |
| Deliverable: setup instructions | todo | `.env.example` | README quick start (item 8) |
| Deliverable: testing approach, limitations, trade-offs | partial | section 5 above | item 8 |

Supporting infrastructure done so far: LLM layer (`warden/llm/`, offline replay so reviewers
need no key), rule 7 dependency check (`test_dependencies.py`).
