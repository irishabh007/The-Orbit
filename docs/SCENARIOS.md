# Scenarios

Each scenario runs the real orchestrator, policy engine, approval gates and audit log, and each
run writes `runs/<scenario>/report.md`. This document covers how each scenario decomposes the
work, how it is orchestrated, and how the result is validated.

## 1. Greenfield: URL shortener (recorded)

`python run_demo.py greenfield --auto-approve` replays the recorded .NET run. The request is in
[demo/requests/greenfield.md](../demo/requests/greenfield.md): shorten, redirect, analytics, health
check, custom aliases, expiry, SSRF protection, rate limiting, persistence. Unedited output:
[evidence/greenfield/](evidence/greenfield/).

**Decomposition** (recorded gemini-3.8-flash run)
- Requirements: 10 numbered requirements (all must), each with acceptance criteria, plus
  assumptions and 4 clarifying questions (3 blocking, each with a proposed default).
- Architect: 14 components, 4 endpoints, 4 decisions with rejected alternatives, 3 risks, and
  **13 tasks** with owners and dependencies, each traced to requirement IDs. Every must
  requirement is checked to have a task, and dependency cycles are rejected.

**Orchestration**
- Requirements escalated to a human because of the blocking questions. With auto-approve, the
  proposed defaults were accepted.
- Architect required approval (graph) and was escalated by policy (`supervised`).
- Developer, tester and docs ran **in parallel** from the approved design (contract-first: the
  tester works from the design, not the code). Developer and tester were escalated on every
  attempt because they write `*.csproj` and `appsettings*.json`, protected paths.
