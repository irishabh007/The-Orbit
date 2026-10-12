# URL Shortener Service

High-performance, secure URL shortener service built with ASP.NET Core minimal APIs on .NET 10 and Entity Framework Core with SQLite.

## Purpose

The service provisions shortened URLs with either auto-generated Base62 codes or custom aliases, supports optional ISO-8601 expirations, issues HTTP 307 temporary redirects to keep click metrics accurate, records real-time analytics, performs operational health checks, and defends against SSRF and link creation abuse.

## Requirements

- [.NET 10 SDK](https://dotnet.microsoft.com/download)
- SQLite runtime support (embedded / file-based)

## Quick Start

### 1. Clone & Restore

```bash
git clone <repo-url>
cd url-shortener
dotnet restore
```

### 2. Run the Service

```bash
dotnet run --project src/App
```

By default, the server listens on `http://localhost:5000` (or the configured `ASPNETCORE_URLS`). EF Core database migrations apply automatically on startup, initializing the SQLite database file.

### 3. Run Automated Tests

```bash
dotnet test tests/Tests.csproj
```

The test suite exercises:
- Auto-generated code and custom alias creation via `POST /shorten`
- SSRF blocking on private/loopback/non-HTTP schemes
- Alias collision conflicts (`HTTP 409`)
- Redirections (`HTTP 307`) and click count increments via `GET /{code}`
- Link expiration enforcement (`HTTP 404` on redirect while retaining analytics)
- Analytics reporting via `GET /analytics/{code}`
- Service & database health status via `GET /health`
- Per-IP rate limiting (`HTTP 429` with `Retry-After`)
- Persistence across host restarts

## Configuration

Configuration is managed via ASP.NET Core configuration providers (`appsettings.json`, environment variables, command-line arguments).

| Setting Key | Default / Example | Purpose |
|---|---|---|
| `ConnectionStrings:DefaultConnection` | `Data Source=app.db;Cache=Shared;Mode=ReadWriteCreate` | SQLite connection string. Configured with Shared cache and ReadWriteCreate mode for WAL concurrency. |
| `RateLimiting:PermitLimit` | `60` *(per spec assumption / proposed default Q3)* | Maximum requests allowed per rate-limiting window. |
| `RateLimiting:WindowSeconds` | `60` *(per spec assumption / proposed default Q3)* | Duration in seconds of the rate-limiting window per client IP. |
| `ForwardedHeaders:KnownNetworks` | `[]` | Trusted reverse proxy networks for validating `X-Forwarded-For` client IP resolution. |

*Note on rate limiting configuration:* The spec documents a proposed default of 60 requests per minute per IP address (Q3). The exact configuration keys are subject to implementation in `RateLimitingConfiguration.cs`.

## API Summary

All timestamps use ISO-8601 UTC format (`YYYY-MM-DDTHH:mm:ssZ`).

| Method | Endpoint | Description | Status Codes |
|---|---|---|---|
| `POST` | `/shorten` | Create short link (auto-generated or custom alias, optional expiry) | `201`, `400`, `409`, `429` |
| `GET` | `/{code}` | Redirect to original target URL; increments click count | `307`, `404` |
| `GET` | `/analytics/{code}` | Retrieve click statistics and timestamps | `200`, `404` |
| `GET` | `/health` | Check health of service and backing SQLite database | `200`, `503` |

### Quick API Examples

#### Create a Short URL (Auto-Generated Code)

```bash
curl -i -X POST http://localhost:5000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/very/long/path"}'
```

**Response (`201 Created`):**
```json
{
  "code": "aZ91kL2",
  "short_url": "http://localhost:5000/aZ91kL2"
}
```

#### Create with Custom Alias and Expiration

```bash
curl -i -X POST http://localhost:5000/shorten \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "custom_alias": "my-link",
    "expires_at": "2026-01-01T00:00:00Z"
  }'
```

**Response (`201 Created`):**
```json
{
  "code": "my-link",
  "short_url": "http://localhost:5000/my-link"
}
```

#### Resolve / Redirect

```bash
curl -i http://localhost:5000/my-link
```

**Response (`307 Temporary Redirect`):**
```http
HTTP/1.1 307 Temporary Redirect
Location: https://example.com
```

#### Fetch Analytics

```bash
curl -i http://localhost:5000/analytics/my-link
```

**Response (`200 OK`):**
```json
{
  "total_clicks": 1,
  "created_at": "2025-05-10T12:00:00Z",
  "last_visited_at": "2025-05-10T12:05:00Z"
}
```

#### Check Service Health

```bash
curl -i http://localhost:5000/health
```

**Response (`200 OK`):**
```json
{
  "status": "healthy"
}
```

## Further Documentation

- [Architecture & Design Decisions](docs/architecture.md)
- [API Specification & Reference](docs/api.md)