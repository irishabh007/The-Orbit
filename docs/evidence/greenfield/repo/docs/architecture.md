# Architecture Documentation

## Overview

The URL Shortener service is implemented in ASP.NET Core with minimal APIs targeting .NET 10. Persistence is handled by Entity Framework Core with an embedded SQLite database configured for WAL concurrency.

```
                    [ Client Request ]
                            │
                            ▼
               [ Rate Limiting Middleware ]
             (FixedWindow per RemoteIpAddress)
                            │
                            ▼
                  [ LinkEndpoints ]
       (POST /shorten, GET /{code}, GET /analytics, GET /health)
                            │
                            ▼
                     [ LinkService ]
              ┌─────────────┴─────────────┐
              ▼                           ▼
   [ UrlValidationService ]    [ CodeGeneratorService ]
   (SSRF / DNS checking)       (Base62 / Alias regex)
              │                           │
              └─────────────┬─────────────┘
                            ▼
                    [ AppDbContext ]
                 (EF Core SQLite Provider)
                            │
                            ▼
                     [ SQLite DB ]
                  (ShortLinks table)
```

## Component Breakdown

### 1. `Program` (`src/App/Program.cs`)
- Bootstraps dependency injection, SQLite connection, and rate limiter configuration.
- Applies EF Core migrations on application boot.
- Configures forward header options for trusted reverse proxies.
- Declares a public partial `Program` class enabling integration testing via `WebApplicationFactory`.

### 2. `Contracts` (`src/App/Models/Contracts.cs`)
- Defines strongly typed data transfer objects:
  - `ShortenRequest`: input model with `url`, optional `custom_alias`, and optional `expires_at`.
  - `ShortenResponse`: output model with `code` and `short_url`.
  - `AnalyticsResponse`: output model with `total_clicks`, `created_at`, and nullable `last_visited_at`.
  - `HealthResponse`: operational status model (`{"status": "healthy"}`).
  - `ErrorResponse`: error model returned on validation, conflict, or SSRF rejections.

### 3. `ShortLink` Entity (`src/App/Data/ShortLink.cs`)
Persisted domain entity mapped to SQLite:
- `Id` (`int`, Primary Key, autoincrement)
- `Code` (`string`, MaxLength 64, unique index, non-null)
- `OriginalUrl` (`string`, non-null)
- `CreatedAt` (`DateTime`, UTC, non-null)
- `ExpiresAt` (`DateTime?`, UTC, nullable)
- `ClickCount` (`long`, default 0, non-null)
- `LastVisitedAt` (`DateTime?`, UTC, nullable)

### 4. `AppDbContext` (`src/App/Data/AppDbContext.cs`)
- Configures EF Core model mappings.
- Enforces unique index constraint on `Code`.
- Ensures UTC conversion for all `DateTime` values.
- Manages database migrations and connection lifecycles.

### 5. `UrlValidationService` (`src/App/Services/UrlValidationService.cs`)
- Enforces scheme safety: rejects all schemes other than `http` and `https`.
- Prevents Server-Side Request Forgery (SSRF) before database persistence.
- Resolves target hostnames asynchronously (`Dns.GetHostAddressesAsync`).
- Rejects loopback addresses (`127.0.0.0/8`, `::1`), link-local addresses (`169.254.0.0/16`, `fe80::/10`), private networks (RFC 1918: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), and multicast/reserved ranges.

### 6. `CodeGeneratorService` (`src/App/Services/CodeGeneratorService.cs`)
- Generates 7-character Base62 alphanumeric codes (`[a-zA-Z0-9]`) using a cryptographically secure random number generator.
- Validates custom aliases against constraints: alphanumeric characters, dashes (`-`), and underscores (`_`) with length between 3 and 64 characters.

### 7. `LinkService` (`src/App/Services/LinkService.cs`)
- Business logic coordinator:
  - Validates URLs and SSRF safety via `UrlValidationService`.
  - Generates codes or validates custom aliases via `CodeGeneratorService`.
  - Detects duplicate aliases and surfaces conflict conditions (`HTTP 409`).
  - Evaluates link expiration on redirection requests.
  - Increments click counters atomically and sets `LastVisitedAt` to `DateTime.UtcNow`.
  - Queries link metrics for analytics reporting.

### 8. `RateLimitingConfiguration` (`src/App/Middleware/RateLimitingConfiguration.cs`)
- Implements ASP.NET Core's built-in `FixedWindowRateLimiter` partitioned by client `RemoteIpAddress`.
- Protects `POST /shorten` from abuse, returning `429 Too Many Requests` with a `Retry-After` header when thresholds are exceeded.

