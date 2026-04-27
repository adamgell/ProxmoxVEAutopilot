using System.Diagnostics;
using System.Text.Json;
using Autopilot.Launcher.Models;

namespace Autopilot.Launcher;

public sealed class StepRunner
{
    private readonly Orchestrator _orchestrator;
    private readonly string _vmUuid;
    private Func<Task>? _onHeartbeat;

    public StepRunner(Orchestrator orchestrator, string vmUuid)
    {
        _orchestrator = orchestrator;
        _vmUuid = vmUuid;
    }

    public void SetHeartbeat(Func<Task> onHeartbeat) => _onHeartbeat = onHeartbeat;

    public async Task<(string LogTail, Dictionary<string, object> Extra)> ExecuteStepAsync(
        Step step, Action<long, long> onDownloadProgress)
    {
        return step.Type switch
        {
            "log"           => ExecuteLog(step),
            "partition"     => await ExecutePwshStepAsync("Invoke-PartitionStep", $"-Layout '{step.Layout}'"),
            "apply-wim"     => await ExecuteApplyWimAsync(step, onDownloadProgress),
            "write-unattend"=> await ExecuteWriteContentAsync(step, onDownloadProgress),
            "stage-files"   => await ExecuteWriteContentAsync(step, onDownloadProgress),
            "set-registry"  => await ExecuteSetRegistryAsync(step),
            "bcdboot"       => await ExecutePwshStepAsync("Invoke-BcdbootStep", $"-Windows '{step.Windows}' -Esp '{step.Esp}'"),
            "inject-driver" => await ExecuteInjectDriverAsync(step, onDownloadProgress),
            "schedule-task" => await ExecuteScheduleTaskAsync(step),
            "reboot"        => (LogTail: "reboot deferred", Extra: new Dictionary<string, object> { ["deferred"] = true }),
            "shutdown"      => ExecuteShutdown(),
            _ => throw new InvalidOperationException($"Unknown step type: {step.Type}"),
        };
    }

    private static (string, Dictionary<string, object>) ExecuteLog(Step step)
    {
        Console.WriteLine($"  LOG: {step.Message}");
        return (step.Message ?? "", new());
    }

    private async Task<(string, Dictionary<string, object>)> ExecuteApplyWimAsync(
        Step step, Action<long, long> onProgress)
    {
        var target = step.Target ?? "W:";
        var index = step.Index ?? 1;
        var sha = step.Content!.Sha256;
        var tmpPath = $@"{target}\install-{sha}.wim";

        await _orchestrator.DownloadContentAsync(sha, tmpPath, step.Content.Size, onProgress);

        var result = await PwshInvoker.InvokeAsync(
            $"Expand-WindowsImage -ImagePath '{tmpPath}' -Index {index} -ApplyPath '{target}' -ErrorAction Stop",
            _onHeartbeat);
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"Expand-WindowsImage failed: {result.Stderr}");

        try { File.Delete(tmpPath); } catch { }

        return ($"applied wim {sha} → {target} (index {index})",
            new Dictionary<string, object> { ["target"] = target });
    }

    private async Task<(string, Dictionary<string, object>)> ExecuteWriteContentAsync(
        Step step, Action<long, long> onProgress)
    {
        var target = step.Target!;
        var sha = step.Content!.Sha256;

        var dir = Path.GetDirectoryName(target);
        if (dir != null && !Directory.Exists(dir))
            Directory.CreateDirectory(dir);

        await _orchestrator.DownloadContentAsync(sha, target, step.Content.Size, onProgress);

        return ($"wrote {sha} → {target}",
            new Dictionary<string, object> { ["target"] = target });
    }

    private async Task<(string, Dictionary<string, object>)> ExecuteSetRegistryAsync(Step step)
    {
        var keysJson = step.Keys?.GetRawText() ?? "[]";
        var escaped = keysJson.Replace("'", "''");
        var result = await PwshInvoker.InvokeAsync(
            $"Import-Module Autopilot.PESteps; Invoke-SetRegistryStep -Hive '{step.Hive}' -Target '{step.Target}' -Keys ('{escaped}' | ConvertFrom-Json)",
            _onHeartbeat);
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"SetRegistryStep failed: {result.Stderr}");

        return ($"set registry keys in {step.Hive} of {step.Target}",
            new Dictionary<string, object> { ["hive"] = step.Hive!, ["target"] = step.Target! });
    }

    private async Task<(string, Dictionary<string, object>)> ExecuteInjectDriverAsync(
        Step step, Action<long, long> onProgress)
    {
        var target = step.Target!;
        var sha = step.Content!.Sha256;
        var tmpZip = $@"X:\Windows\Temp\driver-{sha}.zip";
        var tmpDir = $@"X:\Windows\Temp\driver-{sha}";

        await _orchestrator.DownloadContentAsync(sha, tmpZip, step.Content.Size, onProgress);

        var result = await PwshInvoker.InvokeAsync(
            $"Expand-Archive -Path '{tmpZip}' -DestinationPath '{tmpDir}' -Force; " +
            $"Add-WindowsDriver -Path '{target}' -Driver '{tmpDir}' -Recurse -ForceUnsigned",
            _onHeartbeat);
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"InjectDriverStep failed: {result.Stderr}");

        try { File.Delete(tmpZip); Directory.Delete(tmpDir, true); } catch { }

        return ($"injected drivers → {target}",
            new Dictionary<string, object> { ["target"] = target });
    }

    private async Task<(string, Dictionary<string, object>)> ExecuteScheduleTaskAsync(Step step)
    {
        var result = await PwshInvoker.InvokeAsync(
            $"Import-Module Autopilot.PESteps; Invoke-ScheduleTaskStep -Target '{step.Target}' -Name '{step.Name}' -TaskXml '{step.TaskXml?.Replace("'", "''")}'",
            _onHeartbeat);
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"ScheduleTaskStep failed: {result.Stderr}");

        return ($"scheduled task {step.Name}",
            new Dictionary<string, object> { ["task"] = step.Name! });
    }

    private async Task<(string, Dictionary<string, object>)> ExecutePwshStepAsync(string cmdlet, string args)
    {
        var result = await PwshInvoker.InvokeAsync($"Import-Module Autopilot.PESteps; {cmdlet} {args}", _onHeartbeat);
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"{cmdlet} failed: {result.Stderr}");
        return (result.Stdout.Trim(), new());
    }

    private static (string, Dictionary<string, object>) ExecuteShutdown()
    {
        Process.Start(new ProcessStartInfo { FileName = "wpeutil", Arguments = "shutdown", UseShellExecute = false });
        return ("wpeutil shutdown", new());
    }
}
