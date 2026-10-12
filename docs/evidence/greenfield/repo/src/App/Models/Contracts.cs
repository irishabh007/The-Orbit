using System.Text.Json.Serialization;

namespace App.Models;

public sealed record ShortenRequest(
    [property: JsonPropertyName("url")] string? Url,
    [property: JsonPropertyName("custom_alias")] string? CustomAlias = null,
    [property: JsonPropertyName("expires_at")] DateTime? ExpiresAt = null
);

public sealed record ShortenResponse(
    [property: JsonPropertyName("code")] string Code,
    [property: JsonPropertyName("short_url")] string ShortUrl,
    [property: JsonPropertyName("url")] string? Url = null,
    [property: JsonPropertyName("created_at")] DateTime? CreatedAt = null,
    [property: JsonPropertyName("expires_at")] DateTime? ExpiresAt = null
);

public sealed record AnalyticsResponse(
    [property: JsonPropertyName("code")] string Code,
    [property: JsonPropertyName("url")] string Url,
    [property: JsonPropertyName("total_clicks")] long TotalClicks,
    [property: JsonPropertyName("created_at")] DateTime CreatedAt,
    [property: JsonPropertyName("last_visited_at")] DateTime? LastVisitedAt,
    [property: JsonPropertyName("expires_at")] DateTime? ExpiresAt,
    [property: JsonPropertyName("expired")] bool Expired
);

public sealed record HealthResponse(
    [property: JsonPropertyName("status")] string Status
);

public sealed record ErrorResponse(
    [property: JsonPropertyName("error")] string Error,
    [property: JsonPropertyName("message")] string Message
)
{
    public ErrorResponse(string message) : this(message, message) { }
}