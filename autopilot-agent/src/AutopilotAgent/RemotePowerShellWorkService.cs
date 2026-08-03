using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.Json;

namespace AutopilotAgent;

public sealed class RemotePowerShellWorkService(AgentApiClient apiClient, AgentFileLog log)
{
    private const int OutputLimitBytes = 64 * 1024;
    private const int FailureOutputLimitChars = 4096;
    private static readonly string[] RequiredResultFields =
    [
        "command_id",
        "computer_name",
        "os_version",
        "powershell_version",
        "current_user",
        "ipv4_addresses",
    ];

    public const string SupportedKind = "remote_powershell";
    public const string EndpointFactsCommandId = "endpoint_facts";
    public const string EndpointFactsScriptResourceName =
        "AutopilotAgent.RemotePowerShellEndpointFacts.ps1";

    public async Task ProcessAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var request = ValidateRequest(work.Request);
        var scriptPath = WriteRunbook(work.Id, EndpointFactsScriptResourceName);
        var output = await RunPowerShellAsync(scriptPath, cancellationToken);
        if (output.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"Remote PowerShell {request.CommandId} failed with exit code {output.ExitCode}. "
                + $"stderr={TruncateFailureOutput(output.Stderr)} "
                + $"stdout={TruncateFailureOutput(output.Stdout)}");
        }

        var result = ParseEndpointFactsResult(output.Stdout, request);
        await apiClient.CompleteWorkAsync(config, work.Id, result, cancellationToken);
        log.Info($"Remote PowerShell {request.CommandId} completed ({work.Id}).");
    }

    public static RemotePowerShellRequest ValidateRequest(
        IReadOnlyDictionary<string, JsonElement> values)
    {
        if (values.Count != 1 || !values.ContainsKey("command_id"))
        {
            throw new InvalidOperationException(
                "remote PowerShell accepts exactly one field: command_id.");
        }
        if (values["command_id"].ValueKind != JsonValueKind.String)
        {
            throw new InvalidOperationException("command_id must be a string.");
        }
        var commandId = values["command_id"].GetString()?.Trim() ?? string.Empty;
        if (!string.Equals(commandId, EndpointFactsCommandId, StringComparison.Ordinal))
        {
            throw new InvalidOperationException("remote PowerShell command_id is not approved.");
        }
        return new RemotePowerShellRequest(EndpointFactsCommandId);
    }

    private static string WriteRunbook(string workId, string resourceName)
    {
        var root = Path.Combine(AgentConfig.ProgramDataRoot, "remote-powershell");
        Directory.CreateDirectory(root);
        var safeWorkId = string.Concat(workId.Where(ch => char.IsLetterOrDigit(ch) || ch is '-' or '_'));
        if (string.IsNullOrWhiteSpace(safeWorkId))
        {
            throw new InvalidOperationException("work item ID is invalid.");
        }
        var path = Path.Combine(root, $"{safeWorkId}.ps1");
        using var resource = Assembly.GetExecutingAssembly()
            .GetManifestResourceStream(resourceName)
            ?? throw new InvalidOperationException("remote PowerShell runbook resource is missing.");
        using var reader = new StreamReader(resource, Encoding.UTF8, detectEncodingFromByteOrderMarks: true);
        File.WriteAllText(path, reader.ReadToEnd(), new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        return path;
    }

    private static async Task<ProcessOutput> RunPowerShellAsync(
        string scriptPath,
        CancellationToken cancellationToken)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = "pwsh.exe",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        startInfo.ArgumentList.Add("-NoProfile");
        startInfo.ArgumentList.Add("-NonInteractive");
        startInfo.ArgumentList.Add("-ExecutionPolicy");
        startInfo.ArgumentList.Add("Bypass");
        startInfo.ArgumentList.Add("-File");
        startInfo.ArgumentList.Add(scriptPath);
        using var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Failed to start pwsh.exe for remote PowerShell.");
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromSeconds(60));
        var stdoutTask = process.StandardOutput.ReadToEndAsync(timeout.Token);
        var stderrTask = process.StandardError.ReadToEndAsync(timeout.Token);
        try
        {
            await process.WaitForExitAsync(timeout.Token);
        }
        catch (OperationCanceledException)
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync(CancellationToken.None);
            }
            throw;
        }

        var stdout = await stdoutTask;
        var stderr = await stderrTask;
        if (Encoding.UTF8.GetByteCount(stdout) + Encoding.UTF8.GetByteCount(stderr) > OutputLimitBytes)
        {
            throw new InvalidOperationException("remote PowerShell output exceeded the 64 KiB limit.");
        }
        return new ProcessOutput(stdout, stderr, process.ExitCode);
    }

    private static Dictionary<string, object?> ParseEndpointFactsResult(
        string stdout,
        RemotePowerShellRequest request)
    {
        using var document = JsonDocument.Parse(stdout);
        if (document.RootElement.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException("remote PowerShell result must be a JSON object.");
        }
        foreach (var field in RequiredResultFields)
        {
            if (!document.RootElement.TryGetProperty(field, out _))
            {
                throw new InvalidOperationException($"remote PowerShell result is missing {field}.");
            }
        }
        RequireNonEmptyString(document.RootElement, "command_id", request.CommandId);
        foreach (var field in RequiredResultFields.Where(field => field != "command_id" && field != "ipv4_addresses"))
        {
            RequireNonEmptyString(document.RootElement, field);
        }
        var addresses = document.RootElement.GetProperty("ipv4_addresses");
        if (addresses.ValueKind != JsonValueKind.Array)
        {
            throw new InvalidOperationException("remote PowerShell ipv4_addresses must be an array.");
        }
        foreach (var address in addresses.EnumerateArray())
        {
            if (address.ValueKind != JsonValueKind.String
                || !IPAddress.TryParse(address.GetString(), out var parsed)
                || parsed.AddressFamily != AddressFamily.InterNetwork)
            {
                throw new InvalidOperationException("remote PowerShell returned an invalid IPv4 address.");
            }
        }
        return document.RootElement.EnumerateObject().ToDictionary(
            property => property.Name,
            property => (object?)property.Value.Clone(),
            StringComparer.Ordinal);
    }

    private static void RequireNonEmptyString(JsonElement source, string field, string? expected = null)
    {
        if (source.GetProperty(field).ValueKind != JsonValueKind.String)
        {
            throw new InvalidOperationException($"remote PowerShell {field} must be a string.");
        }
        var value = source.GetProperty(field).GetString()?.Trim() ?? string.Empty;
        if (string.IsNullOrWhiteSpace(value)
            || (expected is not null && !string.Equals(value, expected, StringComparison.Ordinal)))
        {
            throw new InvalidOperationException($"remote PowerShell {field} is invalid.");
        }
    }

    private static string TruncateFailureOutput(string value) =>
        value.Length <= FailureOutputLimitChars
            ? value
            : value[..FailureOutputLimitChars] + "…";

    private sealed record ProcessOutput(string Stdout, string Stderr, int ExitCode);
}

public sealed record RemotePowerShellRequest(string CommandId);
