"""Scripted (hand-written) model answers for demos that must not depend on a live model.

They are small but valid against every agent's schema and checks, so the real
orchestrator, policy engine and approval gates process them exactly as they would
process a model's answer. Anything shown with these answers is labelled "scripted".
"""

from __future__ import annotations

import json
from typing import Any

SPEC: dict[str, Any] = {
    "title": "URL shortener (scripted)",
    "problem_statement": "Turn long URLs into short codes that redirect.",
    "requirements": [
        {"id": "R1", "text": "POST /shorten returns a short code", "category": "functional",
         "priority": "must", "acceptance_criteria": ["valid URL -> 201 with code"]},
        {"id": "R2", "text": "GET /{code} redirects to the original URL", "category": "functional",
         "priority": "must", "acceptance_criteria": ["known code -> 307", "unknown code -> 404"]},
    ],
    "assumptions": ["single instance, SQLite storage"],
    "open_questions": [],
    "out_of_scope": ["user accounts"],
}

DESIGN: dict[str, Any] = {
    "overview": "FastAPI app over SQLite.",
    "components": [{"name": "api", "responsibility": "HTTP layer", "module": "app/main.py"}],
    "api": [{"method": "POST", "path": "/shorten", "summary": "create a short link",
             "status_codes": [201, 422], "requirement_ids": ["R1"]},
            {"method": "GET", "path": "/{code}", "summary": "redirect", "status_codes": [307, 404],
             "requirement_ids": ["R2"]}],
    "data_model": [{"name": "Link", "fields": ["code", "url", "created_at"]}],
    "decisions": [{"id": "D1", "title": "Storage", "choice": "sqlite3", "rationale": "no server to run",
                   "alternatives": ["PostgreSQL"]}],
    "tasks": [
        {"id": "T1", "title": "Shorten and redirect endpoints", "owner": "developer",
         "requirement_ids": ["R1", "R2"], "depends_on": []},
        {"id": "T2", "title": "API tests", "owner": "tester", "requirement_ids": ["R1", "R2"],
         "depends_on": ["T1"]},
        {"id": "T3", "title": "README", "owner": "docs", "requirement_ids": ["R1", "R2"], "depends_on": ["T1"]},
    ],
    "risks": [{"risk": "code collisions", "mitigation": "unique index and retry"}],
}

APP = "from fastapi import FastAPI\n\napp = FastAPI()\n"


def bundle(files: dict[str, str], notes: str = "") -> dict[str, Any]:
    return {"files": [{"path": p, "content": c} for p, c in files.items()], "notes": notes}


CODE = bundle({"app/__init__.py": "", "app/main.py": APP, "requirements.txt": "fastapi>=0.110\n"})
TESTS = bundle({"tests/test_api.py": "def test_r1_placeholder() -> None:\n    assert True\n"})
DOCS = bundle({"README.md": "# URL shortener\n\nScripted demo output.\n"})
REVIEW: dict[str, Any] = {
    "recommendation": "ready", "summary": "Scripted review.",
    "checklist": [{"item": "R1 implemented", "status": "pass", "evidence": "app/main.py"}],
    "blocking_issues": [],
}

ANSWERS: dict[str, dict[str, Any]] = {
    "requirements": SPEC, "architect": DESIGN, "developer": CODE, "tester": TESTS, "docs": DOCS,
    "release": REVIEW,
}


def js(obj: Any) -> str:
    return json.dumps(obj)
