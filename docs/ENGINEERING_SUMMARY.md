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
| Recorded real-model runs | `recordings/`: greenfield, brownfield, ambiguous (.NET; 14, 10 and 23 calls) and greenfield-python (12), gemini-3.8-flash |
| Generated URL shortener (per run) | `runs/greenfield/repo`: ASP.NET Core + xUnit, 23 files, ~2,000 lines, 31 passing tests |
| Committed evidence | `docs/evidence/`: reports, audit logs, specs, designs, the generated repo, the brownfield diff |
| Per-run evidence | `runs/<scenario>/events.jsonl`, `report.md`, `metrics.json`, `spec.json`, `design.json`, `verify_report.json`, `release_review.json` |
| Tests | `tests/`: 325 fast + 11 slow (including real dotnet build/test) |
| Docs | `README.md`, `docs/` |

## Risks, trade-offs and how they are validated

| Risk | Mitigation | Validation |
|---|---|---|
| Model output is malformed or invented | Strict schemas (extra fields forbidden); cross-artifact checks (tasks cite real requirements, impact cites real modules); one repair round, then bounded retries | `test_agents.py`; real runs: invalid JSON caught by the schema check and retried, C# syntax errors caught by the build gate |
| Generated code does not work | Verify runs the generated tests and lint; failures go back to both authors with role-specific guidance | Real .NET runs: 3 → 3 → 27 → 30 → 31 of 31 over 4 repair loops (greenfield); build error → 50/50 (brownfield); the ambiguous run exhausted its budget and stopped with nothing released |
| Secrets or personal data leak into code or logs | Policy denies; reasons never echo the value; the child environment is scrubbed of keys; keys are never logged | Tests assert the secret is absent from the log and report; the child process cannot see the key |
| Generated docs claim what the code does not do | **Open** (found by an external review; see below) | Planned: docs from code, a consistency reviewer before release |
| Agents exceed their remit | Autonomy per role; protected paths need a human; agents can only add oversight; undeclared outputs are rejected | Policy and orchestrator tests; failure demos |
| A human decision is not traceable | Approvals are linked into `caused_by` lineage; the report traces the release back to every human decision | Real run: the release lineage reaches all 13 human decisions (122 linked events) |
| Audit log is tampered with | Hash chain (`verify()` locates the break) plus the head hash anchored in each git commit | `test_eventlog.py` forges a full rewrite that `verify()` accepts and the anchor rejects |
| Runaway cost or loops | Every loop is bounded; quota errors are not retried; step budget triggers safe-stop | Tests per bound; safe-stop demo |
| Non-reproducible demos | Record/replay with request-hash keys; normalized tool output; no timings in agent-visible artifacts; closest-match fallback for flaky generated tests | Two identical offline replays per scenario; slow replay test; `ReplayDriftTests` |

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

- The reviewer runs the demos from recordings, with no key. Live runs need a Gemini key; a .NET
  scenario costs about $0.60-1.80 at list price (see the evidence table).
- Generated projects target .NET 10 (ASP.NET Core minimal APIs, EF Core + SQLite, xUnit) by default,
  or Python 3.11 + FastAPI. The stack is a profile (prompts, file rules, verify commands, scanner),
  not hard-coded in the orchestrator.
- Generated projects only use packages on the policy allowlist, all installed in the verify
  interpreter (enforced by a test).
- "Human" approvals in auto-approve demos are scripted and labelled as such in the log
  (`human:auto-approve`, `product-owner (scripted)`).

## Limitations (stated honestly)

- **The ambiguous scenario ends FAILED:** its broader spec needed more repair than the 6-loop
  budget allowed (best 18/26, final 0/26 after a regression in the tests' host setup). The controls
  held and nothing was released; the run is kept as recorded rather than re-rolled until it passed.
- **Generated tests can be flaky:** the ambiguous run's tests raced on a shared SQLite database.
  Replay copes (closest-match fallback), but the generated code itself would need a human fix; a
  test-isolation hint in the tester prompt is the obvious next step (it would invalidate the
  recordings, so it was not made now).
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
- **No file deletion:** agents can add and overwrite files in the generated repo, but cannot
  delete them.
- **Contract mismatches:** when tests and code disagree, verify sends both back. A genuinely wrong
  test can use up the repair loops and halt the run for a human.
- **Cost is an estimate:** every model call's tokens are logged and priced at list price
  (`warden/llm/pricing.py`). The `greenfield-python` recordings predate thinking-token capture, so
  their reported cost (~$0.39) understates the real bill; the .NET recordings include thinking tokens.

## External review: findings and responses

An external review of the submission found six issues. Each was verified against the code.

| Finding | Status | Response |
|---|---|---|
| Generated `architecture.md` says click counts update "atomically"; `LinkService.cs` does `ClickCount += 1` (read, then write), so concurrent redirects can lose counts | **Open** | Kept in the evidence as generated. See the plan below |
| Brownfield docs describe a "purely additive" schema migration for the new table, but there is no migration: `EnsureCreatedAsync()` never adds a table to an existing database. Tests use fresh databases, so they pass, and the release reviewer approved | **Open** | Same plan; plus a brownfield verify that upgrades an existing database |
| The docs agent works from the design only and never sees the code; no gate checks docs against code | **Open** | Planned: docs agent consumes the code and tests; a consistency-reviewer node before release whose blocking findings go back to developer and docs as feedback (bounded like the repair loop); .NET prompt guidance (EF Core migrations, `ExecuteUpdateAsync` for counters). It changes prompts and the graph, so the recorded scenarios must be re-recorded |
| Approvals block the scheduler; no timeout | **Fixed** | `decide()` runs on an approval thread; the decision is applied on the scheduler thread; `Config.approval_timeout_s` fails the gate closed. Escalation to a second approver is not built yet (it needs new event types) |
| Approver identity is a string; no roles; one person can approve everything | **Fixed (authorization)** | Policy `approvals`: roles per gate and separation of duties, enforced on every approval and kept across resume; console identity is the OS login. Authentication stays with the host (SSO, code review system) |
| "Changes requested" feedback is lost on resume | **Fixed** | Pending feedback is saved in the state file and restored |

The open finding is the important one: Warden verifies that generated code *works* (builds, tests
pass), but nothing yet verifies that what the generated docs and design *claim* is true of the code.
Passing tests on a fresh database also hid a missing schema migration, which a human reviewer
would catch only by reading the code.

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
| 16 | A failed call (HTTP 503) used up a replay slot, so the retry's answer landed in the next slot and every rerun paid again | A slot is claimed only by a successful answer |
| 17 | `dotnet test -v q` printed no failure messages, so agents repaired blind (4,000 characters of noise) | TRX results turned into a digest grouped by root cause (a few lines) |
| 18 | Flaky generated tests changed a repair prompt by one line, so replays missed and reruns paid again | Closest-match replay for the same agent (≥ 99% alike), marked `replay-nearest` |
| 19 | A billing denial (HTTP 403) was retried by every agent until exhausted | Requests the model service refuses (4xx) are permanent: the run stops at once with the reason |

## How this maps to the brief

| Brief requirement | Where |
|---|---|
| 1 Requirement understanding | Requirements agent; ambiguous scenario; `run_demo.py new` discovery Q&A |
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
