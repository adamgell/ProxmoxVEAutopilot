using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;

namespace AutopilotAgent;

public sealed class AgentUpdateService(
    AgentApiClient apiClient,
    HttpClient httpClient,
    AgentFileLog log)
{
    public async Task CheckAndApplyOnceAsync(
        AgentConfig config,
        CancellationToken cancellationToken)
    {
        var runtimeIdentifier = OperatingSystem.IsWindows()
            && RuntimeInformation.ProcessArchitecture == Architecture.Arm64
            ? "win-arm64"
            : "win-x64";
        var update = await apiClient.CheckForUpdateAsync(
            config,
            runtimeIdentifier,
            cancellationToken);
        if (!string.Equals(update.Status, "upgrade_available", StringComparison.OrdinalIgnoreCase))
        {
            return;
        }
        if (string.IsNullOrWhiteSpace(update.DownloadUrl) || string.IsNullOrWhiteSpace(update.Sha256))
        {
            log.Warning("Agent update was advertised without a download URL or SHA-256.");
            return;
        }

        var uri = update.DownloadUrl.StartsWith("http", StringComparison.OrdinalIgnoreCase)
            ? update.DownloadUrl
            : $"{config.ServerUrl.TrimEnd('/')}{update.DownloadUrl}";
        var target = Path.Combine(Path.GetTempPath(), "AutopilotAgent-update.msi");
        await using (var stream = await httpClient.GetStreamAsync(uri, cancellationToken))
        await using (var file = File.Create(target))
        {
            await stream.CopyToAsync(file, cancellationToken);
        }

        var actual = await Sha256Async(target, cancellationToken);
        if (!string.Equals(actual, update.Sha256, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("Downloaded AutopilotAgent MSI failed SHA-256 validation.");
        }

        var process = Process.Start(new ProcessStartInfo
        {
            FileName = "msiexec.exe",
            Arguments = $"/i \"{target}\" /qn /norestart",
            UseShellExecute = false,
        }) ?? throw new InvalidOperationException("msiexec.exe did not start.");
        await process.WaitForExitAsync(cancellationToken);
        if (process.ExitCode != 0 && process.ExitCode != 3010)
        {
            throw new InvalidOperationException($"AutopilotAgent MSI update failed with exit code {process.ExitCode}.");
        }
        log.Info($"AutopilotAgent MSI update completed with exit code {process.ExitCode}.");
    }

    private static async Task<string> Sha256Async(
        string path,
        CancellationToken cancellationToken)
    {
        await using var stream = File.OpenRead(path);
        var hash = await SHA256.HashDataAsync(stream, cancellationToken);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }
}
