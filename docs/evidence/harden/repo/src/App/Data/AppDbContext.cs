using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Storage.ValueConversion;

namespace App.Data;

public class AppDbContext : DbContext
{
    public AppDbContext(DbContextOptions<AppDbContext> options)
        : base(options)
    {
    }

    public DbSet<ShortLink> ShortLinks => Set<ShortLink>();
    public DbSet<ShortLinkDailyClick> ShortLinkDailyClicks => Set<ShortLinkDailyClick>();
    public DbSet<ShortLinkDailyClick> DailyClicks => ShortLinkDailyClicks;

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        base.OnModelCreating(modelBuilder);

        var utcConverter = new ValueConverter<DateTime, DateTime>(
            v => v.Kind == DateTimeKind.Utc ? v : DateTime.SpecifyKind(v, DateTimeKind.Utc),
            v => DateTime.SpecifyKind(v, DateTimeKind.Utc));

        var nullableUtcConverter = new ValueConverter<DateTime?, DateTime?>(
            v => v.HasValue ? (v.Value.Kind == DateTimeKind.Utc ? v.Value : DateTime.SpecifyKind(v.Value, DateTimeKind.Utc)) : v,
            v => v.HasValue ? DateTime.SpecifyKind(v.Value, DateTimeKind.Utc) : v);

        modelBuilder.Entity<ShortLink>(entity =>
        {
            entity.ToTable("ShortLinks");
            entity.HasKey(e => e.Id);

            entity.Property(e => e.Code)
                .IsRequired()
                .HasMaxLength(64);

            entity.HasIndex(e => e.Code)
                .IsUnique();

            entity.Property(e => e.OriginalUrl)
                .IsRequired();

            entity.Property(e => e.ClickCount)
                .IsRequired()
                .HasDefaultValue(0L);

            entity.Property(e => e.CreatedAtUtc)
                .IsRequired()
                .HasConversion(utcConverter);

            entity.Property(e => e.LastVisitedAtUtc)
                .HasConversion(nullableUtcConverter);

            entity.Property(e => e.ExpiresAtUtc)
                .HasConversion(nullableUtcConverter);

            entity.HasIndex(e => e.ExpiresAtUtc);
        });

        modelBuilder.Entity<ShortLinkDailyClick>(entity =>
        {
            entity.ToTable("ShortLinkDailyClicks");
            entity.HasKey(e => e.Id);

            entity.Property(e => e.ShortLinkId)
                .IsRequired();

            entity.Property(e => e.Date)
                .IsRequired();

            entity.Property(e => e.Clicks)
                .IsRequired()
                .HasDefaultValue(0L);

            entity.HasIndex(e => new { e.ShortLinkId, e.Date })
                .IsUnique();

            entity.HasOne(e => e.ShortLink)
                .WithMany(s => s.DailyClicks)
                .HasForeignKey(e => e.ShortLinkId)
                .OnDelete(DeleteBehavior.Cascade);
        });
    }
}