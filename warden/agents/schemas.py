"""Strict output schemas for every agent role (Pydantic v2).

The JSON Schema of each model is put in the prompt, and the model's answer must
validate against it. ``extra="forbid"`` means a hallucinated field is an error,
not silently dropped. Cross-artifact checks (e.g. tasks must cite real requirement
ids) live in the agents, because they need the upstream artifacts.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from warden.bundle import path_problem

Text = Annotated[str, Field(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _unique(ids: list[str], what: str) -> None:
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate {what} ids: {dupes}")


# ------------------------------------------------------------------ requirements
class Requirement(_Model):
    id: Annotated[str, Field(pattern=r"^R\d+$")]
    text: Text
    category: Literal["functional", "security", "reliability", "performance", "observability", "usability"]
    priority: Literal["must", "should", "could"]
    acceptance_criteria: Annotated[list[Text], Field(min_length=1)]


class Question(_Model):
    id: Annotated[str, Field(pattern=r"^Q\d+$")]
    question: Text
    why_it_matters: Text
    blocking: bool  # True = cannot design responsibly without a human answer
    proposed_default: Text  # what we assume if the human accepts the default


class RequirementsSpec(_Model):
    title: Text
    problem_statement: Text
    requirements: Annotated[list[Requirement], Field(min_length=1)]
    assumptions: list[Text]
    open_questions: list[Question]
    out_of_scope: list[Text]

    @model_validator(mode="after")
    def _ids(self) -> RequirementsSpec:
        _unique([r.id for r in self.requirements], "requirement")
        _unique([q.id for q in self.open_questions], "question")
        return self


# ------------------------------------------------------------------ design + plan
class Component(_Model):
    name: Text
    responsibility: Text
    module: Text  # relative file path, e.g. app/main.py


class Endpoint(_Model):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: Annotated[str, Field(pattern=r"^/")]
    summary: Text
    status_codes: Annotated[list[int], Field(min_length=1)]
    requirement_ids: list[str]


class Entity(_Model):
    name: Text
    fields: Annotated[list[Text], Field(min_length=1)]


class Decision(_Model):
    id: Annotated[str, Field(pattern=r"^D\d+$")]
    title: Text
    choice: Text
    rationale: Text
    alternatives: list[Text]


class Task(_Model):
    id: Annotated[str, Field(pattern=r"^T\d+$")]
    title: Text
    owner: Literal["developer", "tester", "docs"]
    requirement_ids: Annotated[list[str], Field(min_length=1)]
    depends_on: list[str]


class Risk(_Model):
    risk: Text
    mitigation: Text


class Design(_Model):
    overview: Text
    components: Annotated[list[Component], Field(min_length=1)]
    api: list[Endpoint]
    data_model: list[Entity]
    decisions: list[Decision]
    tasks: Annotated[list[Task], Field(min_length=1)]
    risks: list[Risk]

    @model_validator(mode="after")
    def _task_graph(self) -> Design:
        ids = [t.id for t in self.tasks]
        _unique(ids, "task")
        _unique([d.id for d in self.decisions], "decision")
        deps = {t.id: set(t.depends_on) for t in self.tasks}
        unknown = sorted({d for ds in deps.values() for d in ds} - set(ids))
        if unknown:
            raise ValueError(f"tasks depend on unknown tasks: {unknown}")
        done: set[str] = set()
        while len(done) < len(deps):  # Kahn's algorithm: anything left over is a cycle
            ready = {t for t, ds in deps.items() if t not in done and ds <= done}
            if not ready:
                raise ValueError(f"task dependency cycle among: {sorted(set(deps) - done)}")
            done |= ready
        return self


# ------------------------------------------------------------------ file bundles
class SourceFile(_Model):
    path: Text
    content: str


class FileBundle(_Model):
    files: Annotated[list[SourceFile], Field(min_length=1)]
    notes: str = ""  # anything the next stage or the human should know

    @model_validator(mode="after")
    def _paths(self) -> FileBundle:
        _unique([f.path for f in self.files], "file path")
        problems = [p for f in self.files if (p := path_problem(f.path))]
        if problems:
            raise ValueError(f"unsafe file paths: {problems}")
        return self


# ------------------------------------------------------------------ release review
class CheckItem(_Model):
    item: Text
    status: Literal["pass", "fail", "unknown"]
    evidence: Text


class ReleaseReview(_Model):
    recommendation: Literal["ready", "not_ready"]
    summary: Text
    checklist: Annotated[list[CheckItem], Field(min_length=1)]
    blocking_issues: list[Text]

    @model_validator(mode="after")
    def _consistent(self) -> ReleaseReview:
        failing = [c.item for c in self.checklist if c.status == "fail"]
        if self.recommendation == "ready" and (failing or self.blocking_issues):
            raise ValueError(f"'ready' contradicts failing checks {failing} / blocking issues")
        return self
