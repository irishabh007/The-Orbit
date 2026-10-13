namespace App.Data;

public class ShortLinkDailyClick
{
    public long Id { get; set; }
    public long ShortLinkId { get; set; }
    public ShortLink? ShortLink { get; set; }
    public DateOnly Date { get; set; } 
    public long Clicks { get; set; }
}