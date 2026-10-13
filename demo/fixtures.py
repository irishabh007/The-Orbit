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

CONSISTENCY: dict[str, Any] = {"summary": "Scripted review: docs and design match the code.", "findings": []}

ANSWERS: dict[str, dict[str, Any]] = {
    "requirements": SPEC, "architect": DESIGN, "developer": CODE, "tester": TESTS, "docs": DOCS,
    "reviewer": CONSISTENCY, "release": REVIEW,
}


def js(obj: Any) -> str:
    return json.dumps(obj)


# ---------------------------------------------------------------- .NET (dotnet-aspnetcore)
# A tiny but real ASP.NET Core minimal API + xUnit project: verify really builds and tests it.
DOTNET_APP_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
</Project>
"""

DOTNET_PROGRAM = """using System.Collections.Concurrent;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();
var links = new ConcurrentDictionary<string, string>();

app.MapPost("/shorten", (ShortenRequest request) =>
{
    if (!Uri.TryCreate(request.Url, UriKind.Absolute, out var uri) || uri.Scheme is not ("http" or "https"))
    {
        return Results.UnprocessableEntity(new { error = "url must be http or https" });
    }
    var code = Convert.ToHexString(Guid.NewGuid().ToByteArray())[..7].ToLowerInvariant();
    links[code] = request.Url;
    return Results.Created($"/{code}", new { code });
});

app.MapGet("/{code}", (string code) =>
    links.TryGetValue(code, out var url) ? Results.Redirect(url) : Results.NotFound());

app.Run();

public record ShortenRequest(string Url);

public partial class Program { }
"""

DOTNET_TESTS_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <IsPackable>false</IsPackable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.AspNetCore.Mvc.Testing" Version="10.0.12" />
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.14.1" />
    <PackageReference Include="xunit" Version="2.9.3" />
    <PackageReference Include="xunit.runner.visualstudio" Version="3.1.4" />
  </ItemGroup>
  <ItemGroup>
    <Using Include="Xunit" />
  </ItemGroup>
  <ItemGroup>
    <ProjectReference Include="../src/App/App.csproj" />
  </ItemGroup>
</Project>
"""

DOTNET_TEST = """using System.Net;
using System.Net.Http.Json;
using Microsoft.AspNetCore.Mvc.Testing;

public class ShortenerTests : IClassFixture<WebApplicationFactory<Program>>
{
    private readonly HttpClient _client;

    public ShortenerTests(WebApplicationFactory<Program> factory) =>
        _client = factory.CreateClient(new WebApplicationFactoryClientOptions { AllowAutoRedirect = false });

    [Fact]
    public async Task R1_Shorten_ReturnsCreatedWithCode()
    {
        var response = await _client.PostAsJsonAsync("/shorten", new { url = "https://example.com" });
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
    }

    [Fact]
    public async Task R2_UnknownCode_ReturnsNotFound()
    {
        var response = await _client.GetAsync("/nope");
        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }
}
"""

DOTNET_CODE = bundle({"src/App/App.csproj": DOTNET_APP_CSPROJ, "src/App/Program.cs": DOTNET_PROGRAM})
DOTNET_TESTS = bundle({"tests/Tests.csproj": DOTNET_TESTS_CSPROJ, "tests/ShortenerTests.cs": DOTNET_TEST})
DOTNET_ANSWERS: dict[str, dict[str, Any]] = {**ANSWERS, "developer": DOTNET_CODE, "tester": DOTNET_TESTS}
