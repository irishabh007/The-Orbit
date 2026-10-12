# Warden: a governed, agentic SDLC orchestrator

Warden turns a requirement (well-defined, vague, or a change to an existing codebase) into a
reviewable engineering outcome: a spec, a design and task plan, code, tests and docs, verified
and committed to a normal git repository. Agents do the multi-step work inside defined autonomy
boundaries. Humans own oversight, approvals and final quality.

The orchestrator is the core of the project. It runs an explicit dependency graph with entry and
exit gates, parallel branches with joins, human approval checkpoints, bounded retries, fallback,
rollback, safe-stop, policy guardrails, re-planning when upstream outputs change, and a
hash-chained audit log from which every reported metric is computed.

## Quick start (offline, no API key)

```bash
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -r requirements-demo.txt -r requirements-dev.txt
python run_demo.py greenfield --auto-approve         # recorded real-model run, replayed offline
python run_demo.py failures                          # 8 governance demos: retry, rollback, policy, re-plan...
pytest -q && pytest -q -m slow                       # 257 fast tests + 8 subprocess tests
```

Each run prints a live event feed and writes `runs/<scenario>/report.md`, which includes
reliability metrics, model usage and cost, a Mermaid workflow diagram, a timeline, approvals,
decision lineage and audit verification. Successful scenario runs also publish
`runs/<scenario>/repo`, a plain git repository with no dependency on Warden.

Leave out `--auto-approve` to make every approval decision yourself at the terminal (press **v** at
a gate to see what you are approving). For a guided 10-minute tour, see
[docs/WALKTHROUGH.md](docs/WALKTHROUGH.md).

## What one run produces (recorded greenfield run, gemini-3.8-flash)

| | |
|---|---|
| Input | A one-page brief for a URL shortener |
| Decomposition | 9 requirements with acceptance criteria, 4 clarifying questions (2 blocking), 12 tasks traced to requirements |
| Execution | 7 governed nodes, 14 agent attempts; developer, tester and docs in parallel |
| Recovery | Verify failed twice (3 failing tests → 1 → 0), each time routed back to developer and tester |
| Governance | 6 human decisions, 5 policy escalations, 0 denials; the release decision traces back to all 6 |
| Output | 18 files (~1,800 lines): FastAPI app with SSRF protection, rate limiting, expiry, analytics; 30 passing tests; README and docs |
| Cost | 12 model calls, ~132k input and ~77k output tokens, about $0.39 at list price |
| Audit | Hash-chained log, verified; head hash anchored in the generated repo's git commit |

## Scenarios

| Command | What it shows | Model answers |
|---|---|---|
| `run_demo.py greenfield` | URL shortener from a written brief: 9 requirements, 12 traced tasks, 18 files, 30 generated tests passing after 2 automatic repair loops | **Recorded** from gemini-3.8-flash |
| `run_demo.py brownfield` | Add daily click analytics to the existing app: codebase scan, grounded impact analysis with human sign-off, revision of existing code | Not yet recorded: needs `--record` (see [recording](docs/RECORDING.md)) |
| `run_demo.py ambiguous` | "Make links safer and give marketing better insights": blocking clarifying questions, human answers, requirements re-planned | Not yet recorded: needs `--record` |
| `run_demo.py failures` | Policy block, human rejection, retry → fallback → recovery or rollback, safe-stop, re-plan after a requirement change, human rollback of a shipped feature, resume mid-graph from existing artifacts | Scripted (no model needed) |

Details for each scenario are in [docs/SCENARIOS.md](docs/SCENARIOS.md).

## Model modes

- **Replay** (default): uses `recordings/<scenario>/`. Needs no key and no network, and gives the
  same result every time.
- **`--record`**: calls Gemini only for prompts that have no recording yet, then saves the answers.
- **`--live`**: calls Gemini every time and records nothing.

For `--record` and `--live`, put `GEMINI_API_KEY=...` in `.env` (the file is git-ignored; see
`.env.example`).

## Documentation

| Document | Contents |
|---|---|
| [Walkthrough](docs/WALKTHROUGH.md) | A 10-minute guided tour mapped to the evaluation criteria |
| [Architecture](docs/ARCHITECTURE.md) | Components, orchestration model, control flow, governance, key decisions |
| [Scenarios](docs/SCENARIOS.md) | Decomposition, orchestration and validation for each scenario |
| [Testing](docs/TESTING.md) | Testing approach, how to run it, what each layer proves |
| [Engineering summary](docs/ENGINEERING_SUMMARY.md) | Plan and rationale, artifacts, risks and trade-offs, assumptions, limitations, lessons from real runs |
| [Recording runbook](docs/RECORDING.md) | Recording the remaining scenarios with a paid key |

## Repository layout

```
warden/            core (standard library + PyYAML): orchestrator, graph, state machine, audit log,
                   artifact store, policy engine, report; adapters: llm/ (httpx), agents/ (pydantic)
warden/tools/      workspace writer, scrubbed command runner, verify node, codebase scanner, git
demo/              scenarios, console approver, failure demos, brownfield baseline
policies/          default.yaml: security, compliance, change control, autonomy per role
recordings/        recorded model answers for offline replay
tests/             unit, integration and end-to-end tests (`-m slow`: real subprocesses)
```

Requires Python 3.11+. Developed on Windows 11, and the tools are cross-platform.
