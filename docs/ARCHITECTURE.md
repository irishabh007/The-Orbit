# Architecture

Warden is an in-process orchestrator that runs an SDLC workflow as an explicit dependency graph.
Agents produce work. The orchestrator decides what runs, what gets committed, and when a human
must decide. Every decision is written to a hash-chained audit log.

## Components

```mermaid
graph LR
    subgraph Core["warden/ core (stdlib + PyYAML)"]
        ORCH[orchestrator.py<br/>scheduler + state machine]
        DAG[dag.py<br/>graph, gates, validation]
        MOD[models.py<br/>states + transition table]
        LOG[eventlog.py<br/>hash-chained audit log]
        ART[artifacts.py<br/>versioned content-hashed store]
        POL[policy.py<br/>YAML guardrails]
        APR[approval.py<br/>human gate, fail closed]
        REP[report.py<br/>metrics from the log]
    end
    subgraph Adapters
        AG[agents/<br/>8 roles, Pydantic schemas]
        LLM[llm/<br/>Gemini, record/replay, scripted, retry]
        TL[tools/<br/>verify, review gate, scanner, git]
    end
    ORCH --> DAG & MOD & LOG & ART & POL & APR
    ORCH -->|runs| AG & TL
    AG --> LLM
    REP --> LOG
```

