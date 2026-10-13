using App.Data;
using App.Endpoints;
using App.Middleware;
using App.Services;
using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddEndpointsApiExplorer();
builder.Services.AddOpenApi();

var connectionString = builder.Configuration.GetConnectionString("DefaultConnection")
    ?? "Data Source=shortener.db;Cache=Shared;Mode=ReadWriteCreate;Default Timeout=30";

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
    var db = scope.ServiceProvider.GetRequiredService<AppDbContext>();
    await DatabaseBootstrapper.BootstrapAsync(db);
}

app.MapLinkEndpoints();

app.Run();

public partial class Program { }