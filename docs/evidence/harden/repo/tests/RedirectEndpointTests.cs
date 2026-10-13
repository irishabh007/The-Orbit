using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Text.Json;
using System.Threading.Tasks;
using Xunit;

namespace Tests;

public class RedirectEndpointTests : IClassFixture<CustomWebApplicationFactory>
{
    private readonly CustomWebApplicationFactory _factory;

    public RedirectEndpointTests(CustomWebApplicationFactory factory)
    {
        _factory = factory;
    }

    [Fact]
    public async Task R1_Redirect_ActiveLinkWithPriorClicks_IncrementsLifetimeClickCountAndSetsLastVisited()
    {
        var client = _factory.CreateClient();
        var redirectClient = _factory.CreateNoRedirectClient();
        var targetUrl = "https://example.com/r1-lifetime-" + Guid.NewGuid().ToString("N");

        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(new { url = targetUrl }));
        Assert.Equal(HttpStatusCode.Created, createResponse.StatusCode);
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        // Seed initial ClickCount = 5 directly in SQLite to verify exact atomic increment
        using (var connection = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={_factory.DbPath};Default Timeout=30"))
        {
            await connection.OpenAsync();
            using var cmd = connection.CreateCommand();
            cmd.CommandText = "UPDATE ShortLinks SET ClickCount = 5 WHERE Code = @code;";
            cmd.Parameters.AddWithValue("@code", code);
            await cmd.ExecuteNonQueryAsync();
        }

        var beforeRedirectUtc = DateTime.UtcNow.AddMinutes(-2);
        var redirectResponse = await redirectClient.GetAsync($"/{code}");
        var afterRedirectUtc = DateTime.UtcNow.AddMinutes(2);

        Assert.Equal(HttpStatusCode.TemporaryRedirect, redirectResponse.StatusCode);
        Assert.Equal(targetUrl, redirectResponse.Headers.Location?.ToString());

        var analyticsResponse = await client.GetAsync($"/analytics/{code}");
        Assert.Equal(HttpStatusCode.OK, analyticsResponse.StatusCode);
        using var analyticsDoc = JsonDocument.Parse(await analyticsResponse.Content.ReadAsStringAsync());
        Assert.Equal(6, JsonTestHelper.GetInt64Property(analyticsDoc.RootElement, "total_clicks", "totalClicks"));

        var lastVisited = JsonTestHelper.GetStringProperty(analyticsDoc.RootElement, "last_visited_at", "lastVisitedAt");
        Assert.NotNull(lastVisited);
        Assert.True(DateTime.TryParse(lastVisited, CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var lastVisitedUtc));
        Assert.True(lastVisitedUtc >= beforeRedirectUtc && lastVisitedUtc <= afterRedirectUtc);
    }

    [Fact]
    public async Task R1_Redirect_NonExistentOrExpiredCode_ReturnsNotFound404()
    {
        var redirectClient = _factory.CreateNoRedirectClient();

        // Non-existent link
        var missingCode = "absent-" + Guid.NewGuid().ToString("N");
        var missingResponse = await redirectClient.GetAsync($"/{missingCode}");
        Assert.Equal(HttpStatusCode.NotFound, missingResponse.StatusCode);

        // Expired link
        var client = _factory.CreateClient();
        var expiredUrl = "https://example.com/r1-expired-" + Guid.NewGuid().ToString("N");
        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(new
        {
            url = expiredUrl,
            expires_at = "2020-01-01T00:00:00Z"
        }));
        Assert.Equal(HttpStatusCode.Created, createResponse.StatusCode);
        using var doc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var expiredCode = JsonTestHelper.GetStringProperty(doc.RootElement, "code");
        Assert.NotNull(expiredCode);

