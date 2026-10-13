# Architecture & Design Decisions

## Overview

The URL Shortener is an ASP.NET Core Minimal API service running on .NET 10. Persistence is managed via Entity Framework Core 9 against an embedded SQLite database (`shortener.db`). The architecture eliminates race conditions during concurrent redirects and provides backwards-compatible database evolution from older `EnsureCreatedAsync`-provisioned databases.

```
                           [ HTTP Request ]
                                  │
                                  ▼
                    [ Rate Limiting Middleware ]
                    (FixedWindow on POST /shorten)
                                  │
                                  ▼
                         [ LinkEndpoints ]
       (POST /shorten, GET /{code}, GET /analytics/{code},
        GET /analytics/{code}/daily, GET /health)
                                  │
                                  ▼
                           [ LinkService ]
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
   [ UrlValidationService ] [ CodeGenerator ] [ Daily Zero-Filler ]
     (SSRF & DNS checks)     (Base62 & Regex)     (In-memory loop)
              │                   │                   │
              └───────────────────┼───────────────────┘
                                  ▼
                           [ AppDbContext ]
                                  │
                 ┌────────────────┴────────────────┐
                 ▼                                 ▼
       ExecuteUpdateAsync               ExecuteSqlInterpolatedAsync
   (ShortLinks.ClickCount + 1)           (INSERT ... ON CONFLICT)
                 │                                 │
                 └────────────────┬────────────────┘
                                  ▼
                            [ SQLite DB ]
                     (PRAGMA journal_mode = WAL)
                    (PRAGMA busy_timeout = 30000)
             ┌────────────────────┴────────────────────┐
             ▼                                         ▼
       [ ShortLinks ]                        [ ShortLinkDailyClicks ]
  (Lifetime counter & URL)              (Unique: ShortLinkId, Date)
```

## Component Breakdown

### 1. `Program` (`src/App/Program.cs`)
- Configures dependency injection, EF Core SQLite connection string, rate limiting, and minimal API routing.
- On startup, resolves `AppDbContext` in a scope and runs `DatabaseBootstrapper.BootstrapAsync` before request listening begins.
- Exports `public partial class Program { }` to enable in-process integration testing via `WebApplicationFactory<Program>`.

### 2. `DatabaseBootstrapper` (`src/App/Data/DatabaseBootstrapper.cs`)
- Replaces direct calls to `EnsureCreatedAsync` with managed EF Core migration initialization.
- Verifies relational provider: returns immediately without error if `!dbContext.Database.IsRelational()`.
- When running against SQLite:
  - Ensures the connection is open.
  - Sets SQLite PRAGMAs: `PRAGMA journal_mode = WAL;` and `PRAGMA busy_timeout = 30000;` (exceptions suppressed).
  - Inspects `sqlite_master` for an existing `ShortLinks` table.
  - If `ShortLinks` exists, inspects `PRAGMA table_info('ShortLinks')` and begins an explicit SQLite transaction:
    - Renames legacy column names if present (`CreatedAt` -> `CreatedAtUtc`, `ExpiresAt` -> `ExpiresAtUtc`, `LastVisitedAt` -> `LastVisitedAtUtc`).
    - Adds missing nullable columns (`LastVisitedAtUtc`, `ExpiresAtUtc`) and `ClickCount` if absent.
    - Executes `CREATE TABLE IF NOT EXISTS "ShortLinkDailyClicks"` with foreign keys referencing `ShortLinks(Id)`.
    - Executes `CREATE UNIQUE INDEX IF NOT EXISTS "IX_ShortLinkDailyClicks_ShortLinkId_Date"` on `(ShortLinkId, Date)`.
    - Executes `CREATE TABLE IF NOT EXISTS "__EFMigrationsHistory"` (`MigrationId TEXT PRIMARY KEY`, `ProductVersion TEXT`).
    - Executes `INSERT OR IGNORE INTO "__EFMigrationsHistory" ("MigrationId", "ProductVersion") VALUES ('20260101000000_InitialCreate', '9.0.2')`.
    - Commits the transaction.
- Finally, calls `dbContext.Database.MigrateAsync()` to apply any pending EF Core migrations.

