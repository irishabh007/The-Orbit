using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text.Json;
using System.Threading.Tasks;
using Xunit;

namespace Tests;

public class RateLimitingAndPersistenceTests
{
    [Fact]
    public async Task R3_Startup_CleanDatabase_AppliesInitialMigrationAndRecordsInHistory()
    { 
        var cleanDbPath = Path.Combine(Path.GetTempPath(), $"clean_{Guid.NewGuid():N}.db");
        try
        {
            using (var factory = new CustomWebApplicationFactory(cleanDbPath, deleteDbOnDispose: false))
            {
                var client = factory.CreateClient();
                var health = await client.GetAsync("/health");
                Assert.Equal(HttpStatusCode.OK, health.StatusCode);
            }

            using (var connection = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={cleanDbPath};Default Timeout=30"))
            {
                await connection.OpenAsync();
                using var cmd = connection.CreateCommand();
                cmd.CommandText = "SELECT MigrationId FROM __EFMigrationsHistory;";
                using var reader = await cmd.ExecuteReaderAsync();
                Assert.True(await reader.ReadAsync());
                var migrationId = reader.GetString(0);
                Assert.Contains("InitialCreate", migrationId);
            }
        }
        finally
        {
            Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
            try { if (File.Exists(cleanDbPath)) File.Delete(cleanDbPath); } catch { }
            try { if (File.Exists(cleanDbPath + "-wal")) File.Delete(cleanDbPath + "-wal"); } catch { }
            try { if (File.Exists(cleanDbPath + "-shm")) File.Delete(cleanDbPath + "-shm"); } catch { }
        }
    }

    [Fact]
    public async Task R4_LegacyDatabase_BootstrapsMissingTablesAndMigrationHistory()
    {
        var legacyDbPath = Path.Combine(Path.GetTempPath(), $"bootstrap_{Guid.NewGuid():N}.db");
        try
        {
            // Create legacy SQLite database containing only ShortLinks table (no __EFMigrationsHistory or ShortLinkDailyClicks)
            using (var connection = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={legacyDbPath};Default Timeout=30"))
            {
                await connection.OpenAsync();
                using var cmd = connection.CreateCommand();
                cmd.CommandText = @"
                    CREATE TABLE ShortLinks (
                        Id INTEGER PRIMARY KEY AUTOINCREMENT,
                        Code TEXT NOT NULL UNIQUE,
                        OriginalUrl TEXT NOT NULL,
                        ClickCount INTEGER NOT NULL,
                        CreatedAt TEXT NULL DEFAULT CURRENT_TIMESTAMP,
                        CreatedAtUtc TEXT NULL,
                        LastVisitedAt TEXT NULL,
                        LastVisitedAtUtc TEXT NULL,
                        ExpiresAt TEXT NULL,
                        ExpiresAtUtc TEXT NULL
                    );
                ";
                await cmd.ExecuteNonQueryAsync();
            }

            // Start host; bootstrapper should execute CREATE TABLE IF NOT EXISTS and seed __EFMigrationsHistory
            using (var factory = new CustomWebApplicationFactory(legacyDbPath, deleteDbOnDispose: false))
            {
                var client = factory.CreateClient();
                var healthResponse = await client.GetAsync("/health");
                Assert.Equal(HttpStatusCode.OK, healthResponse.StatusCode);
            }

            // Verify SQLite state: ShortLinkDailyClicks created and migration recorded in __EFMigrationsHistory
            using (var connection = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={legacyDbPath};Default Timeout=30"))
            {
                await connection.OpenAsync();
                using var cmd = connection.CreateCommand();
                cmd.CommandText = "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='ShortLinkDailyClicks';";
                var dailyClicksCount = Convert.ToInt32(await cmd.ExecuteScalarAsync());
                Assert.Equal(1, dailyClicksCount);

                cmd.CommandText = "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='__EFMigrationsHistory';";
                var historyCount = Convert.ToInt32(await cmd.ExecuteScalarAsync());
                Assert.Equal(1, historyCount);

                cmd.CommandText = "SELECT COUNT(*) FROM __EFMigrationsHistory;";
                var migrationRows = Convert.ToInt32(await cmd.ExecuteScalarAsync());
                Assert.True(migrationRows >= 1);
            }

            // Verify subsequent application restart against upgraded database succeeds without error
            using (var subsequentFactory = new CustomWebApplicationFactory(legacyDbPath, deleteDbOnDispose: false))
            {
                var subsequentClient = subsequentFactory.CreateClient();
                var subsequentHealth = await subsequentClient.GetAsync("/health");
                Assert.Equal(HttpStatusCode.OK, subsequentHealth.StatusCode);
            }
        }
        finally
        {
            Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
            try { if (File.Exists(legacyDbPath)) File.Delete(legacyDbPath); } catch { }
            try { if (File.Exists(legacyDbPath + "-wal")) File.Delete(legacyDbPath + "-wal"); } catch { }
            try { if (File.Exists(legacyDbPath + "-shm")) File.Delete(legacyDbPath + "-shm"); } catch { }
        }
    }

    [Fact]
    public async Task R6_LegacyDatabase_WithPreExistingLink42Clicks_UpgradesAndIncrementsTo43Clicks()
    {
        var legacyDbPath = Path.Combine(Path.GetTempPath(), $"legacy_link_{Guid.NewGuid():N}.db");
        try
        {
            // Provision initial release schema with one link having ClickCount = 42
            using (var connection = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={legacyDbPath};Default Timeout=30"))
            {
                await connection.OpenAsync();
                using var cmd = connection.CreateCommand();
                cmd.CommandText = @"
                    CREATE TABLE ShortLinks (
                        Id INTEGER PRIMARY KEY AUTOINCREMENT,
                        Code TEXT NOT NULL UNIQUE,
                        OriginalUrl TEXT NOT NULL,
                        ClickCount INTEGER NOT NULL,
                        CreatedAt TEXT NULL DEFAULT CURRENT_TIMESTAMP,
                        CreatedAtUtc TEXT NULL,
                        LastVisitedAt TEXT NULL,
                        LastVisitedAtUtc TEXT NULL,
                        ExpiresAt TEXT NULL,
                        ExpiresAtUtc TEXT NULL
                    );
                    INSERT INTO ShortLinks (Code, OriginalUrl, ClickCount, CreatedAt, CreatedAtUtc)
                    VALUES ('legacy42', 'https://example.com/legacy-dest', 42, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z');
                ";
                await cmd.ExecuteNonQueryAsync();
            }

            // Start service against this legacy database
            using var factory = new CustomWebApplicationFactory(legacyDbPath, deleteDbOnDispose: false);
            var client = factory.CreateClient();
            var redirectClient = factory.CreateNoRedirectClient();

            // GET /{code} returns HTTP 307 and increments ClickCount to 43
            var redirectResponse = await redirectClient.GetAsync("/legacy42");
            Assert.Equal(HttpStatusCode.TemporaryRedirect, redirectResponse.StatusCode);
            Assert.Equal("https://example.com/legacy-dest", redirectResponse.Headers.Location?.ToString());

            var analyticsResponse = await client.GetAsync("/analytics/legacy42");
            Assert.Equal(HttpStatusCode.OK, analyticsResponse.StatusCode);
            using var analyticsDoc = JsonDocument.Parse(await analyticsResponse.Content.ReadAsStringAsync());
            Assert.Equal(43, JsonTestHelper.GetInt64Property(analyticsDoc.RootElement, "total_clicks", "totalClicks"));

            // GET /analytics/{code}/daily returns HTTP 200 with recorded daily clicks
            var dailyResponse = await client.GetAsync("/analytics/legacy42/daily?days=1");
            Assert.Equal(HttpStatusCode.OK, dailyResponse.StatusCode);
            using var dailyDoc = JsonDocument.Parse(await dailyResponse.Content.ReadAsStringAsync());
            var dailyArray = JsonTestHelper.GetArrayProperty(dailyDoc.RootElement, "daily");
            Assert.NotNull(dailyArray);
            Assert.Equal(1, dailyArray.Value.GetArrayLength());
            Assert.Equal(1, JsonTestHelper.GetInt64Property(dailyArray.Value[0], "clicks"));
        }
        finally
        {
            Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
            try { if (File.Exists(legacyDbPath)) File.Delete(legacyDbPath); } catch { }
            try { if (File.Exists(legacyDbPath + "-wal")) File.Delete(legacyDbPath + "-wal"); } catch { }
            try { if (File.Exists(legacyDbPath + "-shm")) File.Delete(legacyDbPath + "-shm"); } catch { }
        }
    }

    [Fact]
    public async Task R7_RateLimiting_WithinThreshold_ReturnsCreated201()
    {
        using var factory = new CustomWebApplicationFactory();
        var client = factory.CreateClient();
        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/rate-under-limit" };

        var response = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
    }

    [Fact]
    public async Task R7_RateLimiting_ExceedingThreshold_ReturnsTooManyRequests429WithRetryAfter()
    {
        using var factory = new CustomWebApplicationFactory(null, permitLimit: 5);
        var client = factory.CreateClient();
        client.DefaultRequestHeaders.Add("X-Forwarded-For", "198.51.100.25");

        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/rate-spam" };
        HttpResponseMessage? limitedResponse = null;

        for (int i = 0; i < 70; i++)
        {
            var response = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
            if (response.StatusCode == HttpStatusCode.TooManyRequests)
            {
                limitedResponse = response;
                break;
            }
        }

        Assert.NotNull(limitedResponse);
        Assert.Equal(HttpStatusCode.TooManyRequests, limitedResponse.StatusCode);
        Assert.True(
            limitedResponse.Headers.Contains("Retry-After") ||
            limitedResponse.Headers.RetryAfter != null ||
            limitedResponse.Content.Headers.Contains("Retry-After"),
            "Expected Retry-After header on 429 response."
        );
    }

    [Fact]
    public async Task R7_Persistence_SurvivesServiceRestartAndPreservesPriorLinks()
    {
        var sharedDbPath = Path.Combine(Path.GetTempPath(), $"persist_{Guid.NewGuid():N}.db");
        var targetUrl = "https://example.com/persistent-destination";
        var customAlias = "persist-link-" + Guid.NewGuid().ToString("N")[..6];

        try
        {
            using (var hostInstance1 = new CustomWebApplicationFactory(sharedDbPath, deleteDbOnDispose: false))
            {
                var client1 = hostInstance1.CreateClient();
                var createPayload = new Dictionary<string, object?>
                {
                    ["url"] = targetUrl,
                    ["custom_alias"] = customAlias
                };

                var createResponse = await client1.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(createPayload));
                Assert.Equal(HttpStatusCode.Created, createResponse.StatusCode);

                var redirectClient1 = hostInstance1.CreateNoRedirectClient();
                var visit1 = await redirectClient1.GetAsync($"/{customAlias}");
                Assert.Equal(HttpStatusCode.TemporaryRedirect, visit1.StatusCode);

                var analytics1 = await client1.GetAsync($"/analytics/{customAlias}");
                using var doc1 = JsonDocument.Parse(await analytics1.Content.ReadAsStringAsync());
                Assert.Equal(1, JsonTestHelper.GetInt64Property(doc1.RootElement, "total_clicks", "totalClicks"));

                var daily1 = await client1.GetAsync($"/analytics/{customAlias}/daily?days=1");
                Assert.Equal(HttpStatusCode.OK, daily1.StatusCode);
            }

            using (var hostInstance2 = new CustomWebApplicationFactory(sharedDbPath, deleteDbOnDispose: false))
            {
                var client2 = hostInstance2.CreateClient();
                var analyticsBeforeSecondVisit = await client2.GetAsync($"/analytics/{customAlias}");
                Assert.Equal(HttpStatusCode.OK, analyticsBeforeSecondVisit.StatusCode);

                using var docBefore = JsonDocument.Parse(await analyticsBeforeSecondVisit.Content.ReadAsStringAsync());
                Assert.Equal(1, JsonTestHelper.GetInt64Property(docBefore.RootElement, "total_clicks", "totalClicks"));

                var redirectClient2 = hostInstance2.CreateNoRedirectClient();
                var visit2 = await redirectClient2.GetAsync($"/{customAlias}");

                Assert.Equal(HttpStatusCode.TemporaryRedirect, visit2.StatusCode);
                Assert.Equal(targetUrl, visit2.Headers.Location?.ToString());

                var analytics2 = await client2.GetAsync($"/analytics/{customAlias}");
                Assert.Equal(HttpStatusCode.OK, analytics2.StatusCode);

                using var doc2 = JsonDocument.Parse(await analytics2.Content.ReadAsStringAsync());
                Assert.Equal(2, JsonTestHelper.GetInt64Property(doc2.RootElement, "total_clicks", "totalClicks"));

                var daily2 = await client2.GetAsync($"/analytics/{customAlias}/daily?days=1");
                Assert.Equal(HttpStatusCode.OK, daily2.StatusCode);
                using var dailyDoc2 = JsonDocument.Parse(await daily2.Content.ReadAsStringAsync());
                var dailyArray = JsonTestHelper.GetArrayProperty(dailyDoc2.RootElement, "daily");
                Assert.NotNull(dailyArray);
                Assert.Equal(2, JsonTestHelper.GetInt64Property(dailyArray.Value[0], "clicks"));
            }
        }
        finally
        {
            Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
            try { if (File.Exists(sharedDbPath)) File.Delete(sharedDbPath); } catch { }
            try { if (File.Exists(sharedDbPath + "-wal")) File.Delete(sharedDbPath + "-wal"); } catch { }
            try { if (File.Exists(sharedDbPath + "-shm")) File.Delete(sharedDbPath + "-shm"); } catch { }
        }
    }
}