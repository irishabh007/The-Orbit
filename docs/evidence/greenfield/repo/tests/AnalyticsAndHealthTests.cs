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
    public async Task R5_Analytics_NewLink_ReturnsZeroClicksAndNullLastVisited()
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
    public async Task R5_Analytics_AfterVisit_ReturnsUpdatedClicksAndLastVisitedTimestamp()
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
    public async Task R5_Analytics_NonExistentCode_ReturnsNotFound404()
    {
        var client = _factory.CreateClient();
        var missingCode = "absent-" + Guid.NewGuid().ToString("N");

        var response = await client.GetAsync($"/analytics/{missingCode}");
        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    [Fact]
    public async Task R6_Health_WhenOperational_ReturnsOk200()
    {
        var client = _factory.CreateClient();

        var response = await client.GetAsync("/health");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var status = JsonTestHelper.GetStringProperty(doc.RootElement, "status");
        Assert.Equal("healthy", status);
    }

    [Fact]
    public async Task R6_Health_WhenDatastoreUnavailable_ReturnsServiceUnavailable503()
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
}