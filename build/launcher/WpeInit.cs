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

    public static void RunWpeInit(Action<string> onStatus)
    {
        onStatus("Running wpeinit...");
        var p = Process.Start(new ProcessStartInfo
        {
            FileName = "wpeinit",
            UseShellExecute = false,
            RedirectStandardOutput = true,
        });
        p?.WaitForExit();
        onStatus("wpeinit complete.");
    }

    public static string WaitForNetwork(int timeoutSeconds, Action<string> onStatus)
    {
        var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        while (DateTime.UtcNow < deadline)
        {
            onStatus("Initializing network...");
            RunProcess("wpeutil", "InitializeNetwork");
            var output = CaptureProcess("ipconfig");
            var ip = ParseFirstNonApipaIp(output);
            if (ip != null)
                return ip;
            Thread.Sleep(3000);
        }
        throw new TimeoutException($"No non-APIPA IPv4 address after {timeoutSeconds}s");
    }

    private static void RunProcess(string fileName, string args)
    {
        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = fileName,
                Arguments = args,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            })?.WaitForExit(15_000);
        }
        catch { }
    }

    private static string CaptureProcess(string fileName)
    {
        try
        {
            var p = Process.Start(new ProcessStartInfo
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
