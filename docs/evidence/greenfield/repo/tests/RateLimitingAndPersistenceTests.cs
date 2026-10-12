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
    public async Task R8_RateLimiting_WithinThreshold_ReturnsCreated201()
    {
        using var factory = new CustomWebApplicationFactory();
        var client = factory.CreateClient();
        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/rate-under-limit" };

        var response = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
    }

    [Fact]
    public async Task R8_RateLimiting_ExceedingThreshold_ReturnsTooManyRequests429WithRetryAfter()
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
    public async Task R9_Persistence_SurvivesServiceRestart()
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
            }
        }
        finally
        {
            try { Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools(); } catch { }
            try { if (File.Exists(sharedDbPath)) File.Delete(sharedDbPath); } catch { }
            try { if (File.Exists(sharedDbPath + "-wal")) File.Delete(sharedDbPath + "-wal"); } catch { }
            try { if (File.Exists(sharedDbPath + "-shm")) File.Delete(sharedDbPath + "-shm"); } catch { }
        }
    }
}