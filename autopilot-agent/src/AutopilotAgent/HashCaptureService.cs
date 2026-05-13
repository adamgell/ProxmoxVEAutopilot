using System.Diagnostics;
using System.Text.Json;
using Microsoft.VisualBasic.FileIO;

namespace AutopilotAgent;

public sealed class HashCaptureService(
    AgentApiClient apiClient,
    AgentFileLog log)
{
    public async Task CaptureAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var captured = await CaptureHashAsync(
            config,
            work.Id,
            work.Request,
            cancellationToken);
        var groupTag = ReadString(work.Request, "group_tag");
        var upload = await apiClient.UploadHashAsync(
            config,
            work.Id,
            captured.SerialNumber,
            captured.ProductId,
            captured.HardwareHash,
            cancellationToken);
        if (!upload.Ok)
        {
            throw new InvalidOperationException("Server rejected Autopilot hash upload.");
        }
        log.Info($"Autopilot hardware hash uploaded for work item {work.Id}: {upload.Filename}");
        if (!string.IsNullOrWhiteSpace(groupTag))
        {
            log.Info($"Autopilot hardware hash used group tag {groupTag} for work item {work.Id}.");
        }
    }

    public async Task CaptureOsdV2Async(
        AgentConfig config,
        OsdV2Action action,
        string bearerToken,
        CancellationToken cancellationToken)
    {
        var captured = await CaptureHashAsync(
            config,
            action.StepId,
            action.Params,
            cancellationToken);
        var groupTag = ReadString(action.Params, "group_tag");
        await apiClient.UploadOsdV2HashAsync(
            config,
            bearerToken,
            captured.SerialNumber,
            captured.ProductId,
            captured.HardwareHash,
            groupTag,
            cancellationToken);
        log.Info($"Autopilot hardware hash uploaded for OSD v2 step {action.StepId}.");
    }

    private async Task<CapturedHash> CaptureHashAsync(
        AgentConfig config,
        string workId,
        IReadOnlyDictionary<string, JsonElement> request,
        CancellationToken cancellationToken)
    {
        var scriptDir = Path.Combine(AgentConfig.ProgramDataRoot, "tools");
        var hashDir = Path.Combine(AgentConfig.ProgramDataRoot, "hashes");
        Directory.CreateDirectory(scriptDir);
        Directory.CreateDirectory(hashDir);

        var scriptPath = Path.Combine(scriptDir, "Get-WindowsAutopilotInfo.ps1");
        var script = await apiClient.DownloadHashScriptAsync(config, cancellationToken);
        await File.WriteAllTextAsync(scriptPath, script, cancellationToken);

        var safeWorkId = string.Concat(
            workId.Select(ch => char.IsLetterOrDigit(ch) || ch is '-' or '_' ? ch : '_'));
        var csvPath = Path.Combine(hashDir, $"{safeWorkId}_hwid.csv");
        if (File.Exists(csvPath))
        {
            File.Delete(csvPath);
        }

        var groupTag = ReadString(request, "group_tag");
        log.Info($"Capturing Autopilot hardware hash for work item {workId}.");
        await RunAutopilotInfoAsync(scriptPath, csvPath, groupTag, cancellationToken);

        return ReadHashCsv(csvPath);
    }

    private static string ReadString(
        IReadOnlyDictionary<string, JsonElement> values,
        string key)
    {
        if (!values.TryGetValue(key, out var value))
        {
            return string.Empty;
        }
        return value.ValueKind switch
        {
            JsonValueKind.String => value.GetString() ?? string.Empty,
            JsonValueKind.Number => value.ToString(),
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            _ => string.Empty,
        };
    }

    private static async Task RunAutopilotInfoAsync(
        string scriptPath,
        string csvPath,
        string groupTag,
        CancellationToken cancellationToken)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        startInfo.ArgumentList.Add("-ExecutionPolicy");
        startInfo.ArgumentList.Add("Bypass");
        startInfo.ArgumentList.Add("-NoProfile");
        startInfo.ArgumentList.Add("-File");
        startInfo.ArgumentList.Add(scriptPath);
        startInfo.ArgumentList.Add("-OutputFile");
        startInfo.ArgumentList.Add(csvPath);
        if (!string.IsNullOrWhiteSpace(groupTag))
        {
            startInfo.ArgumentList.Add("-GroupTag");
            startInfo.ArgumentList.Add(groupTag);
        }

        using var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Failed to start powershell.exe.");
        var stdoutTask = process.StandardOutput.ReadToEndAsync(cancellationToken);
        var stderrTask = process.StandardError.ReadToEndAsync(cancellationToken);
        await process.WaitForExitAsync(cancellationToken);
        var stdout = await stdoutTask;
        var stderr = await stderrTask;
        if (process.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"Get-WindowsAutopilotInfo.ps1 failed with exit {process.ExitCode}: {stderr} {stdout}".Trim());
        }
        if (!File.Exists(csvPath))
        {
            throw new FileNotFoundException(
                "Get-WindowsAutopilotInfo.ps1 did not create an output CSV.",
                csvPath);
        }
    }

    private static CapturedHash ReadHashCsv(string csvPath)
    {
        using var parser = new TextFieldParser(csvPath)
        {
            HasFieldsEnclosedInQuotes = true,
            TrimWhiteSpace = false,
        };
        parser.SetDelimiters(",");
        var headers = parser.ReadFields()
            ?? throw new InvalidOperationException("Autopilot hash CSV is empty.");
        var values = parser.ReadFields()
            ?? throw new InvalidOperationException("Autopilot hash CSV has no data row.");

        string Field(string name)
        {
            var index = Array.FindIndex(
                headers,
                header => string.Equals(header, name, StringComparison.OrdinalIgnoreCase));
            if (index < 0 || index >= values.Length)
            {
                return string.Empty;
            }
            return values[index] ?? string.Empty;
        }

        var serial = Field("Device Serial Number");
        var productId = Field("Windows Product ID");
        var hardwareHash = Field("Hardware Hash");
        if (string.IsNullOrWhiteSpace(hardwareHash))
        {
            throw new InvalidOperationException("Autopilot hash CSV is missing Hardware Hash.");
        }
        if (string.IsNullOrWhiteSpace(serial))
        {
            serial = Environment.MachineName;
        }
        return new CapturedHash(serial, productId, hardwareHash);
    }

    private sealed record CapturedHash(
        string SerialNumber,
        string ProductId,
        string HardwareHash);
}
