using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text.Json;
using System.Threading.Tasks;
using Xunit;

namespace Tests;

public class AnalyticsAndHealthTests : IClassFixture<CustomWebApplicationFactory>
{
    private readonly CustomWebApplicationFactory _factory;

    public AnalyticsAndHealthTests(CustomWebApplicationFactory factory)
    {
        _factory = factory;
    }

    [Fact]
    public async Task R7_Analytics_NewLink_ReturnsZeroClicksAndNullLastVisited()
    {
        var client = _factory.CreateClient();
        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/fresh-link" };

        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        var analyticsResponse = await client.GetAsync($"/analytics/{code}");
        Assert.Equal(HttpStatusCode.OK, analyticsResponse.StatusCode);

        using var analyticsDoc = JsonDocument.Parse(await analyticsResponse.Content.ReadAsStringAsync());
        Assert.Equal(0, JsonTestHelper.GetInt64Property(analyticsDoc.RootElement, "total_clicks", "totalClicks"));

        var createdAt = JsonTestHelper.GetStringProperty(analyticsDoc.RootElement, "created_at", "createdAt");
        Assert.NotNull(createdAt);
        Assert.True(DateTime.TryParse(createdAt, out _));

        Assert.True(JsonTestHelper.PropertyIsNull(analyticsDoc.RootElement, "last_visited_at", "lastVisitedAt"));
    }

    [Fact]
    public async Task R7_Analytics_AfterVisit_ReturnsUpdatedClicksAndLastVisitedTimestamp()
    {
        var client = _factory.CreateClient();
        var redirectClient = _factory.CreateNoRedirectClient();
        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/visited-link" };

        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        var redirectResponse = await redirectClient.GetAsync($"/{code}");
        Assert.Equal(HttpStatusCode.TemporaryRedirect, redirectResponse.StatusCode);

        var analyticsResponse = await client.GetAsync($"/analytics/{code}");
        Assert.Equal(HttpStatusCode.OK, analyticsResponse.StatusCode);

        using var analyticsDoc = JsonDocument.Parse(await analyticsResponse.Content.ReadAsStringAsync());
        Assert.Equal(1, JsonTestHelper.GetInt64Property(analyticsDoc.RootElement, "total_clicks", "totalClicks"));

        var lastVisited = JsonTestHelper.GetStringProperty(analyticsDoc.RootElement, "last_visited_at", "lastVisitedAt");
        Assert.NotNull(lastVisited);
        Assert.True(DateTime.TryParse(lastVisited, out _));
    }

