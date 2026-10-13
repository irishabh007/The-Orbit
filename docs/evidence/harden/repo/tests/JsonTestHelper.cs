using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text;
using System.Text.Json;

namespace Tests;

public static class JsonTestHelper
{
    public static StringContent CreateJsonContent(object payload)
    {
        var json = JsonSerializer.Serialize(payload);
        return new StringContent(json, Encoding.UTF8, "application/json");
    }

    public static string? GetStringProperty(JsonElement element, params string[] candidateNames)
    {
        foreach (var name in candidateNames)
        {
            if (element.TryGetProperty(name, out var prop) && prop.ValueKind == JsonValueKind.String)
            {
                return prop.GetString();
            }
        }

        foreach (var prop in element.EnumerateObject())
        {
            foreach (var name in candidateNames)
            {
                if (string.Equals(prop.Name, name, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(prop.Name.Replace("_", ""), name.Replace("_", ""), StringComparison.OrdinalIgnoreCase))
                {
                    if (prop.Value.ValueKind == JsonValueKind.String)
                    {
                        return prop.Value.GetString();
                    }
                }
            }
        }

        return null;
    }

    public static long? GetInt64Property(JsonElement element, params string[] candidateNames)
    {
        foreach (var name in candidateNames)
        {
            if (element.TryGetProperty(name, out var prop))
            {
                var val = ExtractInt64(prop);
                if (val.HasValue) return val;
            }
        }

        foreach (var prop in element.EnumerateObject())
        {
            foreach (var name in candidateNames)
            {
                if (string.Equals(prop.Name, name, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(prop.Name.Replace("_", ""), name.Replace("_", ""), StringComparison.OrdinalIgnoreCase))
                {
                    var val = ExtractInt64(prop.Value);
                    if (val.HasValue) return val;
                }
            }
        }

        return null;
    }

    private static long? ExtractInt64(JsonElement prop)
    {
        if (prop.ValueKind == JsonValueKind.Number)
        {
            if (prop.TryGetInt64(out var val)) return val;
            if (prop.TryGetDouble(out var d)) return (long)d;
        }
        else if (prop.ValueKind == JsonValueKind.String)
        {
            if (long.TryParse(prop.GetString(), out var parsed)) return parsed;
        }
        return null;
    }

    public static JsonElement? GetArrayProperty(JsonElement element, params string[] candidateNames)
    {
        foreach (var name in candidateNames)
        {
            if (element.TryGetProperty(name, out var prop) && prop.ValueKind == JsonValueKind.Array)
            {
                return prop;
            }
        }

        foreach (var prop in element.EnumerateObject())
        {
            foreach (var name in candidateNames)
            {
                if (string.Equals(prop.Name, name, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(prop.Name.Replace("_", ""), name.Replace("_", ""), StringComparison.OrdinalIgnoreCase))
                {
                    if (prop.Value.ValueKind == JsonValueKind.Array)
                    {
                        return prop.Value;
                    }
                }
            }
        }

        return null;
    }

    public static bool PropertyIsNull(JsonElement element, params string[] candidateNames)
    {
        foreach (var name in candidateNames)
        {
            if (element.TryGetProperty(name, out var prop))
            {
                return prop.ValueKind == JsonValueKind.Null;
            }
        }

        foreach (var prop in element.EnumerateObject())
        {
            foreach (var name in candidateNames)
            {
                if (string.Equals(prop.Name, name, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(prop.Name.Replace("_", ""), name.Replace("_", ""), StringComparison.OrdinalIgnoreCase))
                {
                    return prop.Value.ValueKind == JsonValueKind.Null;
                }
            }
        }

        return true;
    }
}