using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.ServiceProcess;
using System.Management;
using Microsoft.Win32;

namespace AutopilotAgent;

public sealed class TelemetryCollector
{
    public TelemetrySnapshot Collect()
    {
        var interfaces = NetworkInterface.GetAllNetworkInterfaces()
            .Where(nic => nic.OperationalStatus == OperationalStatus.Up)
            .Select(nic => new NicSnapshot(
                nic.Name,
                nic.NetworkInterfaceType.ToString(),
                nic.GetPhysicalAddress().ToString(),
                nic.GetIPProperties().UnicastAddresses
                    .Where(address => address.Address.AddressFamily == AddressFamily.InterNetwork)
                    .Select(address => address.Address.ToString())
                    .Where(ip => !ip.StartsWith("169.254.", StringComparison.Ordinal))
                    .ToArray()))
            .ToArray();

        var ipAddresses = interfaces
            .SelectMany(nic => nic.IpAddresses)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();

        var qga = QueryService("QEMU-GA");
        var domainState = ReadDomainState();
        var networkDomainName = IPGlobalProperties.GetIPGlobalProperties().DomainName;
        var domainName = !string.IsNullOrWhiteSpace(domainState.DomainName)
            ? domainState.DomainName
            : networkDomainName;
        var domainJoined = domainState.DomainJoined
            ?? !string.IsNullOrWhiteSpace(networkDomainName);

        return new TelemetrySnapshot(
            Environment.MachineName,
            ReadBiosSerial(),
            ipAddresses.FirstOrDefault(),
            ipAddresses,
            interfaces,
            RuntimeInformation.OSDescription,
            Environment.OSVersion.Version.ToString(),
            Environment.OSVersion.Version.Build.ToString(),
            ReadBootTime(),
            Environment.TickCount64 / 1000,
            qga.ServiceName,
            qga.State,
            domainName,
            domainJoined,
            ReadEntraJoinState(),
            ReadTenantId());
    }

    private static (string? ServiceName, string? State) QueryService(string name)
    {
        if (!OperatingSystem.IsWindows())
        {
            return (null, null);
        }
        try
        {
            using var service = new ServiceController(name);
            return (service.ServiceName, service.Status.ToString());
        }
        catch
        {
            return (name, "NotFound");
        }
    }

    private static string? ReadBiosSerial()
    {
        if (!OperatingSystem.IsWindows())
        {
            return null;
        }
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(@"HARDWARE\DESCRIPTION\System\BIOS");
            return key?.GetValue("SystemSerialNumber")?.ToString();
        }
        catch
        {
            return null;
        }
    }

    private static string? ReadBootTime()
    {
        return DateTimeOffset.UtcNow
            .AddSeconds(-Environment.TickCount64 / 1000)
            .ToString("o");
    }

    private static (string? DomainName, bool? DomainJoined) ReadDomainState()
    {
        if (!OperatingSystem.IsWindows())
        {
            return (null, null);
        }
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT Domain, PartOfDomain FROM Win32_ComputerSystem");
            foreach (ManagementObject system in searcher.Get())
            {
                var domainName = system["Domain"]?.ToString();
                var partOfDomainValue = system["PartOfDomain"];
                bool? partOfDomain = partOfDomainValue switch
                {
                    bool value => value,
                    string text when bool.TryParse(text, out var parsed) => parsed,
                    _ => null
                };
                return (domainName, partOfDomain);
            }
        }
        catch
        {
            return (null, null);
        }
        return (null, null);
    }

    private static bool? ReadEntraJoinState()
    {
        if (!OperatingSystem.IsWindows())
        {
            return null;
        }
        try
        {
            using var enrollments = Registry.LocalMachine.OpenSubKey(
                @"SOFTWARE\Microsoft\Enrollments");
            return enrollments?.GetSubKeyNames().Length > 0;
        }
        catch
        {
            return null;
        }
    }

    private static string? ReadTenantId()
    {
        if (!OperatingSystem.IsWindows())
        {
            return null;
        }
        try
        {
            using var enrollments = Registry.LocalMachine.OpenSubKey(
                @"SOFTWARE\Microsoft\Enrollments");
            if (enrollments is null)
            {
                return null;
            }
            foreach (var subkeyName in enrollments.GetSubKeyNames())
            {
                using var subkey = enrollments.OpenSubKey(subkeyName);
                var tenantId = subkey?.GetValue("AADTenantID")?.ToString();
                if (!string.IsNullOrWhiteSpace(tenantId))
                {
                    return tenantId;
                }
            }
        }
        catch
        {
            return null;
        }
        return null;
    }
}

public sealed record TelemetrySnapshot(
    string ComputerName,
    string? SerialNumber,
    string? PrimaryIpv4,
    IReadOnlyList<string> IpAddresses,
    IReadOnlyList<NicSnapshot> Nics,
    string OsName,
    string OsVersion,
    string OsBuild,
    string? BootTime,
    long UptimeSeconds,
    string? QgaServiceName,
    string? QgaState,
    string? DomainName,
    bool? DomainJoined,
    bool? EntraJoined,
    string? TenantId);

public sealed record NicSnapshot(
    string Name,
    string Type,
    string MacAddress,
    IReadOnlyList<string> IpAddresses);