| Component | Responsibility |
|---|---|
| `models.py` | Node and run states plus the **legal transition table**, the single source of truth. Any other transition raises. |
| `dag.py` | `NodeSpec` (dependencies, consumed and produced artifacts, entry and exit gates, approval, retries, fallback, failure re-plan targets) and `Graph`, which is validated at construction: no cycles, every artifact has one producer, and consumers only read artifacts produced upstream (this keeps parallel branches safe). |
| `orchestrator.py` | Scheduler. It runs ready nodes in parallel on a thread pool, but **only the scheduler thread changes node state**. Humans are asked on a separate approval thread, so a pending decision never blocks other work. It handles retries, fallback, repair loops (routed to the node at fault), rollback, safe-stop, re-planning, brownfield `adopt()`, and resume after a crash (including pending human feedback). |
| `artifacts.py` | Versioned store keyed by SHA-256. Each artifact records the hashes of the inputs it was built from, so `stale()` and re-planning know exactly what changed. Writing identical content is a no-op, which is how unchanged outputs stop a cascade. |
| `eventlog.py` | Append-only JSONL. Each event holds the previous event's hash, so editing, deleting or reordering a line fails `verify()`. `caused_by` links give decision lineage. |
| `policy.py` + `policies/*.yaml` | Pre-run, post-run and approval guardrails: secrets, compliance (personal data), dependency allowlist, protected paths, scope, autonomy per role, and who may approve which gate (`governed.yaml`). Loading fails closed on any malformed rule. |
| `approval.py` | Human gate. The default approver **rejects everything**, so with no human connected nothing that needs approval can proceed. |
| `agents/` | Requirements, Impact, Architect (which also plans), Developer, Tester, Docs, Reviewer and Release. Each validates JSON output against a strict schema plus cross-artifact checks, with one repair round. |
| `llm/` | One `LLMClient` interface, with backends for Gemini (httpx), record/replay, scripted answers and bounded backoff. Errors are classified as transient, quota or permanent. |
| `tools/` | Verify (stack commands on a scratch copy), the consistency-review gate, codebase scanners (Python `ast`, C# patterns), workspace writer, command runner with a scrubbed environment, git, publish. |
| `stacks.py` | Stack profiles for generated projects: prompt overrides per role, file-ownership rules, in-agent syntax check, verify commands, scanner, deterministic review rules. `dotnet-aspnetcore` (ASP.NET Core + EF Core + xUnit) and `python-fastapi`. |
| `report.py` | Computes metrics and the Markdown report from the audit log alone. |

## Orchestration model

### Workflows

```mermaid
graph TD
    R[requirements] --> A[architect + plan 🔒]
    A --> D[developer] & T[tester] & DO[docs]
    D --> V[verify]
    T --> V
    V -. failure: feedback to both .-> D
    V -. failure: feedback to both .-> T
    V --> REL[release 🔒]
    DO --> REL
```

Brownfield adds `scan → requirements → impact 🔒 → architect` in front. The scanner and the impact
analysis read an immutable snapshot of the existing repo, while the agents revise its files.
🔒 marks approval set on the graph itself; the policy engine can add more gates but never remove
these.

**With the consistency review** (`review=True`; used by `new` and `harden`), the docs are written
from the verified code and checked before release:

```mermaid
graph LR
    V[verify] --> DO[docs from the code] --> RV[reviewer] --> REL[release 🔒]
    RV -. blocking finding: feedback to its owner .-> D[developer / tester / docs]
```

The reviewer is a model agent whose findings must cite real files, joined by deterministic stack
rules it cannot remove (.NET: read-then-write counters, `EnsureCreated`). The gate names the owner of
each blocking finding, so only that node and its descendants redo work. The recorded scenarios made
before this step existed keep their original graphs, so they still replay.

### Node state machine

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> READY: dependencies done, entry gate passes
    READY --> RUNNING: launched (pre-run policy passed)
    RUNNING --> AWAITING_APPROVAL: needs a human
    RUNNING --> SUCCEEDED: exit gate + policy pass, outputs committed
    RUNNING --> FAILED
    AWAITING_APPROVAL --> SUCCEEDED: approved
    AWAITING_APPROVAL --> FAILED: rejected, or changes requested (retry with feedback)
    FAILED --> READY: bounded retry or fallback
    FAILED --> ROLLED_BACK: exhausted, or not retryable
    SUCCEEDED --> STALE: an input changed (re-plan)
    SUCCEEDED --> ROLLED_BACK: human rollback
    STALE --> PENDING
    ROLLED_BACK --> PENDING: released, or re-planned
```

### Control flow of one attempt

1. **Entry gate:** dependencies have SUCCEEDED, consumed artifacts exist, and the node's entry
   gate (if any) passes. Otherwise the node waits, or is reported as blocked.
2. **Pre-run policy:** for example, a role with autonomy `none` is denied before an agent runs.
3. **Agent runs** in a worker thread and returns **staged** outputs. Agents never write to the store.
4. **Exit gate:** checks for missing or undeclared outputs, then runs the node's own gate (verify:
   tests and lint passed; reviewer: no blocking finding). A failing gate can name the upstream node
   at fault.
5. **Stale-input check:** if an input changed while the agent was working, the result is discarded
   and the node re-queued.
6. **Post-run policy** on every output file, including files inside bundles: deny (secrets,
   personal data, unlisted dependencies, malformed bundle) or escalate (protected path,
   `supervised` role).
7. **Approval** if the graph, the policy or the agent asks for it. The agent can only *add*
   oversight. The human is asked on a separate approval thread, so the scheduler keeps running
   other nodes; the decision is applied back on the scheduler thread. An approval must also pass
   the policy's `approvals` rule (role for this gate, separation of duties), and an optional timeout
   fails the gate closed. Approve leads to commit; reject fails the node without retry; "changes" sends the
   comment back as feedback for a retry.
8. **Commit:** outputs go to the artifact store with input hashes. Content that actually changed
   triggers re-planning of the nodes that consume it.

**Failure handling** is bounded at every level:

| Level | Mechanism | Bound |
|---|---|---|
| Model call | `RetryingClient`: backoff on rate limits, timeouts and 5xx | 4 attempts; a quota longer than 60s, and requests the service refuses (4xx: bad key, billing), are not retried |
| Agent output | Schema or check errors fed back to the model | 1 repair round |
| Node | Retry, then the fallback agent | `max_retries` (2), then one fallback |
| Workflow | Failure routed upstream (verify → developer + tester) | `max_replans`: 4, or 6 for vague requests |
| Consistency review | Blocking findings routed to their owner only | `max_review_loops`: 2 |
| Human | Approval on its own thread; optional timeout fails the gate closed | `approval_timeout_s` |
| Run | Rollback and halt, or safe-stop (kill switch or step budget) | — |

These failures are never retried (CLAUDE.md rule 4):
- policy denials
- human rejections
- `PermanentAgentError` (daily quota, replay miss, a request the model service refuses)

## Governance

| Guardrail | Where | Example |
|---|---|---|
| Security | policy `secrets` | Hard-coded AWS key → deny. The reason names the file and line, never the value |
| Compliance | policy `compliance` | SSN or payment card (Luhn-checked) in a test fixture → deny |
| Supply chain | policy `dependencies` | Unlisted package in `requirements.txt` or `*.csproj` (NuGet `PackageReference`), pip options, direct URLs → deny |
| Change control | policy `protected_paths` | `requirements*.txt`, `*.csproj`, `appsettings*.json`, `Migrations/`, `.github/*`, `policies/*` → human approval |
| Scope | policy `scope` | The developer may write only what the approved design (or brownfield impact analysis) contains, plus scaffolding; anything else stops for a human with the files named |
| Controlled autonomy | policy `autonomy` per role | architect, impact, release: `supervised`; developer: `full`; unknown roles: `supervised` |
| Human checkpoints | graph `requires_approval` | Architect and release always; spec for vague requests; impact for brownfield |
| Who may approve | policy `approvals` (`policies/governed.yaml`) | Release needs the release-manager role; the person who approved the design may not approve the release; an unanswered gate times out as a rejection |
| Execution safety | `tools/runner.py` | Generated code runs with an allowlisted environment (no keys) and a timeout |
| Audit | `eventlog.py`, `tools/git.py` | Hash chain + `caused_by` lineage; the head hash is anchored in each published git commit, so even a full log rewrite is detected. The report traces the release back to every human decision and checks the anchors |

## Key decisions

| Decision | Choice | Why | Alternatives |
|---|---|---|---|
| Orchestration | Own scheduler over an explicit DAG with a transition table | The brief grades the orchestrator; governance needs every transition visible and enforced | LangGraph, Temporal, Airflow: more machinery, less control over the governance semantics |
| Concurrency | Thread pool for agents, a separate approval thread for humans, a single scheduler thread for state | Parallel I/O-bound agents and a waiting human never block each other; no locks on node state | asyncio (fine, but no gain for blocking HTTP and subprocess calls) |
| Commit model | Agents return staged outputs; the orchestrator commits after gates, policy and approval | A failed or discarded attempt has nothing to undo | Agents writing directly, which needs compensation logic |
| Lineage | Content-hashed artifacts with input hashes | Precise re-planning; unchanged outputs stop cascades | Timestamps or version counters (over-invalidate) |
| Audit | Hash-chained JSONL with `caused_by` | Tamper-evident and readable with standard tools | A database (heavier; same evidence) |
| Many files per agent | One `*.bundle` artifact, expanded for policy checks | Nodes declare outputs up front; file names are not known in advance | Dynamic outputs (break graph validation) |
| LLM determinism | Record/replay keyed by request hash, with the n-th repeat recorded separately; closest-match fallback (≥ 99% alike, same agent) when flaky generated tests change a prompt slightly | Reviewers run offline; retries still get fresh answers | Mocking (proves less), always live (cost, flakiness) |
| Model | gemini-3.8-flash behind one interface | Free tier for development; swappable | Any provider fits `LLMClient` |
| Dependencies | Standard library in core; PyYAML for policy; httpx and Pydantic only in adapters | Small trusted core, enforced by a test | — |
| Generated stack | Stack profiles; .NET (ASP.NET Core + xUnit) by default for new projects | The engine stays stack-agnostic; .NET matches the target role; Python kept so recorded runs stay valid | Hard-coding one stack; rewriting the engine in C# |
| Generated output | A normal git repo, one commit per feature, rollback by `git revert` | Humans continue the code without Warden | Keeping output inside Warden's store |
| Docs versus code | Docs written from the verified code; a reviewer agent plus deterministic stack rules before release | Tests prove code *works*, not that claims are *true*; an external review found two bugs that tests had passed | Trusting docs written from the design; human review only |

More rationale, risks and limitations: [ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md).
