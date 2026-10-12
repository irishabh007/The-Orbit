# Engineering summary

## Plan and rationale

The brief calls workflow orchestration the critical differentiator, so effort went to the
orchestrator first and to generated output second. Work was built bottom-up, with each layer
test-proven before the next:

1. **Governed core:** a state machine with a legal transition table, a hash-chained audit log, a
   content-hashed artifact store, and a validated graph. These make "every decision is visible and
   enforced" a structural property, not a convention.
2. **Scheduler:** parallel branches with joins; staged outputs committed only after gates, policy
   and approval; bounded retry → fallback → rollback; safe-stop; re-planning from content hashes;
   resume after a crash.
3. **Guardrails:** a YAML policy engine (security, compliance, change control, autonomy per role)
   that fails closed.
4. **Model layer:** one interface; record/replay so reviewers run offline; failures classified as
   transient, quota or permanent.
5. **Agents and tools:** strict schemas with grounding checks, verify by actually running the
   generated tests, a codebase scanner for brownfield, publication to plain git.
6. **Scenarios, report, docs:** each built on the previous layers.

Two principles shaped most decisions:
- **Agents propose, the orchestrator disposes.** Agents return staged outputs; nothing reaches the
  store or the repo until the gates, policy and humans allow it. A discarded attempt has nothing to
  undo.
- **Fail closed and bounded.** The default approver rejects. Malformed policy refuses to load.
  Every loop (model retries, repair rounds, node retries, repair loops, step budget) has a limit.
  Denials and rejections are never retried.

## Artifacts

| Artifact | Where |
|---|---|
| Orchestrator and governance core | `warden/` (~3,600 lines, standard library + PyYAML) |
| Agents (7 roles), LLM adapters, tools | `warden/agents`, `warden/llm`, `warden/tools` |
| Policy | `policies/default.yaml` |
| Demo runner, scenarios, failure demos | `run_demo.py`, `demo/` |
| Recorded real-model run (greenfield) | `recordings/greenfield` (12 calls, gemini-3.8-flash) |
| Generated URL shortener (per run) | `runs/greenfield/repo`: 18 files, ~1,800 lines, 30 passing tests |
| Per-run evidence | `runs/<scenario>/events.jsonl`, `report.md`, `metrics.json`, `spec.json`, `design.json`, `verify_report.json`, `release_review.json` |
| Tests | `tests/`: 297 fast + 11 slow (including real dotnet build/test) |
| Docs | `README.md`, `docs/` |

## Risks, trade-offs and how they are validated

| Risk | Mitigation | Validation |
|---|---|---|
| Model output is malformed or invented | Strict schemas (extra fields forbidden); cross-artifact checks (tasks cite real requirements, impact cites real modules); one repair round, then bounded retries | `test_agents.py`; real run: a syntax error caught inside the agent |
| Generated code does not work | Verify runs the generated tests and lint; failures go back to both authors with role-specific guidance | Real run: 3 failing → 1 → 0 over 2 repair loops |
| Secrets or personal data leak into code or logs | Policy denies; reasons never echo the value; the child environment is scrubbed of keys; keys are never logged | Tests assert the secret is absent from the log and report; the child process cannot see the key |
| Agents exceed their remit | Autonomy per role; protected paths need a human; agents can only add oversight; undeclared outputs are rejected | Policy and orchestrator tests; failure demos |
| A human decision is not traceable | Approvals are linked into `caused_by` lineage; the report traces the release back to every human decision | Real run: the release lineage reaches all 6 human decisions |
| Audit log is tampered with | Hash chain (`verify()` locates the break) plus the head hash anchored in each git commit | `test_eventlog.py` forges a full rewrite that `verify()` accepts and the anchor rejects |
| Runaway cost or loops | Every loop is bounded; quota errors are not retried; step budget triggers safe-stop | Tests per bound; safe-stop demo |
| Non-reproducible demos | Record/replay with request-hash keys; normalized tool output; no timings in agent-visible artifacts | Two identical offline replays; slow replay test |

**Trade-offs made deliberately**

- **Own orchestrator** instead of a workflow framework: more code to own, but every governance
  semantic is explicit and tested.
- **In-process threads**, not distributed workers: simple and sufficient for one machine. Scaling
  out would need a durable queue and a distributed lock.
- **Contract-first parallelism** (the tester works from the design, not the code): faster and an
  honest test of the contract, at the cost of repair loops when the code and tests disagree.
- **Replay for demos:** reproducible and free, but it shows recorded model behaviour, not the live
  model.

## Assumptions

- The reviewer runs the demos offline from recordings. Live runs need a Gemini key (free tier is
  enough for one scenario per day).
- The URL shortener targets Python 3.11, FastAPI and SQLite. The stack is a prompt constraint, not
  hard-coded in the orchestrator.
- Generated projects only use packages on the policy allowlist, all installed in the verify
  interpreter (enforced by a test).