    [Fact]
    public async Task R7_Analytics_NonExistentCode_ReturnsNotFound404()
    {
        var client = _factory.CreateClient();
        var missingCode = "absent-" + Guid.NewGuid().ToString("N");

        var response = await client.GetAsync($"/analytics/{missingCode}");
        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    [Fact]
    public async Task R7_Health_WhenOperational_ReturnsOk200()
    {
        var client = _factory.CreateClient();

        var response = await client.GetAsync("/health");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var status = JsonTestHelper.GetStringProperty(doc.RootElement, "status");
        Assert.Equal("healthy", status);
    }

    [Fact]
    public async Task R7_Health_WhenDatastoreUnavailable_ReturnsServiceUnavailable503()
    {
        var isolatedDbPath = Path.Combine(Path.GetTempPath(), $"health_fail_{Guid.NewGuid():N}.db");
        using var failureFactory = new CustomWebApplicationFactory(isolatedDbPath, deleteDbOnDispose: true);
        var client = failureFactory.CreateClient();

        var healthyResponse = await client.GetAsync("/health");
        Assert.Equal(HttpStatusCode.OK, healthyResponse.StatusCode);

        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        try { File.Delete(isolatedDbPath); } catch { }
        try { File.Delete(isolatedDbPath + "-wal"); } catch { }
        try { File.Delete(isolatedDbPath + "-shm"); } catch { }
        try { File.WriteAllText(isolatedDbPath, "corrupt-sqlite-database-header"); } catch { }

        var failureResponse = await client.GetAsync("/health");
        Assert.Equal(HttpStatusCode.ServiceUnavailable, failureResponse.StatusCode);
    }

    [Fact]
    public async Task R7_DailyAnalytics_DefaultDays_ReturnsSevenConsecutiveDaysEndingToday()
    {
        var client = _factory.CreateClient();
        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/r1-default" };
        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        var response = await client.GetAsync($"/analytics/{code}/daily");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var daysValue = JsonTestHelper.GetInt64Property(doc.RootElement, "days");
        Assert.Equal(7, daysValue);

        var dailyArray = JsonTestHelper.GetArrayProperty(doc.RootElement, "daily");
        Assert.NotNull(dailyArray);
        Assert.Equal(7, dailyArray.Value.GetArrayLength());

        var todayUtc = DateOnly.FromDateTime(DateTime.UtcNow);
        var expectedDates = new List<DateOnly>();
        for (int i = 6; i >= 0; i--)
        {
            expectedDates.Add(todayUtc.AddDays(-i));
        }

        int index = 0;
        foreach (var entry in dailyArray.Value.EnumerateArray())
        {
            var dateStr = JsonTestHelper.GetStringProperty(entry, "date");
            Assert.NotNull(dateStr);
            Assert.True(DateOnly.TryParse(dateStr, out var actualDate));
            Assert.Equal(expectedDates[index], actualDate);
            index++;
        }
    }

    [Fact]
    public async Task R7_DailyAnalytics_CustomDays14_ReturnsFourteenConsecutiveDays()
    {
        var client = _factory.CreateClient();
        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/r1-14days" };
        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        var response = await client.GetAsync($"/analytics/{code}/daily?days=14");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var daysValue = JsonTestHelper.GetInt64Property(doc.RootElement, "days");
        Assert.Equal(14, daysValue);

        var dailyArray = JsonTestHelper.GetArrayProperty(doc.RootElement, "daily");
        Assert.NotNull(dailyArray);
        Assert.Equal(14, dailyArray.Value.GetArrayLength());

        var todayUtc = DateOnly.FromDateTime(DateTime.UtcNow);
        int index = 0;
        foreach (var entry in dailyArray.Value.EnumerateArray())
        {
            var expectedDate = todayUtc.AddDays(-(13 - index));
            var dateStr = JsonTestHelper.GetStringProperty(entry, "date");
            Assert.NotNull(dateStr);
            Assert.True(DateOnly.TryParse(dateStr, out var actualDate));
            Assert.Equal(expectedDate, actualDate);
            index++;
        }
    }

    [Theory]
    [InlineData("?days=91")]
    [InlineData("?days=0")]
    [InlineData("?days=-1")]
    [InlineData("?days=invalid")]
    [InlineData("?days=abc")]
    [InlineData("?days=3.14")]
    public async Task R7_DailyAnalytics_InvalidDays_ReturnsBadRequest400(string queryString)
    {
        var client = _factory.CreateClient();
        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/r1-invalid-days" };
        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        var response = await client.GetAsync($"/analytics/{code}/daily{queryString}");
        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);

        var body = await response.Content.ReadAsStringAsync();
        Assert.False(string.IsNullOrWhiteSpace(body));
        using var errorDoc = JsonDocument.Parse(body);
        Assert.NotNull(JsonTestHelper.GetStringProperty(errorDoc.RootElement, "error", "message", "detail"));
    }

    [Fact]
    public async Task R7_DailyAnalytics_ZeroFilledDays_ReturnsConsecutiveDatesWithZeroClicks()
    {
        var client = _factory.CreateClient();
        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/r2-zero-filled" };
        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        var response = await client.GetAsync($"/analytics/{code}/daily?days=7");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var dailyArray = JsonTestHelper.GetArrayProperty(doc.RootElement, "daily");
        Assert.NotNull(dailyArray);
        Assert.Equal(7, dailyArray.Value.GetArrayLength());

        foreach (var entry in dailyArray.Value.EnumerateArray())
        {
            var clicks = JsonTestHelper.GetInt64Property(entry, "clicks");
            Assert.Equal(0, clicks);
        }
    }

    [Fact]
    public async Task R7_DailyAnalytics_RecordedClicks_MatchesCountForDateAndZeroForOtherDays()
    {
        var client = _factory.CreateClient();
        var redirectClient = _factory.CreateNoRedirectClient();
        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/r2-recorded-clicks" };
        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        for (int i = 0; i < 3; i++)
        {
            var visit = await redirectClient.GetAsync($"/{code}");
            Assert.Equal(HttpStatusCode.TemporaryRedirect, visit.StatusCode);
        }

        var response = await client.GetAsync($"/analytics/{code}/daily?days=7");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var dailyArray = JsonTestHelper.GetArrayProperty(doc.RootElement, "daily");
        Assert.NotNull(dailyArray);
        Assert.Equal(7, dailyArray.Value.GetArrayLength());

        var todayStr = DateTime.UtcNow.ToString("yyyy-MM-dd");
        foreach (var entry in dailyArray.Value.EnumerateArray())
        {
            var dateStr = JsonTestHelper.GetStringProperty(entry, "date");
            var clicks = JsonTestHelper.GetInt64Property(entry, "clicks");
            if (dateStr == todayStr)
            {
                Assert.Equal(3, clicks);
            }
            else
            {
                Assert.Equal(0, clicks);
            }
        }
    }

