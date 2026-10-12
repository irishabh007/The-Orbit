using App.Data;
using App.Endpoints;
using App.Middleware;
using App.Services;
using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddEndpointsApiExplorer();
builder.Services.AddOpenApi();

var connectionString = builder.Configuration.GetConnectionString("DefaultConnection")
    ?? "Data Source=shortener.db;Cache=Shared;Mode=ReadWriteCreate";

builder.Services.AddDbContext<AppDbContext>(options =>
{
    options.UseSqlite(connectionString);
});

builder.Services.AddSingleton<IUrlValidationService, UrlValidationService>();
builder.Services.AddSingleton<ICodeGeneratorService, CodeGeneratorService>();
builder.Services.AddScoped<ILinkService, LinkService>();

builder.Services.AddShortenRateLimiter(builder.Configuration);

var app = builder.Build();

app.UseRateLimiter();

using (var scope = app.Services.CreateScope())
{
    try
    {
        var db = scope.ServiceProvider.GetRequiredService<AppDbContext>();
        try
        {
            await db.Database.EnsureCreatedAsync();
        }
        catch
        {
            // Database or tables may already exist or be initialized concurrently by test hosts
        }

        try
        {
            if (db.Database.IsSqlite() && !db.Database.IsInMemory())
            {
                await db.Database.ExecuteSqlRawAsync("PRAGMA journal_mode = WAL;");
            }
        }
        catch
        {
            // In-memory SQLite or non-standard providers ignore PRAGMA journal_mode
        }
    }
    catch
    {
        // Datastore might be deliberately unconfigured or unavailable in test scenarios
    }
}

app.MapLinkEndpoints();

app.Run();

public partial class Program { }