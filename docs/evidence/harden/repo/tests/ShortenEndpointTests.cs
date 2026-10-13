using System;
using System.Collections.Generic;
using System.Net;
using System.Text.Json;
using System.Threading.Tasks;
using Xunit;

namespace Tests;

public class ShortenEndpointTests : IClassFixture<CustomWebApplicationFactory>
{
    private readonly CustomWebApplicationFactory _factory;

    public ShortenEndpointTests(CustomWebApplicationFactory factory)
    {
        _factory = factory;
    }

    [Fact]
    public async Task R7_Shorten_ValidUrl_ReturnsCreatedWithCode()
    {
        var client = _factory.CreateClient();
        var payload = new Dictionary<string, object?>
        {
            ["url"] = "https://example.com/very/long/path"
        };

        var response = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        var body = await response.Content.ReadAsStringAsync();
        using var doc = JsonDocument.Parse(body);

        var code = JsonTestHelper.GetStringProperty(doc.RootElement, "code");
        var shortUrl = JsonTestHelper.GetStringProperty(doc.RootElement, "short_url", "shortUrl");

        Assert.False(string.IsNullOrWhiteSpace(code));
        Assert.False(string.IsNullOrWhiteSpace(shortUrl));
        Assert.Contains(code!, shortUrl!);
    }

    [Fact]
    public async Task R7_Shorten_DifferentUrls_ReceiveDistinctGeneratedCodes()
    {
        var client = _factory.CreateClient();
        var payload1 = new Dictionary<string, object?> { ["url"] = "https://example.com/item/1" };
        var payload2 = new Dictionary<string, object?> { ["url"] = "https://example.com/item/2" };

        var response1 = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload1));
        var response2 = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload2));

        Assert.Equal(HttpStatusCode.Created, response1.StatusCode);
        Assert.Equal(HttpStatusCode.Created, response2.StatusCode);

        using var doc1 = JsonDocument.Parse(await response1.Content.ReadAsStringAsync());
        using var doc2 = JsonDocument.Parse(await response2.Content.ReadAsStringAsync());

        var code1 = JsonTestHelper.GetStringProperty(doc1.RootElement, "code");
        var code2 = JsonTestHelper.GetStringProperty(doc2.RootElement, "code");

        Assert.False(string.IsNullOrWhiteSpace(code1));
        Assert.False(string.IsNullOrWhiteSpace(code2));
        Assert.NotEqual(code1, code2);
    }

    [Fact]
    public async Task R7_Shorten_WithCustomAlias_ReturnsCreatedWithSpecifiedCode()
    {
        var client = _factory.CreateClient();
        var alias = "my-link-" + Guid.NewGuid().ToString("N")[..6];
        var payload = new Dictionary<string, object?>
        {
            ["url"] = "https://example.com",
            ["custom_alias"] = alias
        };

        var response = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var code = JsonTestHelper.GetStringProperty(doc.RootElement, "code");
        Assert.Equal(alias, code);
    }

    [Fact]
    public async Task R7_Shorten_DuplicateCustomAlias_ReturnsConflict409()
    {
        var client = _factory.CreateClient();
        var duplicateAlias = "dup-alias-" + Guid.NewGuid().ToString("N")[..6];
        var firstPayload = new Dictionary<string, object?>
        {
            ["url"] = "https://example.com/first",
            ["custom_alias"] = duplicateAlias
        };
        var secondPayload = new Dictionary<string, object?>
        {
            ["url"] = "https://other.com/second",
            ["custom_alias"] = duplicateAlias
        };

        var firstResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(firstPayload));
        Assert.Equal(HttpStatusCode.Created, firstResponse.StatusCode);

        var secondResponse = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(secondPayload));
        Assert.Equal(HttpStatusCode.Conflict, secondResponse.StatusCode);

        var errorContent = await secondResponse.Content.ReadAsStringAsync();
        Assert.False(string.IsNullOrWhiteSpace(errorContent));
    }

    [Fact]
    public async Task R7_Shorten_WithExpiration_StoresExpirationTimestamp()
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

    [Theory]
    [InlineData("ftp://example.com/file")]
    [InlineData("javascript:alert(1)")]
    [InlineData("file:///etc/passwd")]
    [InlineData("data:text/html,<h1>test</h1>")]
    public async Task R7_Shorten_NonHttpScheme_ReturnsBadRequest400(string invalidSchemeUrl)
    {
        var client = _factory.CreateClient();
        var payload = new Dictionary<string, object?>
        {
            ["url"] = invalidSchemeUrl
        };

        var response = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }

    [Theory]
    [InlineData("http://localhost")]
    [InlineData("http://localhost:8080")]
    [InlineData("http://127.0.0.1:8080")]
    [InlineData("http://169.254.169.254/latest/meta-data/")]
    [InlineData("http://10.0.0.1/admin")]
    [InlineData("http://[::1]/")]
    [InlineData("http://192.168.1.1/")]
    [InlineData("http://172.16.0.1/")]
    public async Task R7_Shorten_PrivateOrLoopbackIp_ReturnsBadRequest400(string privateTargetUrl)
    {
        var client = _factory.CreateClient();
        var payload = new Dictionary<string, object?>
        {
            ["url"] = privateTargetUrl
        };

        var response = await client.PostAsync("/shorten", JsonTestHelper.CreateJsonContent(payload));

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }
}