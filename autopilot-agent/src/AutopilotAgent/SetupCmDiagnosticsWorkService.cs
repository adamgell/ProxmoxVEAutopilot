using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
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
    public const string ContentLocationDiagnosticScriptResourceName =
        "AutopilotAgent.SetupCmContentLocationDiagnostics.ps1";
    public static readonly string[] SupportedKinds =
    [
        "setup_cm_diagnostics",
        "setup_cm_source_access",
        "setup_cm_content_location_diagnostics",
    ];

    public async Task ProcessAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        if (string.Equals(
                work.Kind,
                "setup_cm_content_location_diagnostics",
                StringComparison.Ordinal))
        {
            await ProcessContentLocationAsync(config, work, cancellationToken);
            return;
        }

        var request = ValidateRequest(work.Request);
        var scriptPath = WriteDiagnosticScript(work.Id, DiagnosticScriptResourceName);
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

    private async Task ProcessContentLocationAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var request = ValidateContentLocationRequest(work.Request);
        var scriptPath = WriteDiagnosticScript(work.Id, ContentLocationDiagnosticScriptResourceName);
        var output = await RunPowerShellAsync(scriptPath, request, cancellationToken);
        if (output.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"Setup-CM content location diagnostics failed with exit code {output.ExitCode}.");
        }

        var result = ParseContentLocationDiagnosticResult(output.Stdout);
        await apiClient.CompleteWorkAsync(config, work.Id, result, cancellationToken);
        log.Info($"Setup-CM content location diagnostics completed ({work.Id}).");
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

    public static SetupCmContentLocationDiagnosticsRequest ValidateContentLocationRequest(
        IReadOnlyDictionary<string, JsonElement> values)
    {
        RequireOnlyFields(values, "site_code", "target_computer_name", "client_ipv4");
        var siteCode = RequiredString(values, "site_code");
        var targetComputerName = RequiredString(values, "target_computer_name");
        var clientIpv4 = RequiredString(values, "client_ipv4");
        if (!string.Equals(siteCode, "LAB", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("site_code must be LAB for content location diagnostics.");
        }
        if (!ComputerNamePattern.IsMatch(targetComputerName))
        {
            throw new InvalidOperationException("target_computer_name must be a NetBIOS computer name.");
        }
        if (!IPAddress.TryParse(clientIpv4, out var parsedAddress)
            || parsedAddress.AddressFamily != AddressFamily.InterNetwork)
        {
            throw new InvalidOperationException("client_ipv4 must be an IPv4 address.");
        }
        return new SetupCmContentLocationDiagnosticsRequest(
            siteCode,
            targetComputerName,
            parsedAddress.ToString());
    }

    private static string WriteDiagnosticScript(string workId, string resourceName)
    {
        var root = Path.Combine(AgentConfig.ProgramDataRoot, "setup-cm-diagnostics");
        Directory.CreateDirectory(root);
        var safeWorkId = string.Concat(workId.Where(ch => char.IsLetterOrDigit(ch) || ch is '-' or '_'));
        var path = Path.Combine(root, $"{safeWorkId}.ps1");
        using var resource = Assembly.GetExecutingAssembly()
            .GetManifestResourceStream(resourceName)
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

    private static async Task<ProcessOutput> RunPowerShellAsync(
        string scriptPath,
        SetupCmContentLocationDiagnosticsRequest request,
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
        startInfo.ArgumentList.Add("-ClientIpv4");
        startInfo.ArgumentList.Add(request.ClientIpv4);
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

    private static Dictionary<string, object?> ParseContentLocationDiagnosticResult(string stdout)
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
            "site_code", "target_computer_name", "client_ipv4", "client_subnet",
            "matching_boundaries", "boundary_groups", "distribution_points",
            "client_package", "errors",
        };
        foreach (var name in required)
        {
            if (!document.RootElement.TryGetProperty(name, out _))
            {
                throw new InvalidOperationException($"Setup-CM diagnostic result is missing {name}.");
            }
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

public sealed record SetupCmContentLocationDiagnosticsRequest(
    string SiteCode,
    string TargetComputerName,
    string ClientIpv4);
