using App.Data;
using App.Middleware;
using App.Models;
using App.Services;
using Microsoft.EntityFrameworkCore;

namespace App.Endpoints;

public static class LinkEndpoints
{
    public static IEndpointRouteBuilder MapLinkEndpoints(this IEndpointRouteBuilder app)
    {
        app.MapGet("/health", async (AppDbContext dbContext, CancellationToken ct) =>
        {
            try
            {
                if (!await dbContext.Database.CanConnectAsync(ct))
                {
                    return Results.Json(new HealthResponse("unhealthy"), statusCode: StatusCodes.Status503ServiceUnavailable);
                }

                _ = await dbContext.ShortLinks.Take(1).ToListAsync(ct);

                return Results.Ok(new HealthResponse("healthy"));
            }
            catch
            {
                return Results.Json(new HealthResponse("unhealthy"), statusCode: StatusCodes.Status503ServiceUnavailable);
            }
        });

        app.MapPost("/shorten", async (ShortenRequest? request, ILinkService linkService, HttpContext httpContext, CancellationToken ct) =>
        {
            if (request == null || string.IsNullOrWhiteSpace(request.Url))
            {
                return Results.BadRequest(new ErrorResponse("Url is required."));
            }

            var scheme = httpContext.Request.Scheme;
            var host = httpContext.Request.Host.Value;
            var baseUrl = $"{scheme}://{host}";

            var result = await linkService.CreateShortLinkAsync(request, baseUrl, ct);
            return result.Status switch
            {
                ShortenStatus.Success => Results.Created($"/{result.Response!.Code}", result.Response),
                ShortenStatus.BadRequest => Results.BadRequest(new ErrorResponse(result.ErrorMessage ?? "Bad Request")),
                ShortenStatus.Conflict => Results.Conflict(new ErrorResponse(result.ErrorMessage ?? "Conflict")),
                _ => Results.StatusCode(StatusCodes.Status500InternalServerError)
            };
        })
        .RequireRateLimiting(RateLimitingConfiguration.ShortenPolicyName);

        app.MapGet("/analytics/{code}", async (string code, ILinkService linkService, CancellationToken ct) =>
        {
            var result = await linkService.GetAnalyticsAsync(code, ct);
            if (!result.Found || result.Response == null)
            {
                return Results.NotFound(new ErrorResponse("Short code not found."));
            }

            return Results.Ok(result.Response);
        });

        app.MapGet("/{code}", async (string code, ILinkService linkService, CancellationToken ct) =>
        {
            if (code.Equals("health", StringComparison.OrdinalIgnoreCase) ||
                code.Equals("shorten", StringComparison.OrdinalIgnoreCase) ||
                code.Equals("analytics", StringComparison.OrdinalIgnoreCase))
            {
                return Results.NotFound(new ErrorResponse("Not found."));
            }

            var result = await linkService.GetRedirectAsync(code, ct);
            return result.Status switch
            {
                RedirectStatus.Success => Results.Redirect(result.TargetUrl!, permanent: false, preserveMethod: true),
                _ => Results.NotFound(new ErrorResponse("Short code not found or link has expired."))
            };
        });

        return app;
    }
}