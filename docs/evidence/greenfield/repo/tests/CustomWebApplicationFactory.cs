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
        : this(null, 60, true)
    {
    }

    internal CustomWebApplicationFactory(string? dbPath, int permitLimit = 60, bool deleteDbOnDispose = true)
    {
        _dbPath = dbPath ?? Path.Combine(Path.GetTempPath(), $"shortener_{Guid.NewGuid():N}.db");
        _permitLimit = permitLimit;
        _deleteDbOnDispose = deleteDbOnDispose;

        Environment.SetEnvironmentVariable("ConnectionStrings__DefaultConnection", $"Data Source={_dbPath}");
        Environment.SetEnvironmentVariable("ConnectionStrings__SqliteConnection", $"Data Source={_dbPath}");
        Environment.SetEnvironmentVariable("ConnectionStrings__Sqlite", $"Data Source={_dbPath}");
        Environment.SetEnvironmentVariable("ConnectionStrings__Database", $"Data Source={_dbPath}");
        Environment.SetEnvironmentVariable("RateLimiting__PermitLimit", _permitLimit.ToString());
        Environment.SetEnvironmentVariable("RateLimiting__Limit", _permitLimit.ToString());
        Environment.SetEnvironmentVariable("RateLimit__PermitLimit", _permitLimit.ToString());
    }

    public string DbPath => _dbPath;

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.UseEnvironment("Testing");

        builder.UseSetting("ConnectionStrings:DefaultConnection", $"Data Source={_dbPath}");
        builder.UseSetting("ConnectionStrings:SqliteConnection", $"Data Source={_dbPath}");
        builder.UseSetting("ConnectionStrings:Sqlite", $"Data Source={_dbPath}");
        builder.UseSetting("ConnectionStrings:Database", $"Data Source={_dbPath}");
        builder.UseSetting("RateLimiting:PermitLimit", _permitLimit.ToString());
        builder.UseSetting("RateLimiting:Limit", _permitLimit.ToString());
        builder.UseSetting("RateLimit:PermitLimit", _permitLimit.ToString());

        builder.ConfigureAppConfiguration((_, config) =>
        {
            config.AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["ConnectionStrings:DefaultConnection"] = $"Data Source={_dbPath}",
                ["ConnectionStrings:SqliteConnection"] = $"Data Source={_dbPath}",
                ["ConnectionStrings:Sqlite"] = $"Data Source={_dbPath}",
                ["ConnectionStrings:Database"] = $"Data Source={_dbPath}",
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