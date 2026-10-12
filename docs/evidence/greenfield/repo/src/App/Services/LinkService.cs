using App.Data;
using App.Models;
using Microsoft.EntityFrameworkCore;

namespace App.Services;

public enum ShortenStatus
{
    Success,
    BadRequest,
    Conflict
}

public sealed record ShortenResult(
    ShortenStatus Status,
    ShortenResponse? Response = null,
    string? ErrorMessage = null
);

public enum RedirectStatus
{
    Success,
    NotFound,
    Expired
}

public sealed record RedirectResult(
    RedirectStatus Status,
    string? TargetUrl = null
);

public sealed record AnalyticsResult(
    bool Found,
    AnalyticsResponse? Response = null
);

public interface ILinkService
{
    Task<ShortenResult> CreateShortLinkAsync(ShortenRequest request, string baseUrl, CancellationToken cancellationToken = default);
    Task<RedirectResult> GetRedirectAsync(string code, CancellationToken cancellationToken = default);
    Task<AnalyticsResult> GetAnalyticsAsync(string code, CancellationToken cancellationToken = default);
}

public class LinkService : ILinkService
{
    private readonly AppDbContext _dbContext;
    private readonly IUrlValidationService _urlValidationService;
    private readonly ICodeGeneratorService _codeGeneratorService;

    public LinkService(
        AppDbContext dbContext,
        IUrlValidationService urlValidationService,
        ICodeGeneratorService codeGeneratorService)
    {
        _dbContext = dbContext;
        _urlValidationService = urlValidationService;
        _codeGeneratorService = codeGeneratorService;
    }

    public async Task<ShortenResult> CreateShortLinkAsync(ShortenRequest request, string baseUrl, CancellationToken cancellationToken = default)
    {
        if (request == null || string.IsNullOrWhiteSpace(request.Url))
        {
            return new ShortenResult(ShortenStatus.BadRequest, ErrorMessage: "Url is required.");
        }

        var (isValidUrl, urlError) = await _urlValidationService.ValidateUrlAsync(request.Url, cancellationToken);
        if (!isValidUrl)
        {
            return new ShortenResult(ShortenStatus.BadRequest, ErrorMessage: urlError ?? "Invalid URL.");
        }

        string code;
        if (!string.IsNullOrWhiteSpace(request.CustomAlias))
        {
            var (isValidAlias, aliasError) = _codeGeneratorService.ValidateCustomAlias(request.CustomAlias);
            if (!isValidAlias)
            {
                return new ShortenResult(ShortenStatus.BadRequest, ErrorMessage: aliasError ?? "Invalid custom alias.");
            }

            var aliasConflict = await _dbContext.ShortLinks.AnyAsync(l => l.Code == request.CustomAlias, cancellationToken);
            if (aliasConflict)
            {
                return new ShortenResult(ShortenStatus.Conflict, ErrorMessage: "The specified custom alias is already in use.");
            }

            code = request.CustomAlias;
        }
        else
        {
            const int maxRetries = 10;
            string generatedCode = string.Empty;
            bool uniqueFound = false;

            for (int i = 0; i < maxRetries; i++)
            {
                generatedCode = _codeGeneratorService.GenerateCode();
                var codeExists = await _dbContext.ShortLinks.AnyAsync(l => l.Code == generatedCode, cancellationToken);
                if (!codeExists)
                {
                    uniqueFound = true;
                    break;
                }
            }

            if (!uniqueFound)
            {
                generatedCode = _codeGeneratorService.GenerateCode(8);
            }

            code = generatedCode;
        }

        DateTime? expiresAtUtc = null;
        if (request.ExpiresAt.HasValue)
        {
            expiresAtUtc = request.ExpiresAt.Value.Kind == DateTimeKind.Utc
                ? request.ExpiresAt.Value
                : request.ExpiresAt.Value.ToUniversalTime();
        }

        var shortLink = new ShortLink
        {
            Code = code,
            OriginalUrl = request.Url.Trim(),
            CreatedAt = DateTime.UtcNow,
            ExpiresAt = expiresAtUtc,
            ClickCount = 0,
            LastVisitedAt = null
        };

        try
        {
            _dbContext.ShortLinks.Add(shortLink);
            await _dbContext.SaveChangesAsync(cancellationToken);
        }
        catch (DbUpdateException)
        {
            if (!string.IsNullOrWhiteSpace(request.CustomAlias))
            {
                return new ShortenResult(ShortenStatus.Conflict, ErrorMessage: "The specified custom alias is already in use.");
            }
            return new ShortenResult(ShortenStatus.Conflict, ErrorMessage: "A collision occurred while generating the code. Please try again.");
        }

        var shortUrl = $"{baseUrl.TrimEnd('/')}/{code}";
        var response = new ShortenResponse(
            Code: code,
            ShortUrl: shortUrl,
            Url: shortLink.OriginalUrl,
            CreatedAt: shortLink.CreatedAt,
            ExpiresAt: shortLink.ExpiresAt
        );

        return new ShortenResult(ShortenStatus.Success, Response: response);
    }

    public async Task<RedirectResult> GetRedirectAsync(string code, CancellationToken cancellationToken = default)
    {
        var link = await _dbContext.ShortLinks.FirstOrDefaultAsync(l => l.Code == code, cancellationToken);
        if (link == null)
        {
            return new RedirectResult(RedirectStatus.NotFound);
        }

        if (link.ExpiresAt.HasValue && link.ExpiresAt.Value <= DateTime.UtcNow)
        {
            return new RedirectResult(RedirectStatus.Expired);
        }

        link.ClickCount += 1;
        link.LastVisitedAt = DateTime.UtcNow;
        await _dbContext.SaveChangesAsync(cancellationToken);

        return new RedirectResult(RedirectStatus.Success, TargetUrl: link.OriginalUrl);
    }

    public async Task<AnalyticsResult> GetAnalyticsAsync(string code, CancellationToken cancellationToken = default)
    {
        var link = await _dbContext.ShortLinks.AsNoTracking().FirstOrDefaultAsync(l => l.Code == code, cancellationToken);
        if (link == null)
        {
            return new AnalyticsResult(false);
        }

        bool isExpired = link.ExpiresAt.HasValue && link.ExpiresAt.Value <= DateTime.UtcNow;
        var response = new AnalyticsResponse(
            Code: link.Code,
            Url: link.OriginalUrl,
            TotalClicks: link.ClickCount,
            CreatedAt: link.CreatedAt,
            LastVisitedAt: link.LastVisitedAt,
            ExpiresAt: link.ExpiresAt,
            Expired: isExpired
        );

        return new AnalyticsResult(true, response);
    }
}