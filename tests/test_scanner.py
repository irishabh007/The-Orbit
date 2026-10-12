import json
import unittest

from warden.agent import AgentContext
from warden.artifacts import ArtifactStore
from warden.bundle import pack
from warden.tools.scanner import ScanAgent, scan_files

APP = {
    "app/__init__.py": "",
    "app/db.py": "import sqlite3\n\n\ndef connect(path: str) -> sqlite3.Connection:\n    return sqlite3.connect(path)\n",
    "app/service.py": (
        "from app.db import connect\n\n\nclass LinkService:\n    def create(self, url: str) -> str:\n"
        "        return 'abc'\n\n\ndef helper() -> None:\n    pass\n"
    ),
    "app/main.py": (
        "from fastapi import APIRouter, FastAPI\n\nfrom app.service import LinkService\n\n"
        "app = FastAPI()\nrouter = APIRouter()\n\n\n@app.post('/shorten', status_code=201)\n"
        "def shorten() -> dict:\n    return {}\n\n\n@router.get(\"/{code}\")\nasync def redirect(code: str):\n"
        "    return code\n\n\n@app.get('/health')\ndef health():\n    return {'ok': True}\n"
    ),
}
TESTS = {
    "tests/test_api.py": "from fastapi.testclient import TestClient\n\nfrom app.main import app\n\n\ndef test_x():\n    pass\n",
    "tests/test_service.py": "from app.service import LinkService\n\n\ndef test_y():\n    pass\n",
}


class ScanFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = scan_files({**APP, **TESTS})
        self.modules = {m["path"]: m for m in self.result["modules"]}

    def test_lists_modules_with_symbols(self) -> None:
        self.assertEqual(set(self.modules), {"app/__init__.py", "app/db.py", "app/service.py", "app/main.py",
                                             "tests/test_api.py", "tests/test_service.py"})
        self.assertEqual(self.modules["app/service.py"]["classes"], ["LinkService"])
        self.assertEqual(self.modules["app/service.py"]["functions"], ["helper"])

    def test_internal_import_graph(self) -> None:
        self.assertEqual(self.modules["app/main.py"]["imports_internal"], ["app/service.py"])
        self.assertEqual(self.modules["app/service.py"]["imports_internal"], ["app/db.py"])
        self.assertIn("fastapi", self.modules["app/main.py"]["imports_external"])

    def test_finds_http_endpoints_from_decorators(self) -> None:
        endpoints = {(e["method"], e["path"]): e for e in self.result["endpoints"]}
        self.assertEqual(set(endpoints), {("POST", "/shorten"), ("GET", "/{code}"), ("GET", "/health")})
        self.assertEqual(endpoints[("GET", "/{code}")]["handler"], "redirect")
        self.assertEqual(endpoints[("GET", "/{code}")]["module"], "app/main.py")

    def test_maps_tests_to_the_modules_they_cover(self) -> None:
        self.assertEqual(self.result["tests"]["tests/test_api.py"], ["app/main.py"])
        self.assertEqual(self.result["tests"]["tests/test_service.py"], ["app/service.py"])

    def test_reverse_dependencies_show_blast_radius(self) -> None:
        # changing app/db.py affects service directly and main transitively
        self.assertEqual(self.result["dependents"]["app/db.py"], ["app/main.py", "app/service.py"])

    def test_unparseable_files_are_reported_not_fatal(self) -> None:
        result = scan_files({"app/broken.py": "def (:\n"})
        self.assertEqual(result["parse_errors"][0]["path"], "app/broken.py")

    def test_output_is_deterministic(self) -> None:
        self.assertEqual(json.dumps(scan_files({**TESTS, **APP}), sort_keys=True),
                         json.dumps(self.result, sort_keys=True))


CSHARP = {
    "src/App/Program.cs": (
        "using App.Data;\nusing App.Services;\n\nvar builder = WebApplication.CreateBuilder(args);\n"
        "var app = builder.Build();\napp.MapPost(\"/shorten\", (ShortenRequest r, LinkService s) => s.Create(r));\n"
        "app.MapGet(\"/{code}\", (string code) => Results.Ok());\napp.Run();\npublic partial class Program { }\n"
    ),
    "src/App/Services/LinkService.cs": (
        "using App.Data;\n\nnamespace App.Services;\n\npublic sealed class LinkService\n{\n}\n"
        "public record ShortenRequest(string Url);\n"
    ),
    "src/App/Data/AppDbContext.cs": "namespace App.Data;\n\npublic class AppDbContext\n{\n}\n",
    "src/App/Controllers/AnalyticsController.cs": (
        "using Microsoft.AspNetCore.Mvc;\nusing App.Services;\n\nnamespace App.Controllers;\n\n"
        "[ApiController]\n[Route(\"api/analytics\")]\npublic class AnalyticsController : ControllerBase\n{\n"
        "    [HttpGet(\"{code}\")]\n    public IActionResult Get(string code) => Ok();\n}\n"
    ),
    "tests/LinkTests.cs": "using App.Services;\n\nnamespace Tests;\n\npublic class LinkTests\n{\n}\n",
}


class CSharpScanTests(unittest.TestCase):
    def setUp(self) -> None:
        from warden.tools.scanner import scan_csharp
        self.result = scan_csharp(CSHARP)
        self.modules = {m["path"]: m for m in self.result["modules"]}

    def test_endpoints_from_minimal_apis_and_controllers(self) -> None:
        endpoints = {(e["method"], e["path"]) for e in self.result["endpoints"]}
        self.assertEqual(endpoints, {("POST", "/shorten"), ("GET", "/{code}"), ("GET", "/api/analytics/{code}")})

    def test_types_and_namespace_dependencies(self) -> None:
        self.assertEqual(self.modules["src/App/Services/LinkService.cs"]["classes"],
                         ["LinkService", "ShortenRequest"])
        self.assertIn("src/App/Data/AppDbContext.cs", self.modules["src/App/Services/LinkService.cs"]["imports_internal"])
        self.assertIn("Microsoft.AspNetCore.Mvc",
                      self.modules["src/App/Controllers/AnalyticsController.cs"]["imports_external"])

    def test_blast_radius_and_test_coverage(self) -> None:
        self.assertIn("src/App/Program.cs", self.result["dependents"]["src/App/Data/AppDbContext.cs"])
        self.assertEqual(self.result["tests"]["tests/LinkTests.cs"], ["src/App/Services/LinkService.cs"])


class ScanAgentTests(unittest.TestCase):
    def test_scans_consumed_bundles_into_codebase_json(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            store.put("code.bundle", pack(APP))
            store.put("tests.bundle", pack(TESTS))
            ctx = AgentContext("run1", "scan", 1, store, ("code.bundle", "tests.bundle"))
            result = ScanAgent().run(ctx)
        data = json.loads(result.outputs["codebase.json"])
        self.assertEqual(len(data["endpoints"]), 3)
        self.assertIn("6 modules", result.summary)
        self.assertIn("3 endpoints", result.summary)