- "Human" approvals in auto-approve demos are scripted and labelled as such in the log
  (`human:auto-approve`, `product-owner (scripted)`).

## Limitations (stated honestly)

- **Recordings:** only greenfield is recorded with the real model. Brownfield and ambiguous are
  built and tested with scripted answers and need one recording session
  ([RECORDING.md](RECORDING.md)).
- **Anchoring is per published commit:** each feature commit records the audit-log head hash
  (`Warden-Audit-Head` trailer), so a later full rewrite of the log is detected. Events written
  after the last commit, and runs that publish nothing (for example a failed run), are protected
  only by the hash chain until something anchors them.
- **Not a sandbox:** generated code runs with the user's permissions (scrubbed environment and a
  timeout; no container on the development machine).
- **Single machine:** one process, one scheduler thread; no distributed workers.
- **Nondeterministic model:** replay makes demos reproducible, not the model. Recordings are keyed
  by request, not by model.
- **Regex-based scanning:** secret and personal-data detection catches common formats, not
  everything. Dependency checks read manifests, not imports in code.
- **C# scanning is pattern-based:** the brownfield scanner for C# finds routes, types and
  `using` dependencies with regular expressions (no C# parser in the standard library). Good
  for grounding impact analysis; not a compiler.
- **.NET scenarios need recording:** the .NET stack is verified end to end with scripted answers
  and real `dotnet build`/`dotnet test`; real-model .NET runs need the paid-key recording session.
- **No file deletion:** agents can add and overwrite files in the generated repo, but cannot
  delete them.
- **Contract mismatches:** when tests and code disagree, verify sends both back. A genuinely wrong
  test can use up the repair loops and halt the run for a human.
- **Cost is an estimate:** every model call's tokens are logged and priced at list price
  (`warden/llm/pricing.py`). The committed greenfield recordings predate thinking-token capture, so
  their reported cost (~$0.39) understates the real bill; new recordings include thinking tokens.

## Lessons from real runs

Scripted tests proved the orchestration logic; the real model, the real operating system and the
real .NET toolchain found the rest. Each defect below was fixed with a regression test.

| # | Found | Fix |
|---|---|---|
| 1 | Windows refused `os.replace` while antivirus or the indexer held the state file | Atomic write with bounded retry (`fsutil.py`) |
| 2 | Generated code used an allowed package missing from the verify interpreter (28 test errors) | Demo requirements must cover the allowlist; a test enforces it |
| 3 | Test failures went only to the developer, who may not edit tests | Verify failures go to developer **and** tester, each with guidance |
| 4 | Repairs regenerated whole files from scratch | Agents see and revise their own previous output |
| 5 | Ruff's unpinned defaults changed what "lint passes" meant | Verify lint pinned to correctness rules (E4, E7, E9, F) |
| 6 | A syntax error used up a whole repair loop | `compile()` check inside the agent costs one call, not a loop |
| 7 | Feedback was consumed at launch, so a retry ran without it | Feedback kept until the node succeeds |
| 8 | A retry replayed the same failed recording | The n-th identical request has its own recording |
| 9 | Read-only git objects broke cleanup, and the run marker was deleted first | Make-writable retry; marker removed last |
| 10 | A daily-quota 429 ("retry in 11h") was retried as transient | Quota errors are permanent; `PermanentAgentError` is never retried |
| 11 | `duration_ms` in the verify report changed the release prompt, breaking replay | No timings in artifacts that agents read |
| 12 | Approval decisions were logged but not linked into lineage | The decision becomes the node's latest cause |
| 13 | `gemini-2.5-flash` was retired for new accounts | Configurable model; default `gemini-3.8-flash` |
| 14 | NuGet failed ("Value cannot be null, path1") under the scrubbed environment | Keep the system-folder variables the .NET SDK needs (paths, not secrets) |
| 15 | `dotnet test` exits 0 when nothing was built or run | Verify checks can require evidence; .NET requires `Total: N > 0` |

## How this maps to the brief

| Brief requirement | Where |
|---|---|
| 1 Requirement understanding | Requirements agent; ambiguous scenario |
| 2 Task decomposition | Architect tasks with owners, dependencies, traceability; validated |
| 3 Codebase reasoning | Scanner + grounded impact agent; brownfield scenario |
| 4 Orchestration | `orchestrator.py`, `dag.py`, `models.py`; [ARCHITECTURE.md](ARCHITECTURE.md) |
| 4 Policy (security, compliance, change control) | `policy.py`, `policies/default.yaml` |
| 4 Observability, metrics | `eventlog.py`, `report.py` (success rate, retry/rollback, MTTR, latency) |
| 4 Dynamic re-planning | `replan()`, repair loops, `replan-on-change` demo |
| 5 Engineering output | Generated repo: code, API, tests, docs |
| 6 Validation and risk control | Gates, verify, policy, this document |
| 7 Controlled autonomy | Autonomy per role, approvals, fail-closed approver, release decided by a human |
| 8 Engineering summary | This document |
