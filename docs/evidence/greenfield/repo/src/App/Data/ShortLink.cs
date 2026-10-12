namespace App.Data;

public class ShortLink
{
    public int Id { get; set; }
    public string Code { get; set; } = string.Empty;
    public string OriginalUrl { get; set; } = string.Empty;
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public DateTime? ExpiresAt { get; set; } 
    public long ClickCount { get; set; } = 0;
    public DateTime? LastVisitedAt { get; set; }
}