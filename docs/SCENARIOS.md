# Scenarios

Each scenario runs the real orchestrator, policy engine, approval gates and audit log, and each
run writes `runs/<scenario>/report.md`. This document covers how each scenario decomposes the
work, how it is orchestrated, and how the result is validated.

## 1. Greenfield: URL shortener (recorded)

`python run_demo.py greenfield --auto-approve`. The request is in
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

## 2. Brownfield: daily click analytics on the existing app (built; recording pending)

`python run_demo.py brownfield --record --auto-approve` (one-time recording; see
[RECORDING.md](RECORDING.md)). The request is in
[demo/requests/brownfield.md](../demo/requests/brownfield.md). The existing code is the real
greenfield output, frozen in `demo/baselines/url-shortener.json`.

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

## 3. Ambiguous: "make links safer and give marketing better insights" (built; recording pending)

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

## 4. Failure and control demos (scripted, always available)

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
