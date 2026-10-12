# Testing approach

## How to run

```bash
pytest -q              # 297 fast tests, deterministic (budget: <5s on an idle machine)
pytest -q -m slow      # 11 tests with real subprocesses: pytest, ruff, git, dotnet build/test, report CLI, recorded replay
ruff check .           # lint
mypy warden demo run_demo.py
```

All four must pass before every commit (CLAUDE.md).

## Layers

| Layer | What it proves | Examples |
|---|---|---|
| Unit | Each building block on its own | Transition table, hash chain and tamper detection, artifact lineage and staleness, graph validation, policy rules, bundle path safety, scanner, metrics math with an exact fake clock |
| Orchestrator integration | Governance behaviour with scripted agents | Parallel branches really run concurrently (barrier); joins; retry → fallback → rollback; approvals fail closed; policy denials and rejections never retried; repair loops bounded; safe-stop; re-plan precision; crash recovery; feedback kept across retries; lineage reaches human decisions |
| Agents | Strict outputs and grounding | Schema violations repaired once then rejected; tasks must cite real requirements; impact cannot modify modules that do not exist; role-scoped file paths; syntax errors caught in the agent |
| Pipeline | All agents through the real orchestrator and default policy | Greenfield end to end; ambiguous Q&A re-plan; failed verification routes to developer and tester |
| Scenarios and demos | What a reviewer runs | Greenfield, brownfield, ambiguous (scripted answers); all six failure/control demos; reports written |
| Slow (real processes) | Tools work for real | Generated tests run under pytest, git commit and revert, scrubbed environment hides keys from child processes, **recorded greenfield replays offline to a verified release** |
| Architecture rules | The working agreement is enforced | Core imports only stdlib + PyYAML; adapters are the only third-party users; the verify interpreter has every package the policy allows |

## Why scripted and recorded answers

Model output is nondeterministic, so tests use `ScriptedClient` (fixed answers or errors) to
prove orchestration behaviour exactly. Real-model behaviour is captured once with `--record` and
replayed. The replay is deterministic because every prompt is a pure function of its inputs
(timings and temp paths are normalized out). The n-th repeat of an identical request has its own
recording, so retries replay the same sequence.

## Bugs found by real runs (each now covered by a test)

Running against the real model exposed defects that scripted tests had missed. Each was fixed
with a regression test; the list is in [ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md#lessons-from-real-runs).
That is also why the recorded greenfield replay is part of the test suite.

## Not covered

- Live-model quality in CI (deliberately: cost and flakiness). It is covered by recordings and the
  replay test.
- Brownfield and ambiguous with the real model until they are recorded.
- Load and concurrency beyond a single machine (out of scope; see limitations).
