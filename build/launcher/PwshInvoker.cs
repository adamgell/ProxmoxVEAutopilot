using System.Diagnostics;
using System.Text;

namespace Autopilot.Launcher;

public sealed record PwshResult(int ExitCode, string Stdout, string Stderr);

public static class PwshInvoker
{
    private const string PwshPath = @"X:\Program Files\PowerShell\7\pwsh.exe";
    private const string ModulePath = @"X:\autopilot\Modules";

    public static PwshResult Invoke(string command, int timeoutMs = 600_000)
    {
        var fullCommand = $"$env:PSModulePath = '{ModulePath};' + $env:PSModulePath; {command}";
        // Use -EncodedCommand to avoid escaping issues with quotes, $, backticks
        var encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(fullCommand));
        var psi = new ProcessStartInfo
        {
            FileName = PwshPath,
            Arguments = $"-NoProfile -NonInteractive -EncodedCommand {encoded}",
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };

        var stdout = new StringBuilder();
        var stderr = new StringBuilder();

        using var p = Process.Start(psi)
            ?? throw new InvalidOperationException("Failed to start pwsh.exe");

        p.OutputDataReceived += (_, e) => { if (e.Data != null) stdout.AppendLine(e.Data); };
        p.ErrorDataReceived += (_, e) => { if (e.Data != null) stderr.AppendLine(e.Data); };
        p.BeginOutputReadLine();
        p.BeginErrorReadLine();

        if (!p.WaitForExit(timeoutMs))
        {
            p.Kill(entireProcessTree: true);
            throw new TimeoutException($"pwsh command timed out after {timeoutMs}ms");
        }

        return new PwshResult(p.ExitCode, stdout.ToString(), stderr.ToString());
    }
}
