# Scenarios

Each scenario runs the real orchestrator, policy engine, approval gates and audit log, and each
run writes `runs/<scenario>/report.md`. This document covers how each scenario decomposes the
work, how it is orchestrated, and how the result is validated.

## 1. Greenfield: URL shortener (recorded)

`python run_demo.py greenfield --auto-approve` replays the recorded .NET run (ASP.NET Core minimal API, EF Core + SQLite, xUnit;
31 tests, passing after 4 repair loops: 3 -> 3 -> 27 -> 30 -> 31);
`python run_demo.py greenfield-python --auto-approve` replays the recorded run below, made on the Python
stack with the same brief. The request is in
[demo/requests/greenfield.md](../demo/requests/greenfield.md): shorten, redirect, analytics, health
check, custom aliases, expiry, SSRF protection, rate limiting, persistence.

**Decomposition** (from the recorded gemini-3.8-flash run)
- Requirements: 9 numbered requirements (8 must, 1 should), each with acceptance criteria, plus
  assumptions and 4 clarifying questions (2 blocking, each with a proposed default).
- Architect: 8 components, 4 endpoints, 4 decisions with rejected alternatives, 3 risks, and
  **12 tasks** with owners and dependencies, each traced to requirement IDs. Every must
  requirement is checked to have a task, and dependency cycles are rejected.

**Orchestration**
- Requirements escalated to a human because of the blocking questions. With auto-approve, the
  proposed defaults were accepted.
- Architect required approval (graph) and was escalated by policy (`supervised`).
- Developer, tester and docs ran **in parallel** from the approved design. The developer was
  escalated because it writes `requirements.txt`, a protected path.
- Verify ran the generated tests and lint. It **failed twice**, and each failure went back to both
  developer and tester as feedback:
  - Repair loop 1: 3 failing tests and 5 lint errors.
  - Repair loop 2: 1 failing test.
  - The developer's next revision had a syntax error, which the agent's own check caught. A
    retry, still carrying the feedback, fixed it. Verify then passed.
- Release recommended "ready", and a human made the final call.

**Validation:** 30 generated tests pass, ruff is clean, and the release checklist cites evidence
for every must requirement. The report shows 14 agent executions, 2 repair loops, 2 incidents
recovered (MTTR is computed from the log), and 6 human decisions in the lineage of the release.

**Output:** `runs/greenfield/repo` contains an `app/` package (config, database, service,
security/SSRF, rate limiter, JSON logging), 4 test modules, a README and `docs/`, about 1,800 lines
in one commit whose message lists the requirements and approvals.

## 2. Brownfield: daily click analytics on the existing app (recorded)

`python run_demo.py brownfield --auto-approve` replays the recorded .NET run: one repair loop
(a build error, fixed), then release. To re-record, see [RECORDING.md](RECORDING.md). The request is in
[demo/requests/brownfield.md](../demo/requests/brownfield.md). The existing code is the real
greenfield output, frozen in `demo/baselines/url-shortener-dotnet.json`.

**Decomposition**
- `scan` (static analysis with `ast`, nothing executes) builds `codebase.json`: modules, the
  import graph and reverse dependencies (blast radius), endpoints from route decorators, and which
  tests cover which modules.
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

## 5. Failure and control demos (scripted, always available)

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