- Verify ran `dotnet build` and `dotnet test`. Failures are turned into a short digest grouped by
  root cause (from the TRX results), which goes back to both developer and tester:
  - 3/31 passing: 28 tests failed on one cause (the test host's fixture constructor).
  - 3/31: the fix introduced a second constructor, which xUnit rejects.
  - 27/31: tests shared one SQLite database ("table already exists").
  - 30/31: one persistence test.
  - 31/31: verify passed after 4 repair loops (the budget is 4).
- Release recommended "ready", and a human made the final call.

**Validation:** 31 xUnit tests pass, and the release checklist cites evidence for every must
requirement. The report shows 19 agent executions, 4 repair loops, 12 policy escalations, and 13
human decisions in the lineage of the release.

**Output:** `runs/greenfield/repo`: `src/App` (minimal API endpoints, EF Core context, link,
code-generator and URL-validation services, rate limiting) and `tests/` (xUnit with
`WebApplicationFactory`), a README and `docs/`; 23 files, about 2,000 lines, in one commit whose
message lists the requirements and approvals. It passes `dotnet test tests` on its own.

**Same brief on the Python stack:** `python run_demo.py greenfield-python --auto-approve` replays an
earlier recorded run (FastAPI + sqlite3 + pytest): 9 requirements, 12 tasks, 30 generated tests
passing after 2 repair loops, 18 files. It shows the stack is a pluggable profile, not hard-coded.

## 2. Brownfield: daily click analytics on the existing app (recorded)

`python run_demo.py brownfield --auto-approve` replays the recorded .NET run: one repair loop
(a build error, fixed), then release. To re-record, see [RECORDING.md](RECORDING.md). The request is in
[demo/requests/brownfield.md](../demo/requests/brownfield.md). The existing code is the real
greenfield output, frozen in `demo/baselines/url-shortener-dotnet.json`.

**Decomposition**
- `scan` (static analysis, nothing executes) builds `codebase.json` from the C# sources: files,
  types, `using` dependencies and reverse dependencies (blast radius), endpoints from `Map*` route
  calls, and which tests cover which files. (The Python stack uses `ast` for the same map.)
- `impact` produces `impact.json`: affected modules, endpoints, data, tests and docs; a risk level;
  regression tests. It is **grounded**: claiming to modify a module, endpoint or test that is not in
  `codebase.json` is rejected.
- The Architect plans against the spec, the impact analysis and the codebase map.

**Orchestration**
- A human approves the impact analysis (graph + policy) before any design work.
- Developer, tester and docs each **revise the existing files** they own; their prompts include
  the current code.
- The generated repo's history is "import existing codebase" followed by the feature commit, so
  the diff is reviewable.

**Validation:** verify runs the existing **and** new tests together, so a regression fails the
gate. Release reviews against the spec and the impact analysis. Tested end to end with scripted
answers (`tests/test_demo.py::BrownfieldScenarioTests`).

**Recorded outcome:** 8 requirements, an impact analysis signed off by a human, 8 tasks. The first
build failed on a C# syntax error; one repair loop fixed it and verify passed **50 of 50** tests
(the existing suite, revised where the feature changed behaviour, plus the new tests). Released; the diff against the existing app is in
[evidence/brownfield/change.diff](evidence/brownfield/change.diff).

## 3. Ambiguous: "make links safer and give marketing better insights" (recorded; ends FAILED)

`python run_demo.py ambiguous` (interactive) or `--auto-approve` (scripted product-owner answers
from [demo/answers/ambiguous.md](../demo/answers/ambiguous.md)).

**Decomposition:** the Requirements agent must turn the one-liner into requirements, record its
assumptions, and ask clarifying questions with a proposed default for each. It marks a question
blocking only when a wrong guess would change the API, the data model or the security posture.

**Orchestration**
- For a vague request the spec **always** needs human sign-off.
- The human answers through "changes requested".
- The answers become feedback, and the requirements agent re-plans, folding them in without
  asking again.
- Everything downstream is planned from the revised spec.

**Validation:** the same verify and release gates as greenfield. Tested with scripted answers
(`tests/test_demo.py::AmbiguousScenarioTests`): the second requirements prompt contains the
human's answers, and the committed spec reflects them.

**Recorded outcome: a safe failure.** Discovery, spec approval and design succeed; the much broader
spec (SSRF protection, rate limiting, analytics) then needs more repair than the budget allows.
Verify across the 6 repair loops (vague briefs get 6, others 4): build error -> 1/26 -> build
error -> 1/26 -> 18/26 -> build error -> 0/26, the last a regression in the tests' host setup
(a configuration key added twice). The loop budget holds, the run stops `FAILED`, release never
runs and nothing is published; the audit log and report show every attempt and its feedback.
The generated tests are also flaky (a SQLite race), so a replay can see slightly different test
output than the recording: replay then serves the closest recorded answer for that agent and
marks it `replay-nearest` (2 calls in this run). We kept this run as recorded rather than
re-rolling it until it passed.

## 4. New project: your own idea, interactively (.NET by default)

`python run_demo.py new` (or `--request-file brief.md --name "Link shortener"`). Needs a Gemini key;
the session is recorded to `recordings/<project>/` so it can be replayed.

1. **Intake:** type the idea; it can be as vague as "a link shortener for marketing".
2. **Discovery:** each clarifying question is asked on its own, with why it matters and a proposed
   default (Enter accepts it), plus free-form notes. The requirements agent revises the spec with
   the answers (up to 3 drafts).
3. **Spec approval:** a readable spec (requirements with acceptance criteria, assumptions, out of
   scope). Approving **locks** it: a later change re-runs requirements and needs approval again.
4. **Plan approval:** a readable design (components, endpoints, decisions, tasks traced to
   requirements).
5. **Build:** developer, tester and docs work in parallel on the .NET stack. Verify runs
   `dotnet build` and `dotnet test`, and failures route back as in greenfield.
6. **Guardrails:** project files and settings are protected paths, and NuGet packages are
   allowlisted. The **scope** rule stops anything outside the approved plan for your decision.
7. **Release:** your final call. Then one git commit with the audit-head anchor.

## 5. Harden: Warden fixes its own production defects (recorded)

`python run_demo.py harden --auto-approve`. The request is in
[demo/requests/harden.md](../demo/requests/harden.md); the existing app is the released brownfield
output (`demo/baselines/url-shortener-dotnet-analytics.json`). Unedited output:
[evidence/harden/](evidence/harden/).

An external review found that the recorded output loses click counts under concurrency and never
migrates an existing database. This scenario asks Warden to fix both, and runs with the
**consistency review** (docs from the verified code; reviewer agent plus deterministic stack rules).

**Decomposition:** 8 requirements (atomic lifetime counter, atomic daily upsert, EF Core migrations,
legacy upgrade path, concurrency test, upgrade test, unchanged API), an impact analysis signed off by
a human, 9 tasks.

**Orchestration:**
- Verify: build error -> 49/52 (the new legacy-upgrade test caught a missing column) -> 52/52.
- Docs written from the verified code.
- Consistency review: **2 blocking findings, both from the deterministic rules, after all 52 tests had
  passed**: an `EnsureCreated` still in the startup path and a read-then-write daily counter. Sent to
  the developer only (targeted repair); verify, docs and review re-ran; the second review found
  nothing.
- Release: "ready", all 7 must requirements pass; a human makes the final call.

**Result:** atomic counters (`ExecuteUpdateAsync`, `INSERT ... ON CONFLICT DO UPDATE`), an EF Core
migration applied at startup with an upgrade path for databases created by `EnsureCreated`, a
100-way concurrent-redirect test and a first-release-database upgrade test. The generated repo
passes 52/52 with plain `dotnet test`. 19 model calls, about $1.74 at list price.

## 6. Failure and control demos (scripted, always available)

`python run_demo.py failures [--case NAME]`. Model answers are scripted and labelled as such.
Everything else is real.

| Case | Shows |
|---|---|
| `policy-block` | Hard-coded AWS key in generated code → post-run policy DENY, no retry, nothing committed, key never in the log or report |
| `human-reject` | Reviewer rejects the architecture → fail closed, nothing downstream runs |
| `retry-fallback-recover` | Primary model returns 503 → bounded retries → fallback agent recovers the run |
| `retry-fallback-rollback` | Fallback fails too → node rolled back, run halted, nothing committed |
| `safe-stop` | Step budget exhausted → in-flight work discarded, SAFE_STOPPED |
| `replan-on-change` | Finished run, then the request changes → only nodes reading a changed input re-run; verify is skipped because code and tests came out byte-identical |
| `human-rollback` | After release a reviewer rolls back the developer's work → code reverted, verify and release invalidated, node **held** (a run while held is BLOCKED) → reviewer releases → feature rebuilt, re-verified, re-approved |
| `resume-midway` | Approved spec and design already exist → `adopt()` marks requirements and architect done → the run starts at the developer (start from any point) |
