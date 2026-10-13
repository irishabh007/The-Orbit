using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.Configuration;

namespace Tests;

public class CustomWebApplicationFactory : WebApplicationFactory<Program>
{
    private readonly string _dbPath;
    private readonly int _permitLimit;
    private readonly bool _deleteDbOnDispose;

    public CustomWebApplicationFactory()
        : this(null, 1000, true)
    {
    }

    internal CustomWebApplicationFactory(string? dbPath, int permitLimit = 1000, bool deleteDbOnDispose = true)
    {
        _dbPath = dbPath ?? Path.Combine(Path.GetTempPath(), $"shortener_{Guid.NewGuid():N}.db");
        _permitLimit = permitLimit;
        _deleteDbOnDispose = deleteDbOnDispose;
        var connectionString = $"Data Source={_dbPath};Default Timeout=30";

        Environment.SetEnvironmentVariable("ConnectionStrings__DefaultConnection", connectionString);
        Environment.SetEnvironmentVariable("ConnectionStrings__Default", connectionString);
        Environment.SetEnvironmentVariable("ConnectionStrings__SqliteConnection", connectionString);
        Environment.SetEnvironmentVariable("ConnectionStrings__Sqlite", connectionString);
        Environment.SetEnvironmentVariable("ConnectionStrings__Database", connectionString);
        Environment.SetEnvironmentVariable("ConnectionStrings__AppDbContext", connectionString);
        Environment.SetEnvironmentVariable("RateLimiting__PermitLimit", _permitLimit.ToString());
        Environment.SetEnvironmentVariable("RateLimiting__Limit", _permitLimit.ToString());
        Environment.SetEnvironmentVariable("RateLimit__PermitLimit", _permitLimit.ToString());
    }

    public string DbPath => _dbPath;

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        var connectionString = $"Data Source={_dbPath};Default Timeout=30";
        builder.UseEnvironment("Testing");

        builder.UseSetting("ConnectionStrings:DefaultConnection", connectionString);
        builder.UseSetting("ConnectionStrings:Default", connectionString);
        builder.UseSetting("ConnectionStrings:SqliteConnection", connectionString);
        builder.UseSetting("ConnectionStrings:Sqlite", connectionString);
        builder.UseSetting("ConnectionStrings:Database", connectionString);
        builder.UseSetting("ConnectionStrings:AppDbContext", connectionString);
        builder.UseSetting("RateLimiting:PermitLimit", _permitLimit.ToString());
        builder.UseSetting("RateLimiting:Limit", _permitLimit.ToString());
        builder.UseSetting("RateLimit:PermitLimit", _permitLimit.ToString());

        builder.ConfigureAppConfiguration((_, config) =>
        {
            config.AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["ConnectionStrings:DefaultConnection"] = connectionString,
                ["ConnectionStrings:Default"] = connectionString,
                ["ConnectionStrings:SqliteConnection"] = connectionString,
                ["ConnectionStrings:Sqlite"] = connectionString,
                ["ConnectionStrings:Database"] = connectionString,
                ["ConnectionStrings:AppDbContext"] = connectionString,
                ["RateLimiting:PermitLimit"] = _permitLimit.ToString(),
                ["RateLimiting:Limit"] = _permitLimit.ToString(),
                ["RateLimit:PermitLimit"] = _permitLimit.ToString()
            });
        });
    }

    public HttpClient CreateNoRedirectClient()
    {
        return CreateClient(new WebApplicationFactoryClientOptions
        {
            AllowAutoRedirect = false
        });
    }

    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);
        if (disposing && _deleteDbOnDispose)
        {
            try
            {
                Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
            }
            catch
            {
            }

            try { if (File.Exists(_dbPath)) File.Delete(_dbPath); } catch { }
            try { if (File.Exists(_dbPath + "-wal")) File.Delete(_dbPath + "-wal"); } catch { }
            try { if (File.Exists(_dbPath + "-shm")) File.Delete(_dbPath + "-shm"); } catch { }
        }
    }
}