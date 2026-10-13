using System.Security.Cryptography;
using App.Data;
using App.Models;
using Microsoft.Data.Sqlite;
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

public enum DailyAnalyticsStatus
{
    Success,
    NotFound,
    Expired,
    BadRequest
}

public sealed record DailyAnalyticsResult(
    DailyAnalyticsStatus Status,
    DailyAnalyticsResponse? Response = null,
    string? ErrorMessage = null
);

public interface ILinkService
{
    Task<ShortenResult> CreateShortLinkAsync(ShortenRequest request, string baseUrl, CancellationToken cancellationToken = default);
    Task<RedirectResult> GetRedirectAsync(string code, CancellationToken cancellationToken = default);
    Task<AnalyticsResult> GetAnalyticsAsync(string code, CancellationToken cancellationToken = default);
    Task<DailyAnalyticsResult> GetDailyAnalyticsAsync(string code, int days, CancellationToken cancellationToken = default);
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
            CreatedAtUtc = DateTime.UtcNow,
            ExpiresAtUtc = expiresAtUtc,
            ClickCount = 0,
            LastVisitedAtUtc = null
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
            CreatedAt: shortLink.CreatedAtUtc,
            ExpiresAt: shortLink.ExpiresAtUtc
        );

        return new ShortenResult(ShortenStatus.Success, Response: response);
    }

    public async Task<RedirectResult> GetRedirectAsync(string code, CancellationToken cancellationToken = default)
    {
        var link = await _dbContext.ShortLinks
            .AsNoTracking()
            .Where(l => l.Code == code)
            .Select(l => new { l.Id, l.OriginalUrl, l.ExpiresAtUtc })
            .FirstOrDefaultAsync(cancellationToken);

        if (link == null)
        {
            return new RedirectResult(RedirectStatus.NotFound);
        }

        if (link.ExpiresAtUtc.HasValue && link.ExpiresAtUtc.Value <= DateTime.UtcNow)
        {
            return new RedirectResult(RedirectStatus.Expired);
        }

        var nowUtc = DateTime.UtcNow;
        var todayUtc = DateOnly.FromDateTime(nowUtc);
        var dateString = todayUtc.ToString("yyyy-MM-dd");

        await ExecuteWithRetryAsync(async () =>
        {
            await _dbContext.ShortLinks
                .Where(s => s.Id == link.Id)
                .ExecuteUpdateAsync(s => s
                    .SetProperty(b => b.ClickCount, b => b.ClickCount + 1)
                    .SetProperty(b => b.LastVisitedAtUtc, nowUtc),
                    cancellationToken);
        }, cancellationToken);

        await ExecuteWithRetryAsync(async () =>
        {
            await _dbContext.Database.ExecuteSqlInterpolatedAsync($@"
                INSERT INTO ShortLinkDailyClicks (ShortLinkId, Date, Clicks)
                VALUES ({link.Id}, {dateString}, 1)
                ON CONFLICT(ShortLinkId, Date) DO UPDATE SET Clicks = ShortLinkDailyClicks.Clicks + 1;
            ", cancellationToken);
        }, cancellationToken);

        return new RedirectResult(RedirectStatus.Success, TargetUrl: link.OriginalUrl);
    }

    private static async Task ExecuteWithRetryAsync(Func<Task> action, CancellationToken cancellationToken)
    {
        const int maxRetries = 20;
        for (int attempt = 0; attempt < maxRetries; attempt++)
        {
            try
            {
                await action();
                return;
            }
            catch (Exception ex) when (IsSqliteBusy(ex))
            {
                if (attempt == maxRetries - 1) throw;
                await Task.Delay(RandomNumberGenerator.GetInt32(10, 50) * (attempt + 1), cancellationToken);
            }
        }
    }

    private static bool IsSqliteBusy(Exception ex)
    {
        var current = ex;
        while (current != null)
        {
            if (current is SqliteException sqliteEx)
            {
                if (sqliteEx.SqliteErrorCode == 5 ||
                    sqliteEx.SqliteErrorCode == 6 ||
                    (sqliteEx.SqliteErrorCode & 0xFF) == 5 ||
                    (sqliteEx.SqliteErrorCode & 0xFF) == 6 ||
                    sqliteEx.Message.Contains("busy", StringComparison.OrdinalIgnoreCase) ||
                    sqliteEx.Message.Contains("locked", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            if (current.Message.Contains("busy", StringComparison.OrdinalIgnoreCase) ||
                current.Message.Contains("locked", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
            current = current.InnerException!;
        }
        return false;
    }

    public async Task<AnalyticsResult> GetAnalyticsAsync(string code, CancellationToken cancellationToken = default)
    {
        var link = await _dbContext.ShortLinks.AsNoTracking().FirstOrDefaultAsync(l => l.Code == code, cancellationToken);
        if (link == null || (link.ExpiresAtUtc.HasValue && link.ExpiresAtUtc.Value <= DateTime.UtcNow))
        {
            return new AnalyticsResult(false);
        }

        var response = new AnalyticsResponse(
            Code: link.Code,
            Url: link.OriginalUrl,
            TotalClicks: link.ClickCount,
            CreatedAt: link.CreatedAtUtc,
            LastVisitedAt: link.LastVisitedAtUtc,
            ExpiresAt: link.ExpiresAtUtc,
            Expired: false
        );

        return new AnalyticsResult(true, response);
    }

    public async Task<DailyAnalyticsResult> GetDailyAnalyticsAsync(string code, int days, CancellationToken cancellationToken = default)
    {
        if (days < 1 || days > 90)
        {
            return new DailyAnalyticsResult(DailyAnalyticsStatus.BadRequest, ErrorMessage: "The days parameter must be an integer between 1 and 90.");
        }

        var link = await _dbContext.ShortLinks.AsNoTracking().FirstOrDefaultAsync(l => l.Code == code, cancellationToken);
        if (link == null || (link.ExpiresAtUtc.HasValue && link.ExpiresAtUtc.Value <= DateTime.UtcNow))
        {
            return new DailyAnalyticsResult(DailyAnalyticsStatus.NotFound, ErrorMessage: "Short code not found.");
        }

        var todayUtc = DateOnly.FromDateTime(DateTime.UtcNow);
        var startDate = todayUtc.AddDays(-(days - 1));

        var dbClicks = await _dbContext.ShortLinkDailyClicks
            .AsNoTracking()
            .Where(d => d.ShortLinkId == link.Id && d.Date >= startDate && d.Date <= todayUtc)
            .ToListAsync(cancellationToken);

        var clicksMap = dbClicks
            .GroupBy(d => d.Date)
            .ToDictionary(g => g.Key, g => g.Sum(x => x.Clicks));

        var dailyRecords = new List<DailyClickRecord>(days);
        for (var d = startDate; d <= todayUtc; d = d.AddDays(1))
        {
            var clicks = clicksMap.TryGetValue(d, out var count) ? count : 0L;
            dailyRecords.Add(new DailyClickRecord(d.ToString("yyyy-MM-dd"), clicks));
        }

        var response = new DailyAnalyticsResponse(
            Code: link.Code,
            TotalClicks: link.ClickCount,
            Days: days,
            Daily: dailyRecords
        );

        return new DailyAnalyticsResult(DailyAnalyticsStatus.Success, Response: response);
    }
}