# URL Shortener Service

High-performance URL shortener service built with ASP.NET Core Minimal APIs on .NET 10, Entity Framework Core 9, and SQLite.

## Purpose

The service generates shortened URLs with random Base62 identifiers or custom aliases, protects against Server-Side Request Forgery (SSRF), enforces optional ISO-8601 UTC expiration dates, issues HTTP 307 temporary redirects, records lifetime and contiguous daily visit counts, and applies fixed-window client IP rate limiting on link generation.

Key persistence and concurrency features:
- **Atomic lifetime counting**: Updates click counters and last visited timestamps directly in SQLite via EF Core `ExecuteUpdateAsync` without in-memory mutations.
- **Atomic daily upserts**: Inserts or increments calendar-day click counts via SQLite `INSERT ... ON CONFLICT(ShortLinkId, Date) DO UPDATE` queries.
- **Migration lifecycle & legacy bootstrap**: Replaces `EnsureCreatedAsync` with a handwritten EF Core initial migration (`20260101000000_InitialCreate`) and an automated bootstrapper that upgrades pre-existing SQLite databases within an explicit transaction prior to executing `Database.MigrateAsync()`.

## Quick Start

### Prerequisites
- [.NET 10 SDK](https://dotnet.microsoft.com/download) (NET 10.0)
- File-system access for SQLite storage

### 1. Build and Restore

```bash
git clone <repo-url>
cd url-shortener
dotnet restore
```

### 2. Run the Service

```bash
dotnet run --project src/App
```

On startup, the host creates a service scope and invokes `DatabaseBootstrapper.BootstrapAsync`. It configures SQLite WAL mode, sets a 30-second busy timeout, runs legacy schema migration checks if a pre-existing `ShortLinks` table is detected, and applies registered migrations via `Database.MigrateAsync()`.

### 3. Run Automated Tests

```bash
dotnet test tests/Tests.csproj
```

The test suite runs integration and unit tests covering:
- Link creation, custom alias validation, duplicate collisions (`409 Conflict`), and SSRF network validation (`400 Bad Request`).
- Atomic lifetime increment and last-visited timestamp updates on redirect (`GET /{code}`).
- Atomic daily upsert recording first-time visits and incrementing subsequent clicks.
- Parallel execution of 100 concurrent redirects ensuring exact click counts across lifetime and daily tables without lost updates.
- Legacy database bootstrapping: upgrading databases provisioned under older schemas and incrementing existing link clicks without data loss.
- Lifetime analytics (`GET /analytics/{code}`) and contiguous zero-filled daily analytics (`GET /analytics/{code}/daily`).
- Fixed-window rate limiting on link creation (`429 Too Many Requests` with `Retry-After`).
- Health probe verification (`GET /health`).

## Configuration

Application settings are loaded from `appsettings.json`, environment variables, or command-line parameters.

| Setting | Default | Description |
|---|---|---|
| `ConnectionStrings:DefaultConnection` | `Data Source=shortener.db;Cache=Shared;Mode=ReadWriteCreate;Default Timeout=30` | SQLite database connection string. Enables shared cache and a 30-second busy timeout. |
| `RateLimiting:PermitLimit` | `60` | Maximum link creation requests permitted per window per client IP. |
| `RateLimiting:WindowSeconds` | `60` | Fixed rate limiting window duration in seconds. |
| `RateLimiting:QueueLimit` | `0` | Rate limiter queue capacity when the permit limit is reached. |

Client IP addresses for rate limiting are parsed from the first comma-separated entry of the `X-Forwarded-For` HTTP header, falling back to `HttpContext.Connection.RemoteIpAddress`.

## API Summary

All timestamps use ISO-8601 UTC format (`YYYY-MM-DDTHH:mm:ssZ`) and daily analytics dates use ISO-8601 calendar date format (`YYYY-MM-DD`).

| Method | Endpoint | Summary | Status Codes |
|---|---|---|---|
| `POST` | `/shorten` | Create a shortened URL with optional custom alias and expiration date. | `201`, `400`, `409`, `429` |
| `GET` | `/{code}` | Redirect to the target URL; atomically increments lifetime and daily counters. | `307`, `404` |
| `GET` | `/analytics/{code}` | Retrieve lifetime visit metrics, creation date, and last visited timestamp. | `200`, `404` |
| `GET` | `/analytics/{code}/daily` | Retrieve contiguous, zero-filled daily click metrics over a UTC window (default 7 days, max 90). | `200`, `400`, `404` |
| `GET` | `/health` | Check service health and database query readiness. | `200`, `503` |

### API Examples

#### Create a Short Link

```bash
curl -i -X POST http://localhost:5000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/target-page", "custom_alias": "promo2026"}'
```

**Response (`201 Created`):**
```json
{
  "code": "promo2026",
  "short_url": "http://localhost:5000/promo2026",
  "url": "https://example.com/target-page",
  "created_at": "2026-03-30T12:00:00Z",
  "expires_at": null
}
```

#### Resolve Redirect

```bash
curl -i http://localhost:5000/promo2026
```

**Response (`307 Temporary Redirect`):**
```http
HTTP/1.1 307 Temporary Redirect
Location: https://example.com/target-page
```

#### Get Lifetime Analytics

```bash
curl -i http://localhost:5000/analytics/promo2026
```

**Response (`200 OK`):**
```json
{
  "code": "promo2026",
  "url": "https://example.com/target-page",
  "total_clicks": 1,
  "created_at": "2026-03-30T12:00:00Z",
  "last_visited_at": "2026-03-30T12:05:00Z",
  "expires_at": null,
  "expired": false
}
```

#### Get Daily Analytics

```bash
curl -i "http://localhost:5000/analytics/promo2026/daily?days=7"
```

**Response (`200 OK`):**
```json
{
  "code": "promo2026",
  "totalClicks": 1,
  "days": 7,
  "daily": [
    { "date": "2026-03-24", "clicks": 0 },
    { "date": "2026-03-25", "clicks": 0 },
    { "date": "2026-03-26", "clicks": 0 },
    { "date": "2026-03-27", "clicks": 0 },
    { "date": "2026-03-28", "clicks": 0 },
    { "date": "2026-03-29", "clicks": 0 },
    { "date": "2026-03-30", "clicks": 1 }
  ]
}
```

#### Health Check

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
- [API Reference](docs/api.md)