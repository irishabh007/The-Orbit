using System.Security.Cryptography;
using System.Text.RegularExpressions;

namespace App.Services;

public interface ICodeGeneratorService
{
    string GenerateCode(int length = 7);
    (bool IsValid, string? ErrorMessage) ValidateCustomAlias(string? alias);
}

public class CodeGeneratorService : ICodeGeneratorService
{
    private const string Base62Chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    private static readonly Regex AliasRegex = new("^[a-zA-Z0-9_-]{3,64}$", RegexOptions.Compiled);

    public string GenerateCode(int length = 7)
    {
        if (length <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(length), "Length must be greater than zero.");
        }

        return RandomNumberGenerator.GetString(Base62Chars, length);
    }

    public (bool IsValid, string? ErrorMessage) ValidateCustomAlias(string? alias)
    {
        if (string.IsNullOrWhiteSpace(alias))
        {
            return (false, "Custom alias cannot be empty.");
        }

        if (alias.Length < 3 || alias.Length > 64)
        {
            return (false, "Custom alias must be between 3 and 64 characters in length.");
        }

        if (!AliasRegex.IsMatch(alias))
        {
            return (false, "Custom alias may only contain alphanumeric characters, hyphens, and underscores.");
        }

        return (true, null);
    }
}