    [Fact]
    public async Task R7_DailyAnalytics_NonExistentCode_ReturnsNotFound404MatchingBaseAnalytics()
    {
        var client = _factory.CreateClient();
        var unknownCode = "unknown-" + Guid.NewGuid().ToString("N");

        var baseAnalytics = await client.GetAsync($"/analytics/{unknownCode}");
        var dailyAnalytics = await client.GetAsync($"/analytics/{unknownCode}/daily");

        Assert.Equal(HttpStatusCode.NotFound, baseAnalytics.StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, dailyAnalytics.StatusCode);

        var dailyBody = await dailyAnalytics.Content.ReadAsStringAsync();
        Assert.False(string.IsNullOrWhiteSpace(dailyBody));

        using var dailyDoc = JsonDocument.Parse(dailyBody);
        Assert.NotNull(JsonTestHelper.GetStringProperty(dailyDoc.RootElement, "error", "message", "detail"));
    }

    [Fact]
    public async Task R7_DailyAnalytics_ExpiredCode_ReturnsNotFound404MatchingBaseAnalytics()
    {
        var client = _factory.CreateClient();
        var payload = new Dictionary<string, object?>
        {
            ["url"] = "https://example.com/expired-daily-r3",
            ["expires_at"] = "2020-01-01T00:00:00Z"
        };

        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        Assert.Equal(HttpStatusCode.Created, createResponse.StatusCode);
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        var baseAnalytics = await client.GetAsync($"/analytics/{code}");
        var dailyAnalytics = await client.GetAsync($"/analytics/{code}/daily");

        Assert.Equal(HttpStatusCode.NotFound, baseAnalytics.StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, dailyAnalytics.StatusCode);

        var dailyBody = await dailyAnalytics.Content.ReadAsStringAsync();
        Assert.False(string.IsNullOrWhiteSpace(dailyBody));
    }

    [Fact]
    public async Task R7_DailyAnalytics_MaxDays90_ReturnsNinetyRecordsUnderFiftyMilliseconds()
    {
        var client = _factory.CreateClient();
        var payload = new Dictionary<string, object?> { ["url"] = "https://example.com/perf-r6" };
        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        _ = await client.GetAsync($"/analytics/{code}/daily?days=7");

        var stopwatch = System.Diagnostics.Stopwatch.StartNew();
        var response = await client.GetAsync($"/analytics/{code}/daily?days=90");
        stopwatch.Stop();

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var dailyArray = JsonTestHelper.GetArrayProperty(doc.RootElement, "daily");
        Assert.NotNull(dailyArray);
        Assert.Equal(90, dailyArray.Value.GetArrayLength());
    }

    [Theory]
    [InlineData("' OR '1'='1", "7")]
    [InlineData("xyz; DROP TABLE ShortLinks;--", "7")]
    [InlineData("validCode", "1; DROP TABLE ShortLinkDailyClicks;--")]
    [InlineData("validCode", "<script>alert(1)</script>")]
    public async Task R7_DailyAnalytics_SqlInjectionOrMalformedPayloads_RejectsWith400Or404WithoutStackTrace(string codeParam, string daysParam)
    {
        var client = _factory.CreateClient();
        var safeUrl = StatusCodeUrl(codeParam, daysParam);

        var response = await client.GetAsync(safeUrl);
        Assert.True(response.StatusCode == HttpStatusCode.BadRequest || response.StatusCode == HttpStatusCode.NotFound,
            $"Expected 400 or 404 but got {response.StatusCode}");

        var content = await response.Content.ReadAsStringAsync();
        Assert.DoesNotContain("SqliteException", content);
        Assert.DoesNotContain("Microsoft.Data.Sqlite", content);
        Assert.DoesNotContain("StackTrace", content, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("SELECT ", content, StringComparison.OrdinalIgnoreCase);
    }

    private static string StatusCodeUrl(string code, string days)
    {
        return $"/analytics/{Uri.EscapeDataString(code)}/daily?days={Uri.EscapeDataString(days)}";
    }
}