        var expiredResponse = await redirectClient.GetAsync($"/{expiredCode}");
        Assert.Equal(HttpStatusCode.NotFound, expiredResponse.StatusCode);
    }

    [Fact]
    public async Task R2_Redirect_FirstVisitOnDate_CreatesDailyRecordWithSingleClick()
    {
        var client = _factory.CreateClient();
        var redirectClient = _factory.CreateNoRedirectClient();
        var targetUrl = "https://example.com/r2-single-" + Guid.NewGuid().ToString("N");

        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(new { url = targetUrl }));
        Assert.Equal(HttpStatusCode.Created, createResponse.StatusCode);
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        var redirectResponse = await redirectClient.GetAsync($"/{code}");
        Assert.Equal(HttpStatusCode.TemporaryRedirect, redirectResponse.StatusCode);

        var dailyResponse = await client.GetAsync($"/analytics/{code}/daily?days=1");
        Assert.Equal(HttpStatusCode.OK, dailyResponse.StatusCode);
        using var dailyDoc = JsonDocument.Parse(await dailyResponse.Content.ReadAsStringAsync());
        var dailyArray = JsonTestHelper.GetArrayProperty(dailyDoc.RootElement, "daily");
        Assert.NotNull(dailyArray);
        Assert.Equal(1, dailyArray.Value.GetArrayLength());

        var todayRecord = dailyArray.Value[0];
        var todayUtcDate = DateTime.UtcNow.ToString("yyyy-MM-dd");
        Assert.Equal(todayUtcDate, JsonTestHelper.GetStringProperty(todayRecord, "date"));
        Assert.Equal(1, JsonTestHelper.GetInt64Property(todayRecord, "clicks"));
    }

    [Fact]
    public async Task R2_Redirect_ExistingDailyRecordWithClicksN_UpsertsToNPlusOne()
    {
        var client = _factory.CreateClient();
        var redirectClient = _factory.CreateNoRedirectClient();
        var targetUrl = "https://example.com/r2-upsert-" + Guid.NewGuid().ToString("N");

        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(new { url = targetUrl }));
        Assert.Equal(HttpStatusCode.Created, createResponse.StatusCode);
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        // First redirect initializes daily record
        var firstVisit = await redirectClient.GetAsync($"/{code}");
        Assert.Equal(HttpStatusCode.TemporaryRedirect, firstVisit.StatusCode);

        // Manually update daily clicks to N = 10 directly in database
        using (var connection = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={_factory.DbPath};Default Timeout=30"))
        {
            await connection.OpenAsync();
            using var cmd = connection.CreateCommand();
            cmd.CommandText = "UPDATE ShortLinkDailyClicks SET Clicks = 10 WHERE ShortLinkId = (SELECT Id FROM ShortLinks WHERE Code = @code);";
            cmd.Parameters.AddWithValue("@code", code);
            await cmd.ExecuteNonQueryAsync();
        }

        // Second redirect executes atomic SQLite upsert
        var secondVisit = await redirectClient.GetAsync($"/{code}");
        Assert.Equal(HttpStatusCode.TemporaryRedirect, secondVisit.StatusCode);

        var dailyResponse = await client.GetAsync($"/analytics/{code}/daily?days=1");
        Assert.Equal(HttpStatusCode.OK, dailyResponse.StatusCode);

        using var dailyDoc = JsonDocument.Parse(await dailyResponse.Content.ReadAsStringAsync());
        var dailyArray = JsonTestHelper.GetArrayProperty(dailyDoc.RootElement, "daily");
        Assert.NotNull(dailyArray);
        Assert.Equal(1, dailyArray.Value.GetArrayLength());
        Assert.Equal(11, JsonTestHelper.GetInt64Property(dailyArray.Value[0], "clicks"));
    }

    [Fact]
    public async Task R5_Redirect_Concurrent100Requests_AccuratelyIncrementsLifetimeAndDailyClicks()
    {
        var client = _factory.CreateClient();
        var redirectClient = _factory.CreateNoRedirectClient();
        var targetUrl = "https://example.com/parallel-100-" + Guid.NewGuid().ToString("N");

        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(new { url = targetUrl }));
        Assert.Equal(HttpStatusCode.Created, createResponse.StatusCode);
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        const int concurrency = 100;
        var tasks = new Task<HttpResponseMessage>[concurrency];
        for (int i = 0; i < concurrency; i++)
        {
            tasks[i] = redirectClient.GetAsync($"/{code}");
        }

        var responses = await Task.WhenAll(tasks);
        foreach (var response in responses)
        {
            Assert.Equal(HttpStatusCode.TemporaryRedirect, response.StatusCode);
            Assert.Equal(targetUrl, response.Headers.Location?.ToString());
        }

        // Lifetime ClickCount in database equals exactly 100
        var analyticsResponse = await client.GetAsync($"/analytics/{code}");
        Assert.Equal(HttpStatusCode.OK, analyticsResponse.StatusCode);
        using var analyticsDoc = JsonDocument.Parse(await analyticsResponse.Content.ReadAsStringAsync());
        Assert.Equal(100, JsonTestHelper.GetInt64Property(analyticsDoc.RootElement, "total_clicks", "totalClicks"));

        // ShortLinkDailyClicks.Clicks for current UTC date equals exactly 100
        var dailyResponse = await client.GetAsync($"/analytics/{code}/daily?days=1");
        Assert.Equal(HttpStatusCode.OK, dailyResponse.StatusCode);

        using var dailyDoc = JsonDocument.Parse(await dailyResponse.Content.ReadAsStringAsync());
        var dailyArray = JsonTestHelper.GetArrayProperty(dailyDoc.RootElement, "daily");
        Assert.NotNull(dailyArray);
        Assert.Equal(1, dailyArray.Value.GetArrayLength());
        Assert.Equal(100, JsonTestHelper.GetInt64Property(dailyArray.Value[0], "clicks"));
    }

    [Fact]
    public async Task R7_Redirect_ActiveCode_Returns307TemporaryRedirectWithLocation()
    {
        var client = _factory.CreateClient();
        var targetUrl = "https://example.com/target-" + Guid.NewGuid().ToString("N");
        var payload = new Dictionary<string, object?> { ["url"] = targetUrl };

        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        Assert.Equal(HttpStatusCode.Created, createResponse.StatusCode);
        using var doc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(doc.RootElement, "code");
        Assert.NotNull(code);

        var redirectClient = _factory.CreateNoRedirectClient();
        var redirectResponse = await redirectClient.GetAsync($"/{code}");

        Assert.Equal(HttpStatusCode.TemporaryRedirect, redirectResponse.StatusCode);
        Assert.Equal(targetUrl, redirectResponse.Headers.Location?.ToString());
    }

    [Fact]
    public async Task R7_Redirect_ExpiredLink_ReturnsNotFound404AndDoesNotIncrementClicks()
    {
        var client = _factory.CreateClient();
        var targetUrl = "https://example.com/expired-test";
        var pastExpiration = "2020-01-01T00:00:00Z";

        var payload = new Dictionary<string, object?>
        {
            ["url"] = targetUrl,
            ["expires_at"] = pastExpiration
        };

        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        Assert.Equal(HttpStatusCode.Created, createResponse.StatusCode);

        using var doc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(doc.RootElement, "code");
        Assert.NotNull(code);

        var redirectClient = _factory.CreateNoRedirectClient();
        var redirectResponse = await redirectClient.GetAsync($"/{code}");
        Assert.Equal(HttpStatusCode.NotFound, redirectResponse.StatusCode);

        var analyticsResponse = await client.GetAsync($"/analytics/{code}");
        if (analyticsResponse.StatusCode == HttpStatusCode.OK)
        {
            using var analyticsDoc = JsonDocument.Parse(await analyticsResponse.Content.ReadAsStringAsync());
            Assert.Equal(0, JsonTestHelper.GetInt64Property(analyticsDoc.RootElement, "total_clicks", "totalClicks"));
        }
    }
}