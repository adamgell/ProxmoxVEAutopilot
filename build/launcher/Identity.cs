using System.Management;

namespace Autopilot.Launcher;

public sealed record MachineIdentity(string Uuid, string Vendor, string Model);

public static class Identity
{
    public static MachineIdentity GetSmbios()
    {
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT UUID, Vendor, Name FROM Win32_ComputerSystemProduct");
            foreach (ManagementObject obj in searcher.Get())
            {
                return new MachineIdentity(
                    Uuid: obj["UUID"]?.ToString() ?? "UNKNOWN",
                    Vendor: obj["Vendor"]?.ToString() ?? "UNKNOWN",
                    Model: obj["Name"]?.ToString() ?? "UNKNOWN"
                );
            }
        }
        catch { }
        return new MachineIdentity("UNKNOWN", "UNKNOWN", "UNKNOWN");
    }
}
