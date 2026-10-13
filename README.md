# Warden: a governed, agentic SDLC orchestrator

Warden turns a requirement (well-defined, vague, or a change to an existing codebase) into a
reviewable engineering outcome: a spec, a design and task plan, code, tests and docs, verified
and committed to a normal git repository. Agents do the multi-step work inside defined autonomy
boundaries. Humans own oversight, approvals and final quality.

The orchestrator is the core of the project. It runs an explicit dependency graph with entry and
exit gates, parallel branches with joins, human approval checkpoints, bounded retries, fallback,
rollback, safe-stop, policy guardrails, re-planning when upstream outputs change, and a
hash-chained audit log from which every reported metric is computed.

> The GitHub repository is named **The-Orbit**; the project inside it is **Warden**.

## Quick start (no API key needed)

The scenarios replay recorded model answers, so no API key or model calls are needed.
**Requirements:** Python 3.11+. The .NET scenarios also need the
[.NET 10 SDK](https://dotnet.microsoft.com/download/dotnet/10.0) and, on their first run, network
access so `dotnet` can restore NuGet packages. Without .NET, start with the Python stack.
On Windows, clone to a short path (for example `C:\src\The-Orbit`): run outputs nest deeply and can
exceed the 260-character path limit from a very deep folder (or pass `--out C:/runs`).

```bash
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -r requirements-demo.txt -r requirements-dev.txt
python run_demo.py failures                          # 8 governance demos (scripted, no model): retry, rollback, policy, re-plan...
python run_demo.py greenfield-python --auto-approve  # recorded run on the Python stack; needs only Python
python run_demo.py greenfield --auto-approve         # recorded .NET run (needs the .NET 10 SDK)
python run_demo.py new                               # your own idea -> Q&A -> approved spec -> .NET API (needs a Gemini key)
pytest -q && pytest -q -m slow                       # 325 fast tests + 11 subprocess tests
```

**Recorded vs scripted.** `greenfield`, `brownfield`, `ambiguous` (.NET) and `greenfield-python`
replay real gemini-3.8-flash sessions; the verify step really builds and tests the generated code
each time. `failures` uses scripted agents so each failure path is shown deterministically.
`new` calls the model live. `--auto-approve` stands in for the human at every gate; leave it out to
decide yourself.

Each run prints a live event feed and writes `runs/<scenario>/report.md`, which includes
reliability metrics, model usage and cost, a Mermaid workflow diagram, a timeline, approvals,
decision lineage and audit verification. Successful scenario runs also publish
`runs/<scenario>/repo`, a plain git repository with no dependency on Warden.

Leave out `--auto-approve` to make every approval decision yourself at the terminal (press **v** at
a gate to see what you are approving). For a guided 10-minute tour, see
[docs/WALKTHROUGH.md](docs/WALKTHROUGH.md).

## What one run produces (recorded .NET `greenfield` run, gemini-3.8-flash)

| | |
|---|---|
| Input | A one-page brief for a URL shortener |
| Decomposition | 10 requirements with acceptance criteria, 4 clarifying questions (3 blocking), 13 tasks traced to requirements |
| Execution | 7 governed nodes, 19 agent attempts; developer, tester and docs in parallel |
| Recovery | Verify failed four times (3 → 3 → 27 → 30 of 31 tests passing), each failure routed back to developer and tester; the fifth run passed 31 of 31 |
| Governance | 13 human decisions, 12 policy escalations (10 protected paths, 2 autonomy), 0 denials; the release decision traces back through 122 linked events to all 13 |
| Output | ASP.NET Core minimal API (EF Core + SQLite, SSRF protection, rate limiting, expiry, analytics), 31 xUnit tests, README and docs: 23 files, ~2,000 lines |
| Cost | 14 model calls, about $1.12 at list price (thinking tokens included) |
| Audit | Hash-chained log, verified; head hash anchored in the generated repo's git commit |

All of it, unedited, is in [docs/evidence/](docs/evidence/).

## Scenarios

| Command | What it shows | Model answers |
|---|---|---|
| `run_demo.py greenfield` | URL shortener from a written brief, built as an **ASP.NET Core** API with xUnit tests | **Recorded**: 31 xUnit tests passing after 4 repair loops (3 → 3 → 27 → 30 → 31), then release |
| `run_demo.py brownfield` | Add daily click analytics to the existing **.NET** app: C# codebase scan, grounded impact analysis with human sign-off, revision of existing code | **Recorded**: builds on the released greenfield app; one repair loop (build error), then release |
| `run_demo.py ambiguous` | "Make links safer and give marketing better insights": blocking clarifying questions, human answers, requirements re-planned; .NET product | **Recorded; ends FAILED**: the 6-loop repair budget runs out (best 18/26) and nothing is released; see [scenarios](docs/SCENARIOS.md) |
| `run_demo.py greenfield-python` | The same brief on the Python stack (FastAPI): 9 requirements, 12 traced tasks, 18 files, 30 generated tests passing after 2 automatic repair loops | **Recorded** from gemini-3.8-flash; shows the stacks are pluggable |
| `run_demo.py new` | **Your own idea**, interactively: type it (or `--request-file`), answer clarifying questions one at a time, approve the readable spec (then locked) and plan; Warden builds an ASP.NET Core API + xUnit tests, verified with `dotnet build`/`dotnet test`; nothing outside the approved plan lands without your approval | Live Gemini, recorded for replay |
| `run_demo.py failures` | Policy block, human rejection, retry → fallback → recovery or rollback, safe-stop, re-plan after a requirement change, human rollback of a shipped feature, resume mid-graph from existing artifacts | Scripted (no model needed) |

Details for each scenario are in [docs/SCENARIOS.md](docs/SCENARIOS.md). The unedited output of the three
recorded .NET runs (reports, audit logs, specs, designs, the generated repo, the brownfield diff) is in
[docs/evidence/](docs/evidence/).

## Technology of generated projects

Warden itself is Python. What it generates comes from a pluggable **stack profile**
(`warden/stacks.py`):

| Stack | Generated project | Verify |
|---|---|---|
| `dotnet-aspnetcore` (default for `new`) | ASP.NET Core minimal API, EF Core + SQLite, xUnit with `WebApplicationFactory` (`src/App`, `tests/`) | `dotnet build`, then `dotnet test` (must report tests run) |
| `python-fastapi` (prepared scenarios) | FastAPI, sqlite3, pytest | pytest + ruff |

The .NET stack needs the .NET 10 SDK; package restore needs network access the first time.

## Model modes

- **Replay** (default): uses `recordings/<scenario>/`. Needs no key, and gives the same result
  every time. (The .NET scenarios need network once, for the NuGet package restore.)
- **`--record`**: calls Gemini only for prompts that have no recording yet, then saves the answers.
- **`--live`**: calls Gemini every time and records nothing.

For `--record` and `--live`, put `GEMINI_API_KEY=...` in `.env` (the file is git-ignored; see
`.env.example`).

**Policies.** `policies/default.yaml` is used unless you pass `--policy`. `policies/governed.yaml`
adds who may approve which gate and separation of duties (for example, the person who approved the
design may not approve the release). Your approvals are recorded under your login name.

## Documentation

| Document | Contents |
|---|---|
| [Walkthrough](docs/WALKTHROUGH.md) | A 10-minute guided tour mapped to the evaluation criteria |
| [Architecture](docs/ARCHITECTURE.md) | Components, orchestration model, control flow, governance, key decisions |
| [Scenarios](docs/SCENARIOS.md) | Decomposition, orchestration and validation for each scenario |
| [Evidence](docs/evidence/) | Reports, audit logs and generated code from the three recorded .NET runs |
| [Testing](docs/TESTING.md) | Testing approach, how to run it, what each layer proves |
| [Engineering summary](docs/ENGINEERING_SUMMARY.md) | Plan and rationale, artifacts, risks and trade-offs, assumptions, limitations, lessons from real runs |
| [Recording runbook](docs/RECORDING.md) | How the scenarios were recorded, and how to re-record one |

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

Requires Python 3.11+ (and the .NET 10 SDK for .NET projects). Developed on Windows 11; the tools are cross-platform.
