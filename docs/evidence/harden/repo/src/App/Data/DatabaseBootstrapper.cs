using System;
using System.Collections.Generic;
using Microsoft.EntityFrameworkCore;

namespace App.Data;

public static class DatabaseBootstrapper
{
    public const string InitialMigrationId = "20260101000000_InitialCreate";
    public const string ProductVersion = "9.0.2";

    public static async Task BootstrapDatabaseAsync(AppDbContext dbContext, CancellationToken cancellationToken = default)
    {
        await BootstrapAsync(dbContext, cancellationToken);
    }

    public static async Task BootstrapAsync(AppDbContext dbContext, CancellationToken cancellationToken = default)
    {
        if (!dbContext.Database.IsRelational())
        {
            return;
        }

        if (dbContext.Database.IsSqlite())
        {
            var connection = dbContext.Database.GetDbConnection();
            if (connection.State != System.Data.ConnectionState.Open)
            {
                await dbContext.Database.OpenConnectionAsync(cancellationToken);
            }

            try
            {
                await dbContext.Database.ExecuteSqlRawAsync("PRAGMA journal_mode = WAL;", cancellationToken);
            }
            catch
            {
            }

            try
            {
                await dbContext.Database.ExecuteSqlRawAsync("PRAGMA busy_timeout = 30000;", cancellationToken);
            }
            catch
            {
            }

            bool hasShortLinks = false;
            using (var cmd = connection.CreateCommand())
            {
                cmd.CommandText = "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='ShortLinks';";
                var scalar = await cmd.ExecuteScalarAsync(cancellationToken);
                if (scalar != null && Convert.ToInt64(scalar) > 0)
                { 
                    hasShortLinks = true;
                }
            }

            if (hasShortLinks)
            {
                var columns = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                using (var cmd = connection.CreateCommand())
                {
                    cmd.CommandText = "PRAGMA table_info('ShortLinks');";
                    using var reader = await cmd.ExecuteReaderAsync(cancellationToken);
                    while (await reader.ReadAsync(cancellationToken))
                    {
                        columns.Add(reader.GetString(1));
                    }
                }

                await using var transaction = await dbContext.Database.BeginTransactionAsync(cancellationToken);

                if (columns.Contains("CreatedAt") && !columns.Contains("CreatedAtUtc"))
                {
                    await dbContext.Database.ExecuteSqlRawAsync("ALTER TABLE \"ShortLinks\" RENAME COLUMN \"CreatedAt\" TO \"CreatedAtUtc\";", cancellationToken);
                }
                if (columns.Contains("ExpiresAt") && !columns.Contains("ExpiresAtUtc"))
                {
                    await dbContext.Database.ExecuteSqlRawAsync("ALTER TABLE \"ShortLinks\" RENAME COLUMN \"ExpiresAt\" TO \"ExpiresAtUtc\";", cancellationToken);
                }
                if (columns.Contains("LastVisitedAt") && !columns.Contains("LastVisitedAtUtc"))
                {
                    await dbContext.Database.ExecuteSqlRawAsync("ALTER TABLE \"ShortLinks\" RENAME COLUMN \"LastVisitedAt\" TO \"LastVisitedAtUtc\";", cancellationToken);
                }

                if (!columns.Contains("LastVisitedAtUtc") && !columns.Contains("LastVisitedAt"))
                {
                    await dbContext.Database.ExecuteSqlRawAsync("ALTER TABLE \"ShortLinks\" ADD COLUMN \"LastVisitedAtUtc\" TEXT NULL;", cancellationToken);
                }

                if (!columns.Contains("ExpiresAtUtc") && !columns.Contains("ExpiresAt"))
                {
                    await dbContext.Database.ExecuteSqlRawAsync("ALTER TABLE \"ShortLinks\" ADD COLUMN \"ExpiresAtUtc\" TEXT NULL;", cancellationToken);
                }

                if (!columns.Contains("ClickCount"))
                {
                    await dbContext.Database.ExecuteSqlRawAsync("ALTER TABLE \"ShortLinks\" ADD COLUMN \"ClickCount\" INTEGER NOT NULL DEFAULT 0;", cancellationToken);
                }

                await dbContext.Database.ExecuteSqlRawAsync(@"
                    CREATE TABLE IF NOT EXISTS ""ShortLinkDailyClicks"" (
                        ""Id"" INTEGER NOT NULL CONSTRAINT ""PK_ShortLinkDailyClicks"" PRIMARY KEY AUTOINCREMENT,
                        ""ShortLinkId"" INTEGER NOT NULL,
                        ""Date"" TEXT NOT NULL,
                        ""Clicks"" INTEGER NOT NULL DEFAULT 0,
                        CONSTRAINT ""FK_ShortLinkDailyClicks_ShortLinks_ShortLinkId"" FOREIGN KEY (""ShortLinkId"") REFERENCES ""ShortLinks"" (""Id"") ON DELETE CASCADE
                    );
                ", cancellationToken);

                await dbContext.Database.ExecuteSqlRawAsync(@"
                    CREATE UNIQUE INDEX IF NOT EXISTS ""IX_ShortLinkDailyClicks_ShortLinkId_Date"" ON ""ShortLinkDailyClicks"" (""ShortLinkId"", ""Date"");
                ", cancellationToken);

                await dbContext.Database.ExecuteSqlRawAsync(@"
                    CREATE TABLE IF NOT EXISTS ""__EFMigrationsHistory"" (
                        ""MigrationId"" TEXT NOT NULL CONSTRAINT ""PK___EFMigrationsHistory"" PRIMARY KEY,
                        ""ProductVersion"" TEXT NOT NULL
                    );
                ", cancellationToken);

                await dbContext.Database.ExecuteSqlInterpolatedAsync($@"
                    INSERT OR IGNORE INTO ""__EFMigrationsHistory"" (""MigrationId"", ""ProductVersion"")
                    VALUES ({InitialMigrationId}, {ProductVersion});
                ", cancellationToken);

                await transaction.CommitAsync(cancellationToken);
            }
        }

        await dbContext.Database.MigrateAsync(cancellationToken);
    }
}