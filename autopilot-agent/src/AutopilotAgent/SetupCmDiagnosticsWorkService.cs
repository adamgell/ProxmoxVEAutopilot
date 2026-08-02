using System.Diagnostics;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace AutopilotAgent;

public sealed class SetupCmDiagnosticsWorkService(AgentApiClient apiClient, AgentFileLog log)
{
    private const int OutputLimitBytes = 256 * 1024;
    private static readonly Regex SiteCodePattern = new("^[A-Z0-9]{3}$", RegexOptions.CultureInvariant);
    private static readonly Regex ComputerNamePattern = new("^[A-Za-z0-9-]{1,63}$", RegexOptions.CultureInvariant);

    public const string DiagnosticScriptResourceName = "AutopilotAgent.SetupCmSourceDiagnostics.ps1";
    public static readonly string[] SupportedKinds = ["setup_cm_diagnostics", "setup_cm_source_access"];

    public async Task ProcessAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var request = ValidateRequest(work.Request);
        var scriptPath = WriteDiagnosticScript(work.Id);
        var isSourceAccessRemediation = string.Equals(
            work.Kind,
            "setup_cm_source_access",
            StringComparison.Ordinal);
        var output = await RunPowerShellAsync(
            scriptPath,
            request,
            isSourceAccessRemediation,
            cancellationToken);
        if (output.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"Setup-CM source diagnostics failed with exit code {output.ExitCode}.");
        }

        var result = ParseDiagnosticResult(output.Stdout, isSourceAccessRemediation);
        await apiClient.CompleteWorkAsync(config, work.Id, result, cancellationToken);
        log.Info($"Setup-CM source diagnostics completed ({work.Id}).");
    }

    public static SetupCmDiagnosticsRequest ValidateRequest(
        IReadOnlyDictionary<string, JsonElement> values)
    {
        RequireOnlyFields(values, "site_code", "target_computer_name");
        var siteCode = RequiredString(values, "site_code");
        var targetComputerName = RequiredString(values, "target_computer_name");
        if (!SiteCodePattern.IsMatch(siteCode))
        {
            throw new InvalidOperationException("site_code must be exactly three upper-case letters or digits.");
        }
        if (!ComputerNamePattern.IsMatch(targetComputerName))
        {
            throw new InvalidOperationException("target_computer_name must be a NetBIOS computer name.");
        }
        return new SetupCmDiagnosticsRequest(siteCode, targetComputerName);
    }

    private static string WriteDiagnosticScript(string workId)
    {
        var root = Path.Combine(AgentConfig.ProgramDataRoot, "setup-cm-diagnostics");
        Directory.CreateDirectory(root);
        var safeWorkId = string.Concat(workId.Where(ch => char.IsLetterOrDigit(ch) || ch is '-' or '_'));
        var path = Path.Combine(root, $"{safeWorkId}.ps1");
        using var resource = Assembly.GetExecutingAssembly()
            .GetManifestResourceStream(DiagnosticScriptResourceName)
            ?? throw new InvalidOperationException("Setup-CM diagnostic script resource is missing.");
        using var reader = new StreamReader(resource, Encoding.UTF8, detectEncodingFromByteOrderMarks: true);
        File.WriteAllText(path, reader.ReadToEnd(), new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        return path;
    }

    private static async Task<ProcessOutput> RunPowerShellAsync(
        string scriptPath,
        SetupCmDiagnosticsRequest request,
        bool remediateSourceAccess,
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
        startInfo.ArgumentList.Add("-SiteCode");
        startInfo.ArgumentList.Add(request.SiteCode);
        startInfo.ArgumentList.Add("-TargetComputerName");
        startInfo.ArgumentList.Add(request.TargetComputerName);
        if (remediateSourceAccess)
        {
            startInfo.ArgumentList.Add("-RemediateSourceAccess");
        }
        using var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Failed to start pwsh.exe for Setup-CM diagnostics.");
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromMinutes(5));
        var stdoutTask = process.StandardOutput.ReadToEndAsync(timeout.Token);
        var stderrTask = process.StandardError.ReadToEndAsync(timeout.Token);
        await process.WaitForExitAsync(timeout.Token);
        return new ProcessOutput(await stdoutTask, await stderrTask, process.ExitCode);
    }

    private static Dictionary<string, object?> ParseDiagnosticResult(
        string stdout,
        bool remediateSourceAccess)
    {
        if (Encoding.UTF8.GetByteCount(stdout) > OutputLimitBytes)
        {
            throw new InvalidOperationException("Setup-CM diagnostic output exceeded the 256 KiB limit.");
        }
        using var document = JsonDocument.Parse(stdout);
        if (document.RootElement.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException("Setup-CM diagnostic output must be a JSON object.");
        }
        var required = new[]
        {
            "site_code", "target_computer_name", "target_machine_sid", "share_access",
            "client_folder_access", "cifs_spns", "errors",
        };
        foreach (var name in required)
        {
            if (!document.RootElement.TryGetProperty(name, out _))
            {
                throw new InvalidOperationException($"Setup-CM diagnostic result is missing {name}.");
            }
        }
        if (remediateSourceAccess
            && !document.RootElement.TryGetProperty("source_access_remediation", out _))
        {
            throw new InvalidOperationException(
                "Setup-CM source access remediation result is missing source_access_remediation.");
        }
        return document.RootElement.EnumerateObject().ToDictionary(
            property => property.Name,
            property => (object?)property.Value.Clone(),
            StringComparer.Ordinal);
    }

    private static string RequiredString(IReadOnlyDictionary<string, JsonElement> values, string name)
    {
        if (!values.TryGetValue(name, out var value)
            || value.ValueKind != JsonValueKind.String
            || string.IsNullOrWhiteSpace(value.GetString()))
        {
            throw new InvalidOperationException($"{name} is required.");
        }
        return value.GetString()!;
    }

    private static void RequireOnlyFields(IReadOnlyDictionary<string, JsonElement> values, params string[] allowed)
    {
        var unexpected = values.Keys.FirstOrDefault(key => !allowed.Contains(key, StringComparer.Ordinal));
        if (unexpected is not null)
        {
            throw new InvalidOperationException($"Unexpected Setup-CM diagnostic request field: {unexpected}");
        }
    }

    private sealed record ProcessOutput(string Stdout, string Stderr, int ExitCode);
}

public sealed record SetupCmDiagnosticsRequest(string SiteCode, string TargetComputerName);
