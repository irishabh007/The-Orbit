using System.ComponentModel.DataAnnotations.Schema;

namespace App.Data;

public class ShortLink
{
    public long Id { get; set; }
    public string Code { get; set; } = string.Empty;
    public string OriginalUrl { get; set; } = string.Empty;
    public long ClickCount { get; set; } = 0;
    public DateTime CreatedAtUtc { get; set; } = DateTime.UtcNow;
    public DateTime? LastVisitedAtUtc { get; set; }
    public DateTime? ExpiresAtUtc { get; set; }

    [NotMapped]
    public DateTime CreatedAt
    {
        get => CreatedAtUtc;
        set => CreatedAtUtc = value;
    }

    [NotMapped]
    public DateTime? LastVisitedAt
    {
        get => LastVisitedAtUtc;
        set => LastVisitedAtUtc = value;
    }

    [NotMapped]
    public DateTime? ExpiresAt
    {
        get => ExpiresAtUtc;
        set => ExpiresAtUtc = value;
    }

    public ICollection<ShortLinkDailyClick> DailyClicks { get; set; } = new List<ShortLinkDailyClick>();
}