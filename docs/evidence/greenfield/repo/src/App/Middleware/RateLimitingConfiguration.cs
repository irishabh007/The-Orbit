using System.Threading.RateLimiting;
using App.Models;
using Microsoft.AspNetCore.RateLimiting;

namespace App.Middleware;

public static class RateLimitingConfiguration
{
    public const string ShortenPolicyName = "ShortenPolicy";

    public static IServiceCollection AddShortenRateLimiter(this IServiceCollection services, IConfiguration configuration)
    {
        var defaultPermitLimit = configuration.GetValue<int>("RateLimiting:PermitLimit", 60);
        var defaultWindowSeconds = configuration.GetValue<int>("RateLimiting:WindowSeconds", 60);
        var defaultQueueLimit = configuration.GetValue<int>("RateLimiting:QueueLimit", 0);

        services.AddRateLimiter(options =>
        {
            options.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
            options.OnRejected = async (context, token) =>
            {
                context.HttpContext.Response.StatusCode = StatusCodes.Status429TooManyRequests;

                if (context.Lease.TryGetMetadata(MetadataName.RetryAfter, out var retryAfter))
                {
                    var seconds = Math.Max(1, (int)retryAfter.TotalSeconds);
                    context.HttpContext.Response.Headers.RetryAfter = seconds.ToString();
                }
                else
                {
                    context.HttpContext.Response.Headers.RetryAfter = defaultWindowSeconds.ToString();
                }

                context.HttpContext.Response.ContentType = "application/json";
                await context.HttpContext.Response.WriteAsJsonAsync(
                    new ErrorResponse("Rate limit exceeded. Please try again later.", "Rate limit exceeded."),
                    token);
            };

            options.AddPolicy(ShortenPolicyName, httpContext =>
            {
                var config = httpContext.RequestServices.GetService<IConfiguration>();
                var permitLimit = config?.GetValue<int>("RateLimiting:PermitLimit") ?? defaultPermitLimit;
                if (permitLimit <= 0) permitLimit = defaultPermitLimit;

                var windowSeconds = config?.GetValue<int>("RateLimiting:WindowSeconds") ?? defaultWindowSeconds;
                if (windowSeconds <= 0) windowSeconds = defaultWindowSeconds;

                var queueLimit = config?.GetValue<int>("RateLimiting:QueueLimit") ?? defaultQueueLimit;

                var clientIp = ResolveClientIp(httpContext);
                return RateLimitPartition.GetFixedWindowLimiter(
                    partitionKey: clientIp,
                    factory: _ => new FixedWindowRateLimiterOptions
                    {
                        PermitLimit = permitLimit,
                        Window = TimeSpan.FromSeconds(windowSeconds),
                        QueueLimit = queueLimit,
                        AutoReplenishment = true
                    });
            });
        });

        return services;
    }

    public static string ResolveClientIp(HttpContext httpContext)
    {
        var forwardedFor = httpContext.Request.Headers["X-Forwarded-For"].FirstOrDefault();
        if (!string.IsNullOrWhiteSpace(forwardedFor))
        {
            var firstIp = forwardedFor.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).FirstOrDefault();
            if (!string.IsNullOrWhiteSpace(firstIp))
            {
                return firstIp;
            }
        }

        return httpContext.Connection.RemoteIpAddress?.ToString() ?? "unknown-client";
    }
}