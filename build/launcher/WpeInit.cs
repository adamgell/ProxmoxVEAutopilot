using System.Diagnostics;
using System.Text.RegularExpressions;

namespace Autopilot.Launcher;

public static partial class WpeInit
{
    [GeneratedRegex(@"IPv4 Address.*?:\s*([\d\.]+)")]
    private static partial Regex Ipv4Regex();

    public static string? ParseFirstNonApipaIp(string ipconfigOutput)
    {
        foreach (Match m in Ipv4Regex().Matches(ipconfigOutput))
        {
            var ip = m.Groups[1].Value;
            if (ip.StartsWith("169.254.") || ip == "127.0.0.1")
                continue;
            return ip;
        }
        return null;
    }

    public static async Task RunWpeInitAsync(Action<string> onStatus, Func<Task>? onHeartbeat = null)
    {
        onStatus("Running wpeinit...");
        try
        {
            using var p = Process.Start(new ProcessStartInfo
            {
                FileName = "wpeinit",
                UseShellExecute = false,
            });
            if (p != null)
            {
                var elapsed = 0;
                while (!p.WaitForExit(1000))
                {
                    elapsed++;
                    onStatus($"Running wpeinit... ({elapsed}s)");
                    if (onHeartbeat != null) await onHeartbeat();
                }
            }
        }
        catch (Exception ex)
        {
            onStatus($"wpeinit skipped: {ex.Message}");
        }
        onStatus("wpeinit complete.");
        if (onHeartbeat != null) await onHeartbeat();
    }

    public static async Task<string> WaitForNetworkAsync(int timeoutSeconds, Action<string> onStatus, Func<Task>? onHeartbeat = null)
    {
        var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        var attempt = 0;
        while (DateTime.UtcNow < deadline)
        {
            attempt++;
            onStatus($"Initializing network... (attempt {attempt})");
            if (onHeartbeat != null) await onHeartbeat();
            RunProcess("wpeutil", "InitializeNetwork");
            var output = CaptureProcess("ipconfig");
            var ip = ParseFirstNonApipaIp(output);
            if (ip != null)
                return ip;
            await Task.Delay(3000);
        }
        throw new TimeoutException($"No non-APIPA IPv4 address after {timeoutSeconds}s");
    }

    private static void RunProcess(string fileName, string args)
    {
        try
        {
            using var p = Process.Start(new ProcessStartInfo
            {
                FileName = fileName,
                Arguments = args,
                UseShellExecute = false,
            });
            p?.WaitForExit(15_000);
        }
        catch { }
    }

    private static string CaptureProcess(string fileName)
    {
        try
        {
            using var p = Process.Start(new ProcessStartInfo
            {
                FileName = fileName,
                UseShellExecute = false,
                RedirectStandardOutput = true,
            });
            if (p == null) return "";
            var output = p.StandardOutput.ReadToEnd();
            p.WaitForExit(10_000);
            return output;
        }
        catch { return ""; }
    }
}
