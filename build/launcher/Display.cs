using Autopilot.Launcher.Models;

namespace Autopilot.Launcher;

public enum StepState { Pending, Active, Done, Error }

public static class Display
{
    private const int Width = 64;

    public static string FormatDuration(TimeSpan ts)
    {
        if (ts.TotalMinutes >= 1)
            return $"{(int)ts.TotalMinutes}:{ts.Seconds:D2}";
        return $"{(int)ts.TotalSeconds}s";
    }

    public static string FormatBytes(long bytes)
    {
        if (bytes >= 1_073_741_824) return $"{bytes / 1_073_741_824.0:F1} GB";
        if (bytes >= 1_048_576) return $"{bytes / 1_048_576.0:F1} MB";
        if (bytes >= 1_024) return $"{bytes / 1_024.0:F1} KB";
        return $"{bytes} B";
    }

    public static string ProgressBar(int percent, int width)
    {
        var filled = (int)(percent / 100.0 * width);
        return new string('█', filled) + new string('░', width - filled);
    }

    public static string StepIcon(StepState state) => state switch
    {
        StepState.Pending => "[ ]",
        StepState.Active  => "[▸]",
        StepState.Done    => "[✓]",
        StepState.Error   => "[✗]",
        _ => "[ ]",
    };

    public static string StepTypeName(string type) => type switch
    {
        "partition"     => "Partition",
        "apply-wim"     => "Apply WIM",
        "write-unattend"=> "Write unattend",
        "set-registry"  => "Set registry",
        "bcdboot"       => "Configure boot",
        "stage-files"   => "Stage files",
        "inject-driver" => "Inject drivers",
        "schedule-task" => "Schedule task",
        "reboot"        => "Reboot",
        "shutdown"       => "Shutdown",
        "log"           => "Log",
        _ => type,
    };

    private static void Line(string text)
    {
        var padded = text.PadRight(Width);
        Console.WriteLine($"║  {padded}║");
    }

    private static void Separator()
    {
        Console.WriteLine($"╠{"".PadRight(Width + 2, '═')}╣");
    }

    private static void TopBorder()
    {
        Console.WriteLine($"╔{"".PadRight(Width + 2, '═')}╗");
    }

    private static void BottomBorder()
    {
        Console.WriteLine($"╚{"".PadRight(Width + 2, '═')}╝");
    }

    public static void RenderFull(
        string? uuid, string? vendor, string? model,
        string? ip, string? hostname, string? server,
        List<Step> steps, StepState[] states,
        TimeSpan[] elapsed, int? downloadPercent,
        long? downloadedBytes, string statusMessage)
    {
        Console.SetCursorPosition(0, 0);
        TopBorder();
        Line("Autopilot PE Bootstrap");
        Separator();
        Line($"UUID:    {uuid ?? "..."}");
        Line($"Vendor:  {vendor ?? "..."} │ Model: {model ?? "..."}");
        Line($"IP:      {ip ?? "..."} │ Host: {hostname ?? "..."}");
        Line($"Server:  {server ?? "..."}");
        Separator();

        for (var i = 0; i < steps.Count; i++)
        {
            var icon = StepIcon(states[i]);
            var name = StepTypeName(steps[i].Type);
            var dur = states[i] is StepState.Done or StepState.Active
                ? FormatDuration(elapsed[i]).PadLeft(6)
                : "      ";

            var detail = "";
            if (states[i] == StepState.Active && downloadPercent.HasValue)
            {
                var bar = ProgressBar(downloadPercent.Value, 18);
                var pct = $"{downloadPercent.Value}%".PadLeft(4);
                var bytes = downloadedBytes.HasValue ? FormatBytes(downloadedBytes.Value) : "";
                detail = $" {bar} {pct} {bytes}";
            }

            Line($"{icon} {name,-18}{dur}{detail}");
        }

        Separator();
        Line(statusMessage);
        BottomBorder();
    }

    private static readonly string[] BootPhases =
    [
        "Guard check",
        "Initialize hardware",
        "Load config",
        "Wait for network",
        "Identify machine",
        "Fetch manifest",
    ];

    public static void RenderBoot(
        int activePhase, string? uuid, string? vendor, string? model,
        string? ip, string? hostname, string? server, string statusMessage)
    {
        try { Console.SetCursorPosition(0, 0); } catch { }
        TopBorder();
        Line("Autopilot PE Bootstrap");
        Separator();
        Line($"UUID:    {uuid ?? "..."}");
        Line($"Vendor:  {vendor ?? "..."} │ Model: {model ?? "..."}");
        Line($"IP:      {ip ?? "..."} │ Host: {hostname ?? "..."}");
        Line($"Server:  {server ?? "..."}");
        Separator();

        for (var i = 0; i < BootPhases.Length; i++)
        {
            var icon = i < activePhase ? StepIcon(StepState.Done)
                     : i == activePhase ? StepIcon(StepState.Active)
                     : StepIcon(StepState.Pending);
            Line($"{icon} {BootPhases[i],-18}");
        }

        Separator();
        Line(statusMessage);
        BottomBorder();
    }

    public static void ShowGuardShutdown(string driveLetter)
    {
        Console.Clear();
        Console.WriteLine();
        Console.WriteLine($"  Windows already installed on {driveLetter}");
        Console.WriteLine("  Shutting down PE...");
        Console.WriteLine();
    }

    public static void ShowError(string message)
    {
        Console.ForegroundColor = ConsoleColor.Red;
        Console.Error.WriteLine($"  ERROR: {message}");
        Console.ResetColor();
    }

    public static void ShowPhase(string message)
    {
        Console.WriteLine($"  {message}");
    }
}
