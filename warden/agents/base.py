"""Structured LLM agent: prompt -> JSON -> schema + semantic checks -> artifact.

One call per attempt, plus a bounded number of repair rounds where the validation
errors are fed back to the model. If the output is still invalid the agent raises
``AgentOutputError`` and the orchestrator's retry/fallback policy takes over.
LLM errors propagate unchanged for the same reason. Every prompt is a pure function
of the inputs, feedback and previous errors, so record/replay is deterministic.
"""

from __future__ import annotations

import json
import time
from functools import cache
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from warden.agent import AgentContext, AgentResult
from warden.agents.schemas import FileBundle
from warden.bundle import BundleError, is_bundle, pack, unpack
from warden.llm import LLMClient, LLMRequest
from warden.stacks import PYTHON, StackProfile

M = TypeVar("M", bound=BaseModel)

_MAX_ECHO = 4000  # characters of a rejected answer quoted back in a repair prompt


@cache
def _schema_text(schema: type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), sort_keys=True)


class AgentOutputError(Exception):
    """The model's output stayed invalid after the allowed repair rounds."""

    def __init__(self, message: str, usage: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.usage = usage or []  # model calls already spent; the orchestrator still logs them


def strip_fences(text: str) -> str:
    """Drop a ```json ... ``` wrapper some models add even in JSON mode (linear time)."""
    stripped = text.strip()
    if not (stripped.startswith("```") and stripped.endswith("```") and "\n" in stripped):
        return text
    body = stripped[: -3].rstrip()
    return body.split("\n", 1)[1] if "\n" in body else text


class StructuredAgent(Generic[M]):
    # set by each role
    name: str  # plain attribute: satisfies the Agent protocol
    schema: ClassVar[type[BaseModel]]
    output: ClassVar[str]
    kind: ClassVar[str]
    role: str  # system prompt (a stack profile may override it per instance)
    task: str  # what to produce, appended to the user prompt (overridable per stack)
    # how to act on feedback; only added when there is feedback, so first prompts (and
    # their recordings) do not change when this guidance is tuned
    feedback_hint: ClassVar[str] = ""

    def __init__(self, llm: LLMClient, *, repair_attempts: int = 1, stack: StackProfile = PYTHON) -> None:
        self.llm = llm
        self.repair_attempts = repair_attempts
        self.stack = stack
        # a stack profile may replace this role's prompts; the Python profile replaces none
        override = stack.prompts.get(self.name, {})
        if "role" in override:
            self.role = override["role"]
        if "task" in override:
            self.task = override["task"]

    # ---- hooks for roles -------------------------------------------------
    def builtin_checks(self, model: M) -> list[str]:
        """Checks every agent of a kind gets for free (roles add theirs in ``check``)."""
        return []

    def check(self, model: M, inputs: dict[str, str]) -> list[str]:
        """Semantic checks beyond the schema; return human-readable problems."""
        return []

    def render(self, model: M) -> dict[str, str | bytes]:
        return {self.output: model.model_dump_json(indent=2)}

    def summarize(self, model: M) -> str:
        return f"{self.name} produced {self.output}"

    def escalation(self, model: M) -> str:
        return ""

    # ---- engine ------------------------------------------------------------
    def run(self, ctx: AgentContext) -> AgentResult:
        inputs = {name: ctx.read_text(name) for name in ctx.consumes}
        # previous output: after feedback, or existing code in a brownfield run; absent on a first
        # greenfield run, so those prompts (and their recordings) are unchanged
        base = self._prompt(inputs, ctx.feedback, self.previous_output(ctx))
        prompt = base
        errors: list[str] = []
        usage: list[dict[str, Any]] = []
        for _ in range(self.repair_attempts + 1):
            t0 = time.monotonic()
            response = self.llm.complete(LLMRequest(prompt, system=self.role, json_mode=True))
            usage.append({"model": response.model, "backend": response.backend,
                          "input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
                          "thinking_tokens": response.thinking_tokens,
                          "ms": int((time.monotonic() - t0) * 1000)})
            model, errors = self._parse(response.text, inputs)
            if model is not None:
                return AgentResult(self.render(model), kind=self.kind, summary=self.summarize(model),
                                   escalate=self.escalation(model), usage=usage)
            prompt = base + self._repair_section(response.text, errors)
        raise AgentOutputError(f"{self.name}: invalid output after {self.repair_attempts + 1} "
                               f"attempt(s): {'; '.join(errors[:5])}", usage)

    def _parse(self, text: str, inputs: dict[str, str]) -> tuple[M | None, list[str]]:
        try:
            model = self.schema.model_validate_json(strip_fences(text))
        except ValidationError as exc:
            return None, [f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
                          for e in exc.errors()]
        typed: M = model  # type: ignore[assignment]  # schema is the role's M
        problems = self.builtin_checks(typed) + self.check(typed, inputs)
        return (None, problems) if problems else (typed, [])

    def previous_output(self, ctx: AgentContext) -> str:
        """This agent's own last committed output, shown when revising after feedback."""
        art = ctx.store.latest(self.output)
        return self._show(self.output, ctx.store.read_text(art)) if art is not None else ""

    def _prompt(self, inputs: dict[str, str], feedback: str | None, previous: str = "") -> str:
        parts = [f"# Task\n{self.task.strip()}", "# Inputs"]
        for name, content in inputs.items():
            parts.append(f"## {name}\n{self._show(name, content)}")
        if previous:
            parts.append(f"# Your previous {self.output} (revise it; do not start over)\n{previous}")
        if feedback:
            hint = f"\n\n{self.feedback_hint.strip()}" if self.feedback_hint else ""
            parts.append("# Feedback from a human reviewer or a failed verification (must be addressed)\n"
                         + feedback.strip() + hint)
        schema = _schema_text(self.schema)
        parts.append("# Output format\nRespond with ONE JSON object that validates against this "
                     "JSON Schema. No prose and no markdown fences.\n" + schema)
        return "\n\n".join(parts)

    @staticmethod
    def _show(name: str, content: str) -> str:
        if not is_bundle(name):
            return content
        try:
            files = unpack(content)
        except BundleError:
            return content
        return "\n".join(f"### {path}\n```\n{text}\n```" for path, text in files.items())

    @staticmethod
    def _repair_section(previous: str, errors: list[str]) -> str:
        listed = "\n".join(f"- {e}" for e in errors[:20])
        return (f"\n\n# Your previous answer was rejected\nFix these problems:\n{listed}\n\n"
                f"Previous answer:\n{previous[:_MAX_ECHO]}\n\nReturn the corrected JSON object only.")


class BundleAgent(StructuredAgent[FileBundle]):
    """Roles that emit files: the model returns a FileBundle, stored as ``*.bundle``."""

    schema = FileBundle

    def builtin_checks(self, model: FileBundle) -> list[str]:
        """The stack's in-agent syntax check (Python: ``compile``, which only parses).

        A syntax error found here costs one repair call; found by verify it costs a whole
        repair loop (both authors regenerate, then the tests run again).
        """
        check = self.stack.syntax_check
        if check is None:
            return []
        return [problem for f in model.files if (problem := check(f.path, f.content))]

    def render(self, model: FileBundle) -> dict[str, str | bytes]:
        return {self.output: pack({f.path: f.content for f in model.files})}

    def summarize(self, model: FileBundle) -> str:
        paths = sorted(f.path for f in model.files)
        notes = f". Notes: {model.notes}" if model.notes else ""
        return f"{self.name}: {len(paths)} file(s): {', '.join(paths)}{notes}"
