# Hardening: fix two production defects in the URL shortener

The URL shortener with daily click analytics is in production (the existing code is provided).
A code review found two defects that the existing tests do not catch, because every test starts
from a fresh database.

Defect 1: lost click counts
- Redirects increment `ShortLink.ClickCount` and the daily counter by reading the row, adding 1
  in memory and saving (`link.ClickCount += 1`, `dailyClick.Clicks += 1`). Concurrent redirects of
  the same link can lose increments. The docs claim the update is atomic; it is not.
- Required: each counter update is a single atomic database statement. Use
  `ExecuteUpdateAsync(s => s.SetProperty(l => l.ClickCount, l => l.ClickCount + 1))` for the link,
  and an atomic upsert for the daily row (SQLite `INSERT ... ON CONFLICT(...) DO UPDATE SET
  Clicks = Clicks + 1`, for example through `ExecuteSqlInterpolatedAsync`). No in-memory `+= 1`.

Defect 2: existing databases never get new tables
- Startup calls `Database.EnsureCreatedAsync()`, which creates a database only when none exists
  and never changes an existing one. A database created before the daily analytics release has no
  `ShortLinkDailyClicks` table, so the daily analytics endpoint fails after an upgrade.
- Required: manage the schema with EF Core migrations and call `Database.MigrateAsync()` at
  startup; remove `EnsureCreated`. Provide an initial migration for the full current schema (the
  migration class with its `[DbContext]` and `[Migration]` attributes, written by hand: the
  `dotnet ef` tool is not available).
- Upgrade path for databases created by `EnsureCreated` (they have tables but no
  `__EFMigrationsHistory`): before migrating, create any missing table with
  `CREATE TABLE IF NOT EXISTS` matching the model, create `__EFMigrationsHistory` if needed and
  record the initial migration as applied, then call `MigrateAsync()`.

Tests
- A concurrency test: many parallel redirects of one link; the total and today's daily count
  must equal the number of redirects.
- An upgrade test: create a SQLite database with the first release's schema (only the
  `ShortLinks` table, as `EnsureCreated` made it, with one existing link), start the app on it,
  and check that the link still redirects, its total click count is kept, and daily analytics work.
- All existing tests must keep passing.

Constraints
- Every endpoint keeps its current behaviour and response format.
- Existing data must not be lost.
- Update the docs so every claim matches the code (atomicity, migrations, upgrade path).
