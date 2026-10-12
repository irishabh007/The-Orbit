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
            if (element.TryGetProperty(name, out var prop) && prop.TryGetInt64(out var val))
            {
                return val;
            }
        }

        foreach (var prop in element.EnumerateObject())
        {
            foreach (var name in candidateNames)
            {
                if (string.Equals(prop.Name, name, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(prop.Name.Replace("_", ""), name.Replace("_", ""), StringComparison.OrdinalIgnoreCase))
                {
                    if (prop.Value.TryGetInt64(out var val))
                    {
                        return val;
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