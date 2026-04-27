using System.Diagnostics;
using System.Management;

namespace Autopilot.Launcher;

public static class Guard
{
    public static string? FindExistingWindows()
    {
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT DriveLetter, Label, DriveType FROM Win32_Volume WHERE Label = 'Windows' AND DriveType = 3");
            foreach (ManagementObject vol in searcher.Get())
            {
                var letter = vol["DriveLetter"]?.ToString();
                if (string.IsNullOrEmpty(letter))
                {
                    vol["DriveLetter"] = "W:";
                    try { vol.Put(); letter = "W:"; } catch { continue; }
                }
                var ntoskrnl = Path.Combine(letter, "Windows", "System32", "ntoskrnl.exe");
                if (File.Exists(ntoskrnl))
                    return letter;
            }
        }
        catch { }
        return null;
    }

    public static void Shutdown()
    {
        Process.Start(new ProcessStartInfo
        {
            FileName = "wpeutil",
            Arguments = "shutdown",
            UseShellExecute = false,
        })?.WaitForExit(30_000);
    }
}
