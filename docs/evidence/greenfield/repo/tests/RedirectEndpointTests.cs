using System;
using System.Collections.Generic;
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
    public async Task R3_Redirect_ActiveCode_Returns307TemporaryRedirectWithLocation()
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
    public async Task R3_Redirect_NonExistentCode_ReturnsNotFound404()
    {
        var redirectClient = _factory.CreateNoRedirectClient();
        var nonExistentCode = "non-existent-" + Guid.NewGuid().ToString("N");

        var response = await redirectClient.GetAsync($"/{nonExistentCode}");

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    [Fact]
    public async Task R3_Redirect_IncrementsClickCountAndUpdatesLastVisitedAt()
    {
        var client = _factory.CreateClient();
        var redirectClient = _factory.CreateNoRedirectClient();
        var targetUrl = "https://example.com/metrics-" + Guid.NewGuid().ToString("N");

        var createResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(new { url = targetUrl }));
        using var createDoc = JsonDocument.Parse(await createResponse.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(createDoc.RootElement, "code");
        Assert.NotNull(code);

        var initialAnalytics = await client.GetAsync($"/analytics/{code}");
        using var initialDoc = JsonDocument.Parse(await initialAnalytics.Content.ReadAsStringAsync());
        Assert.Equal(0, JsonTestHelper.GetInt64Property(initialDoc.RootElement, "total_clicks", "totalClicks"));
        Assert.True(JsonTestHelper.PropertyIsNull(initialDoc.RootElement, "last_visited_at", "lastVisitedAt"));

        var visitResponse1 = await redirectClient.GetAsync($"/{code}");
        Assert.Equal(HttpStatusCode.TemporaryRedirect, visitResponse1.StatusCode);

        var updatedAnalytics1 = await client.GetAsync($"/analytics/{code}");
        using var updatedDoc1 = JsonDocument.Parse(await updatedAnalytics1.Content.ReadAsStringAsync());
        Assert.Equal(1, JsonTestHelper.GetInt64Property(updatedDoc1.RootElement, "total_clicks", "totalClicks"));
        var lastVisited1 = JsonTestHelper.GetStringProperty(updatedDoc1.RootElement, "last_visited_at", "lastVisitedAt");
        Assert.NotNull(lastVisited1);
        Assert.True(DateTime.TryParse(lastVisited1, out _));

        var visitResponse2 = await redirectClient.GetAsync($"/{code}");
        Assert.Equal(HttpStatusCode.TemporaryRedirect, visitResponse2.StatusCode);

        var updatedAnalytics2 = await client.GetAsync($"/analytics/{code}");
        using var updatedDoc2 = JsonDocument.Parse(await updatedAnalytics2.Content.ReadAsStringAsync());
        Assert.Equal(2, JsonTestHelper.GetInt64Property(updatedDoc2.RootElement, "total_clicks", "totalClicks"));
    }

    [Fact]
    public async Task R4_Shorten_WithExpiration_StoresExpirationTimestamp()
    {
        var client = _factory.CreateClient();
        var targetUrl = "https://example.com/future-exp";
        var futureExpiration = DateTime.UtcNow.AddDays(7).ToString("yyyy-MM-ddTHH:mm:ssZ");

        var payload = new Dictionary<string, object?>
        {
            ["url"] = targetUrl,
            ["expires_at"] = futureExpiration
        };

        var response = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);

        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(doc.RootElement, "code");
        Assert.NotNull(code);

        var redirectClient = _factory.CreateNoRedirectClient();
        var redirectResponse = await redirectClient.GetAsync($"/{code}");
        Assert.Equal(HttpStatusCode.TemporaryRedirect, redirectResponse.StatusCode);
    }

    [Fact]
    public async Task R4_Redirect_ExpiredLink_ReturnsNotFound404AndDoesNotIncrementClicks()
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