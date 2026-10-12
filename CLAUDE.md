# Warden: working agreement for Claude Code

Warden is a governed, agentic SDLC orchestrator (assignment: turn vague requirements into
reviewable software, with human approval at critical points). Agents execute under defined
autonomy boundaries; humans own oversight, approvals and final quality.

## Commands
- Test: `pytest -q`    Lint: `ruff check .`    Types: `mypy warden demo run_demo.py`
- Slow tests (real pytest/ruff/git subprocesses): `pytest -q -m slow`
- All four must pass before any commit. Keep the default suite fast (<2s total) and deterministic;
  anything that spawns a subprocess gets `@pytest.mark.slow`.

## Architecture (read these first)
- `warden/models.py`      node/run states + the legal transition table (single source of truth)
- `warden/eventlog.py`    hash-chained JSONL audit log; `caused_by` = decision lineage
- `warden/artifacts.py`   versioned, content-hashed store; `stale()` powers re-planning
- `warden/dag.py`         Graph + NodeSpec (gates, consumes/produces); validates at construction
- `warden/agent.py`       Agent contract; agents RETURN staged outputs, never write the store
- `warden/policy.py`      pre/post guardrail interface + YAML rule engine (`policies/default.yaml`)
- `warden/llm/`          LLMClient protocol; scripted, record/replay and Gemini (httpx) backends
- `warden/agents/`       six SDLC roles with strict Pydantic output schemas + `sdlc_graph()`
- `warden/bundle.py`     many generated files as one artifact; policy checks each file
- `warden/tools/`        workspace writer, scrubbed-env command runner, verify node, git, publish
- `warden/approval.py`    human approval interface; default approver REJECTS (fail closed)
- `warden/orchestrator.py` scheduler: parallel branches + joins, retries -> fallback -> rollback,
                           approvals, safe-stop, replan, rollback, adopt (brownfield entry), resume

## Rules that must not be broken
1. Only the scheduler thread mutates node state; worker threads only run agents.
2. Nothing is committed to the artifact store until exit gate + policy + approval all pass.
3. Every state change and decision is an event; never mutate or delete log lines.
4. Retries are bounded; policy denials and human rejections are NEVER retried.
5. Generated projects must be normal repos with no runtime dependency on Warden.
6. No secrets in code or logs. Keys live in `.env` (git-ignored).
7. Standard library only in `warden/` core, except PyYAML for loading policy files;
   other third-party deps go in agents/adapters.

## Working style
- Small, reviewable changes: one concern per commit, tests first or alongside.
- Ask before changing the transition table, event schema or any rule above.
- Do not claim a feature is done unless a test demonstrates it.