### 3. `InitialCreate` Migration (`src/App/Data/Migrations/InitialCreate.cs`)
- Handwritten EF Core migration inheriting from `Migration`, annotated with `[DbContext(typeof(AppDbContext))]` and `[Migration("20260101000000_InitialCreate")]`.
- Enables migration discovery and execution via `Database.MigrateAsync()` without requiring the `dotnet-ef` CLI tool in production environments.
- Defines tables `ShortLinks` and `ShortLinkDailyClicks`, indices (`IX_ShortLinks_Code`, `IX_ShortLinks_ExpiresAtUtc`, `IX_ShortLinkDailyClicks_ShortLinkId_Date`), and cascading foreign key deletion.

### 4. `AppDbContext` (`src/App/Data/AppDbContext.cs`)
- Maps entities `ShortLink` and `ShortLinkDailyClick` (also exposed through property alias `DailyClicks`).
- Configures value converters enforcing UTC `DateTimeKind` on `CreatedAtUtc`, `LastVisitedAtUtc`, and `ExpiresAtUtc`.
- Configures indices and constraints:
  - Unique index on `ShortLink.Code`.
  - Index on `ShortLink.ExpiresAtUtc`.
  - Unique composite index on `ShortLinkDailyClick(ShortLinkId, Date)`.

### 5. `LinkService` (`src/App/Services/LinkService.cs`)
- Implements link creation, redirection, aggregate analytics, and contiguous daily analytics.
- **Atomic Redirect Workflow (`GetRedirectAsync`)**:
  1. Queries code existence, URL, and expiration using `AsNoTracking()` without entity tracking.
  2. Returns `RedirectStatus.NotFound` if missing, or `RedirectStatus.Expired` if `ExpiresAtUtc <= DateTime.UtcNow`.
  3. Atomically increments `ClickCount` and updates `LastVisitedAtUtc` via `ExecuteUpdateAsync`:
     ```csharp
     await _dbContext.ShortLinks
         .Where(s => s.Id == link.Id)
         .ExecuteUpdateAsync(s => s
             .SetProperty(b => b.ClickCount, b => b.ClickCount + 1)
             .SetProperty(b => b.LastVisitedAtUtc, nowUtc),
             cancellationToken);
     ```
  4. Atomically upserts daily click counts using raw SQLite SQL:
     ```sql
     INSERT INTO ShortLinkDailyClicks (ShortLinkId, Date, Clicks)
     VALUES ({link.Id}, {dateString}, 1)
     ON CONFLICT(ShortLinkId, Date) DO UPDATE SET Clicks = ShortLinkDailyClicks.Clicks + 1;
     ```
  5. Retries both write operations via `ExecuteWithRetryAsync` up to 20 times upon detecting SQLite busy or locked errors (`SqliteErrorCode` 5 or 6) using randomized progressive backoff delays (`RandomNumberGenerator.GetInt32(10, 50) * (attempt + 1)` ms).
- **Daily Analytics Construction (`GetDailyAnalyticsAsync`)**:
  1. Validates that `days` is between 1 and 90.
  2. Computes the date range `[TodayUtc - (days - 1), TodayUtc]`.
  3. Queries `ShortLinkDailyClicks` within the range and aggregates existing clicks per date in memory.
  4. Iterates through each date in the window to produce a contiguous list of `DailyClickRecord`, zero-filling dates without recorded clicks.

### 6. `UrlValidationService` (`src/App/Services/UrlValidationService.cs`)
- Validates absolute URI structure and enforces `http` or `https` schemes.
- Mitigates SSRF attacks by checking target hosts against loopback, RFC 1918 private IPv4 addresses, link-local, carrier-grade NAT (`100.64.0.0/10`), documentation subnets, broadcast, and IPv6 loopback/link-local/multicast/site-local ranges.
- Performs DNS resolution (`Dns.GetHostAddressesAsync`) to inspect resolved IP addresses.

### 7. `CodeGeneratorService` (`src/App/Services/CodeGeneratorService.cs`)
- Generates random 7-character Base62 strings via `RandomNumberGenerator.GetString(...)`.
- Validates custom aliases against regex `^[a-zA-Z0-9_-]{3,64}$`.

### 8. `RateLimitingConfiguration` (`src/App/Middleware/RateLimitingConfiguration.cs`)
- Configures ASP.NET Core `FixedWindowRateLimiter` named `ShortenPolicy` applied exclusively to `POST /shorten`.
- Extracts client IP address from `X-Forwarded-For` or connection remote IP.
- Rejects requests exceeding quota with status 429 and sets the `Retry-After` header.

## Architectural Decisions & Rationale