### 9. `LinkEndpoints` (`src/App/Endpoints/LinkEndpoints.cs`)
- Maps Minimal API route handlers:
  - `POST /shorten`
  - `GET /{code}`
  - `GET /analytics/{code}`
  - `GET /health`
- Performs parameter binding, applies rate limiting attributes, and handles HTTP status code responses.

### 10. Test Components (`tests/`)
- `CustomWebApplicationFactory`: Configures in-memory or isolated temporary SQLite database instances for E2E tests.
- `ShortenEndpointTests`: Validates code generation, alias collision (`409`), and SSRF blocks (`400`).
- `RedirectEndpointTests`: Validates redirects (`307`), missing/expired codes (`404`), and visit counter increments.
- `AnalyticsAndHealthTests`: Validates visit metrics correctness and `/health` reporting under normal and simulated failure conditions.
- `RateLimitingAndPersistenceTests`: Validates `429` rate limiting behaviors and persistence across host reboots.

## Architecture Decisions & Rationale

### D1: Use HTTP 307 Temporary Redirect for Link Resolution
- **Decision**: Return `HTTP 307 Temporary Redirect` with the destination URL in the `Location` header.
- **Rationale**: Browsers aggressively cache `HTTP 301 Moved Permanently`. If cached, subsequent clicks by the same client do not reach the server, causing undercounted click analytics. `HTTP 307` guarantees every visit hits the service and also preserves the original HTTP request method.
- **Alternatives Rejected**:
  - `HTTP 301 Moved Permanently`: Local client caching breaks click tracking.
  - `HTTP 302 Found`: Some legacy user agents incorrectly rewrite HTTP method verbs (e.g., rewriting `POST` to `GET`).

### D2: Synchronous DNS Resolution for Pre-Shorten SSRF Mitigation
- **Decision**: Resolve target hostnames via `Dns.GetHostAddressesAsync` during `POST /shorten` and block private/loopback IP ranges.
- **Rationale**: Prevents internal network reconnaissance, access to cloud metadata services (`169.254.169.254`), and local developer ports from being linked. Validation at link creation time ensures unsafe destinations are never stored.
- **Alternatives Rejected**:
  - String prefix / regex hostname matching: Does not protect against public domain names whose DNS records point to private or loopback IP addresses.
  - Validating at redirect time: Introduces redirect latency and leaves dangerous entries inside the database.

### D3: ASP.NET Core Built-in Partitioned Rate Limiter for Abuse Mitigation
- **Decision**: Use ASP.NET Core's native `RateLimiter` middleware configured with a `FixedWindowRateLimiter` partitioned by `RemoteIpAddress`.
- **Rationale**: Removes external dependencies (such as external Redis clusters) for rate limiting while providing native `429 Too Many Requests` responses and `Retry-After` header generation.
- **Alternatives Rejected**:
  - Third-party packages (e.g., `AspNetCoreRateLimit`): Adds external maintenance dependencies on .NET 10.
  - Custom dictionary middleware: Reinventing framework features that are already thread-safe and tested.

### D4: Retain Expired Link Metadata for Analytics Access
- **Decision**: Retain expired records in the SQLite database. Return `HTTP 404` for `GET /{code}` if `DateTime.UtcNow > ExpiresAt`, but allow `GET /analytics/{code}` to return historical metrics. Prevent reuse of expired aliases.
- **Rationale**: Satisfies requirement R4 (redirect returns 404 once expired) while preserving historical telemetry data and preventing alias hijacking.
- **Alternatives Rejected**:
  - Hard deletion of expired records: Erases historical analytics and allows short codes or custom aliases to be reclaimed by unauthorized parties.
  - Returning `HTTP 410 Gone` on `GET /{code}`: Requirement R4 acceptance criteria strictly mandates `HTTP 404 Not Found` for expired links.

## Architectural Risks and Mitigations

1. **DNS Rebinding**: A public domain resolves to an external IP at shortening, but resolves to a private IP later.
   - *Mitigation*: The service issues an HTTP redirect rather than making outbound HTTP requests on the client's behalf; the service never fetches target contents directly.
2. **SQLite Write Lock Contention**:
   - *Mitigation*: SQLite is configured in WAL (Write-Ahead Logging) mode using `Data Source=app.db;Cache=Shared;Mode=ReadWriteCreate` with atomic click updates.
3. **Client IP Spoofing Behind Reverse Proxies**:
   - *Mitigation*: Configure `ForwardedHeadersOptions` with explicit known proxies/networks to sanitize the `X-Forwarded-For` header.