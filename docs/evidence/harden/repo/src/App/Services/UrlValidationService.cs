using System.Net;
using System.Net.Sockets;

namespace App.Services;

public interface IUrlValidationService
{
    Task<(bool IsValid, string? ErrorMessage)> ValidateUrlAsync(string? url, CancellationToken cancellationToken = default);
}

public class UrlValidationService : IUrlValidationService
{
    public async Task<(bool IsValid, string? ErrorMessage)> ValidateUrlAsync(string? url, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(url))
        {
            return (false, "URL cannot be empty.");
        }

        if (!Uri.TryCreate(url.Trim(), UriKind.Absolute, out var uri))
        {
            return (false, "The provided URL is not a valid absolute URI.");
        }

        if (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps)
        {
            return (false, "Only HTTP and HTTPS protocols are permitted.");
        }

        var host = uri.DnsSafeHost;
        if (string.IsNullOrWhiteSpace(host))
        {
            return (false, "URL host is missing or invalid.");
        }

        if (string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase) ||
            host.EndsWith(".localhost", StringComparison.OrdinalIgnoreCase))
        {
            return (false, "Target URL resolves to a loopback address (SSRF prevention).");
        }

        var hostToParse = host.Trim('[', ']');
        if (IPAddress.TryParse(hostToParse, out var directIp))
        {
            if (IsPrivateOrRestricted(directIp))
            {
                return (false, "Target URL resolves to a private or restricted network address (SSRF prevention).");
            }
            return (true, null);
        }

        try
        {
            var resolvedIps = await Dns.GetHostAddressesAsync(host, cancellationToken);
            if (resolvedIps.Length > 0)
            {
                foreach (var ip in resolvedIps)
                {
                    if (IsPrivateOrRestricted(ip))
                    {
                        return (false, "Target URL resolves to a private or restricted network address (SSRF prevention).");
                    }
                }
            }
        }
        catch
        {
        }

        return (true, null);
    }

    public static bool IsPrivateOrRestricted(IPAddress address)
    {
        if (address.IsIPv4MappedToIPv6)
        {
            address = address.MapToIPv4();
        }

        if (IPAddress.IsLoopback(address))
        {
            return true;
        }

        if (address.AddressFamily == AddressFamily.InterNetwork)
        {
            var bytes = address.GetAddressBytes();
            byte b0 = bytes[0];
            byte b1 = bytes[1];

            if (b0 == 0) return true;
            if (b0 == 10) return true;
            if (b0 == 100 && (b1 >= 64 && b1 <= 127)) return true;
            if (b0 == 127) return true;
            if (b0 == 169 && b1 == 254) return true;
            if (b0 == 172 && (b1 >= 16 && b1 <= 31)) return true;
            if (b0 == 192 && b1 == 0 && bytes[2] == 0) return true;
            if (b0 == 192 && b1 == 0 && bytes[2] == 2) return true;
            if (b0 == 192 && b1 == 168) return true;
            if (b0 == 198 && (b1 == 18 || b1 == 19)) return true;
            if (b0 == 198 && b1 == 51 && bytes[2] == 100) return true;
            if (b0 == 203 && b1 == 0 && bytes[2] == 113) return true;
            if (b0 >= 224 && b0 <= 239) return true;
            if (b0 >= 240) return true;

            return false;
        }

        if (address.AddressFamily == AddressFamily.InterNetworkV6)
        {
            if (address.Equals(IPAddress.IPv6Any) ||
                address.Equals(IPAddress.IPv6None) ||
                address.Equals(IPAddress.IPv6Loopback))
            {
                return true;
            }

            if (address.IsIPv6LinkLocal || address.IsIPv6Multicast || address.IsIPv6SiteLocal)
            {
                return true;
            }

            var bytes = address.GetAddressBytes();
            if ((bytes[0] & 0xFE) == 0xFC)
            {
                return true;
            }

            return false;
        }

        return true;
    }
}