### D1: Atomic Lifetime Click Increment via ExecuteUpdateAsync
- **Decision**: Update `ShortLinks.ClickCount` and `ShortLinks.LastVisitedAtUtc` directly in SQLite using EF Core 9 `ExecuteUpdateAsync`.
- **Rationale**: Eliminates race conditions and lost updates caused by in-memory read-modify-write patterns (`link.ClickCount += 1; await SaveChangesAsync();`). EF Core generates a direct SQL `UPDATE` statement executed by SQLite without entity tracking overhead.
- **Alternatives Rejected**:
  - *Read-modify-write with EF Core optimistic concurrency tokens (`ConcurrencyCheck` / `RowVersion`)*: Rejected because concurrent redirects would encounter concurrency conflicts and fail redirects for end users.
  - *Raw SQL UPDATE string*: Rejected because `ExecuteUpdateAsync` provides compile-time type safety while generating optimal atomic SQL.

### D2: Atomic Daily Analytics Upsert via Raw SQLite ON CONFLICT
- **Decision**: Execute raw SQL upsert query using `Database.ExecuteSqlInterpolatedAsync`:
  ```sql
  INSERT INTO ShortLinkDailyClicks (ShortLinkId, Date, Clicks)
  VALUES ({link.Id}, {dateString}, 1)
  ON CONFLICT(ShortLinkId, Date) DO UPDATE SET Clicks = ShortLinkDailyClicks.Clicks + 1;
  ```
- **Rationale**: SQLite natively supports `INSERT ... ON CONFLICT(...) DO UPDATE`. A single atomic query prevents race conditions and unique key collisions under concurrent redirects visiting on the same calendar day.
- **Alternatives Rejected**:
  - *Query first, then insert or update in C#*: Rejected due to inherent race conditions under concurrent requests causing duplicate key constraint violations (`IX_ShortLinkDailyClicks_ShortLinkId_Date`).
  - *Application-level `SemaphoreSlim` locking*: Rejected because process locks do not coordinate across multiple service replicas and introduce unnecessary latency.

### D3: Handwritten Initial EF Core Migration without dotnet-ef CLI
- **Decision**: Author `InitialCreate.cs` manually with `[DbContext(typeof(AppDbContext))]` and `[Migration("20260101000000_InitialCreate")]`.
- **Rationale**: Production containers and CI/CD environments do not require the `dotnet-ef` CLI tool to be installed. EF Core discovers the migration class in the assembly at runtime and applies it via `Database.MigrateAsync()`.
- **Alternatives Rejected**:
  - *Requiring dotnet-ef CLI in container builds*: Rejected due to tool version dependencies and operational complexity in container pipelines.
  - *Retaining `Database.EnsureCreatedAsync()`*: Rejected because `EnsureCreatedAsync` bypasses EF Core migration tracking and cannot apply incremental schema changes across versions.

### D4: Transactional Legacy Database Schema Bootstrap
- **Decision**: Before executing `Database.MigrateAsync()`, `DatabaseBootstrapper` checks `sqlite_master`. If legacy tables exist without migration history, it applies necessary DDL (`CREATE TABLE IF NOT EXISTS`, column renames), creates `__EFMigrationsHistory`, and inserts the `InitialCreate` migration identifier within an explicit database transaction.
- **Rationale**: Databases provisioned under prior releases using `EnsureCreatedAsync` lack `__EFMigrationsHistory` and `ShortLinkDailyClicks`. Calling `MigrateAsync()` on such databases would fail with "table 'ShortLinks' already exists". The bootstrapper seeds the initial migration entry so `MigrateAsync()` treats the initial schema as already applied while ensuring any missing structures are created.
- **Alternatives Rejected**:
  - *Calling MigrateAsync directly and catching SQLite exceptions*: Rejected because partial migration failures leave the database in an inconsistent or unrecoverable state.
  - *Manual offline SQL scripts*: Rejected because the service must self-heal and upgrade autonomously across container restarts.

## Implementation Observations & Differences

1. **Separate Retry Blocks during Redirect**: During redirect handling in `LinkService.GetRedirectAsync`, the lifetime click counter update (`ExecuteUpdateAsync`) and the daily click upsert (`ExecuteSqlInterpolatedAsync`) are executed as two distinct calls to `ExecuteWithRetryAsync`, rather than wrapped in a single multi-statement database transaction. Both operations independently retry up to 20 times upon SQLite busy/locked errors.
2. **Non-relational Database Behavior**: In `DatabaseBootstrapper.BootstrapAsync`, if `!dbContext.Database.IsRelational()` (such as when running with in-memory test providers), the method exits immediately without calling `EnsureCreatedAsync` or `MigrateAsync`.