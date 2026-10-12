"""`run_demo.py new`: build a new project from your own idea, interactively.

Intake (typed idea or --request-file) -> discovery (one question at a time) -> spec approval
(locks the spec) -> plan approval -> build with the scope guardrail -> your final call.
"""

from __future__ import annotations

import re
from functools import partial

from demo.runner import Scenario
from warden.agents import sdlc_graph
from warden.stacks import DOTNET, StackProfile


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "project"


def new_project_scenario(name: str, idea: str, stack: StackProfile = DOTNET) -> Scenario:
    """A vague idea always gets its spec reviewed (and its questions answered) by a human."""
    request = f"# {name.strip()}\n\n{idea.strip()}\n"
    return Scenario(slugify(name), request, partial(sdlc_graph, require_spec_approval=True), stack=stack)
