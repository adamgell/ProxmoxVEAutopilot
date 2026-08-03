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
    private const int FailureOutputLimitChars = 4096;
    private static readonly Regex SiteCodePattern = new("^[A-Z0-9]{3}$", RegexOptions.CultureInvariant);
    private static readonly Regex ComputerNamePattern = new("^[A-Za-z0-9-]{1,63}$", RegexOptions.CultureInvariant);

    public const string DiagnosticScriptResourceName = "AutopilotAgent.SetupCmSourceDiagnostics.ps1";
    public const string ContentLocationDiagnosticScriptResourceName =
        "AutopilotAgent.SetupCmContentLocationDiagnostics.ps1";
    public const string ContentLocationRemediationScriptResourceName =
        "AutopilotAgent.SetupCmContentLocationRemediation.ps1";
    public const string ClientNetworkRepairScriptResourceName =
        "AutopilotAgent.SetupCmClientNetworkRepair.ps1";
    public static readonly string[] SupportedKinds =
    [
        "setup_cm_diagnostics",
        "setup_cm_source_access",
        "setup_cm_content_location_diagnostics",
        "setup_cm_content_location_remediation",
        "setup_cm_client_network_repair",
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
        if (string.Equals(
                work.Kind,
                "setup_cm_content_location_remediation",
                StringComparison.Ordinal))
        {
            await ProcessContentLocationRemediationAsync(config, work, cancellationToken);
            return;
        }
        if (string.Equals(
                work.Kind,
                "setup_cm_client_network_repair",
                StringComparison.Ordinal))
        {
            await ProcessClientNetworkRepairAsync(config, work, cancellationToken);
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

    private async Task ProcessContentLocationRemediationAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var request = ValidateContentLocationRemediationRequest(work.Request);
        var scriptPath = WriteDiagnosticScript(work.Id, ContentLocationRemediationScriptResourceName);
        var output = await RunPowerShellAsync(scriptPath, request, cancellationToken);
        if (output.ExitCode != 0)
        {
            throw new InvalidOperationException(
                FormatContentLocationRemediationFailure(
                    output.ExitCode,
                    output.Stdout,
                    output.Stderr));
        }

        var result = ParseContentLocationRemediationResult(output.Stdout, request);
        await apiClient.CompleteWorkAsync(config, work.Id, result, cancellationToken);
        log.Info($"Setup-CM content location remediation completed ({work.Id}).");
    }

    private async Task ProcessClientNetworkRepairAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        RequireOnlyFields(work.Request);
        var scriptPath = WriteDiagnosticScript(work.Id, ClientNetworkRepairScriptResourceName);
        var output = await RunPowerShellAsync(scriptPath, cancellationToken);
        if (output.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"Setup-CM client network repair failed with exit code {output.ExitCode}. "
                + $"stderr={TruncateFailureOutput(output.Stderr)} "
                + $"stdout={TruncateFailureOutput(output.Stdout)}");
        }

        var result = ParseClientNetworkRepairResult(output.Stdout);
        await apiClient.CompleteWorkAsync(config, work.Id, result, cancellationToken);
        log.Info($"Setup-CM client network repair completed ({work.Id}).");
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

    public static SetupCmContentLocationRemediationRequest ValidateContentLocationRemediationRequest(
        IReadOnlyDictionary<string, JsonElement> values)
    {
        RequireOnlyFields(
            values,
            "site_code",
            "client_subnet",
            "boundary_group_name",
            "distribution_point_fqdn");
        var siteCode = RequiredString(values, "site_code");
        var clientSubnet = RequiredString(values, "client_subnet");
        var boundaryGroupName = RequiredString(values, "boundary_group_name");
        var distributionPointFqdn = RequiredString(values, "distribution_point_fqdn");
        if (!string.Equals(siteCode, "LAB", StringComparison.Ordinal)
            || !string.Equals(clientSubnet, "192.168.16.0/24", StringComparison.Ordinal)
            || !string.Equals(boundaryGroupName, "LABZ1 Client Network", StringComparison.Ordinal)
            || !string.Equals(
                distributionPointFqdn,
                "LABZ1-CM01.test.gell.one",
                StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                "content location remediation request does not match the fixed LABZ1 contract.");
        }
        return new SetupCmContentLocationRemediationRequest(
            siteCode,
            clientSubnet,
            boundaryGroupName,
            "LABZ1-CM01.test.gell.one");
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
        return new ProcessOutput(await stdoutTask, await stderrTask, process.ExitCode);
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
            ?? throw new InvalidOperationException("Failed to start pwsh.exe for Setup-CM client network repair.");
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromMinutes(2));
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
        return new ProcessOutput(await stdoutTask, await stderrTask, process.ExitCode);
    }

    private static async Task<ProcessOutput> RunPowerShellAsync(
        string scriptPath,
        SetupCmContentLocationRemediationRequest request,
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
        startInfo.ArgumentList.Add("-ClientSubnet");
        startInfo.ArgumentList.Add(request.ClientSubnet);
        startInfo.ArgumentList.Add("-BoundaryGroupName");
        startInfo.ArgumentList.Add(request.BoundaryGroupName);
        startInfo.ArgumentList.Add("-DistributionPointFqdn");
        startInfo.ArgumentList.Add(request.DistributionPointFqdn);
        using var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Failed to start pwsh.exe for Setup-CM diagnostics.");
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromMinutes(5));
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

    private static Dictionary<string, object?> ParseContentLocationRemediationResult(
        string stdout,
        SetupCmContentLocationRemediationRequest request)
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
            "site_code", "client_subnet", "boundary_group_name",
            "distribution_point_fqdn", "boundary", "boundary_group",
            "distribution_points", "changed", "errors",
        };
        foreach (var name in required)
        {
            if (!document.RootElement.TryGetProperty(name, out _))
            {
                throw new InvalidOperationException($"Setup-CM remediation result is missing {name}.");
            }
        }
        RequireExactString(document.RootElement, "site_code", request.SiteCode);
        RequireExactString(document.RootElement, "client_subnet", request.ClientSubnet);
        RequireExactString(document.RootElement, "boundary_group_name", request.BoundaryGroupName);
        RequireExactString(
            document.RootElement,
            "distribution_point_fqdn",
            request.DistributionPointFqdn,
            StringComparison.OrdinalIgnoreCase);
        if (document.RootElement.GetProperty("changed").ValueKind is not JsonValueKind.True and not JsonValueKind.False)
        {
            throw new InvalidOperationException("Setup-CM remediation changed must be boolean.");
        }
        var errors = document.RootElement.GetProperty("errors");
        if (errors.ValueKind != JsonValueKind.Array || errors.GetArrayLength() != 0)
        {
            throw new InvalidOperationException("Setup-CM remediation returned errors.");
        }
        var boundary = document.RootElement.GetProperty("boundary");
        if (boundary.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException("Setup-CM remediation boundary readback must be an object.");
        }
        RequireExactString(
            boundary,
            "Value",
            NormalizeContentLocationBoundaryValue(request.ClientSubnet));
        var boundaryGroup = document.RootElement.GetProperty("boundary_group");
        if (boundaryGroup.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException("Setup-CM remediation boundary group readback must be an object.");
        }
        RequireExactString(boundaryGroup, "Name", request.BoundaryGroupName);
        var distributionPoints = document.RootElement.GetProperty("distribution_points");
        if (distributionPoints.ValueKind != JsonValueKind.Array || distributionPoints.GetArrayLength() != 1)
        {
            throw new InvalidOperationException("Setup-CM remediation must read back exactly one distribution point.");
        }
        var distributionPoint = distributionPoints[0];
        if (distributionPoint.ValueKind != JsonValueKind.Object
            || !distributionPoint.TryGetProperty("ServerNALPath", out var serverNalPath)
            || serverNalPath.ValueKind != JsonValueKind.String
            || !string.Equals(
                ExtractContentLocationDistributionPointHost(serverNalPath.GetString()!),
                request.DistributionPointFqdn,
                StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("Setup-CM remediation distribution point readback is invalid.");
        }
        return document.RootElement.EnumerateObject().ToDictionary(
            property => property.Name,
            property => (object?)property.Value.Clone(),
            StringComparer.Ordinal);
    }

    private static Dictionary<string, object?> ParseClientNetworkRepairResult(string stdout)
    {
        if (Encoding.UTF8.GetByteCount(stdout) > OutputLimitBytes)
        {
            throw new InvalidOperationException("Setup-CM client network repair output exceeded the 256 KiB limit.");
        }
        using var document = JsonDocument.Parse(stdout);
        if (document.RootElement.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException("Setup-CM client network repair output must be a JSON object.");
        }
        var required = new[]
        {
            "adapter_mac", "ipv4_address", "prefix_length", "default_gateway",
            "dns_servers", "dc_lookup", "tcp_53", "tcp_445", "errors",
        };
        foreach (var name in required)
        {
            if (!document.RootElement.TryGetProperty(name, out _))
            {
                throw new InvalidOperationException($"Setup-CM client network repair result is missing {name}.");
            }
        }
        RequireExactString(document.RootElement, "adapter_mac", "BC-24-11-9C-43-E6");
        RequireExactString(document.RootElement, "ipv4_address", "192.168.16.103");
        RequireExactString(document.RootElement, "default_gateway", "192.168.16.1");
        if (document.RootElement.GetProperty("prefix_length").GetInt32() != 24)
        {
            throw new InvalidOperationException("Setup-CM client network repair prefix length is invalid.");
        }
        var dnsServers = document.RootElement.GetProperty("dns_servers");
        if (dnsServers.ValueKind != JsonValueKind.Array || dnsServers.GetArrayLength() != 1
            || dnsServers[0].ValueKind != JsonValueKind.String
            || !string.Equals(dnsServers[0].GetString(), "192.168.16.12", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Setup-CM client network repair DNS readback is invalid.");
        }
        foreach (var name in new[] { "dc_lookup", "tcp_53", "tcp_445" })
        {
            if (document.RootElement.GetProperty(name).ValueKind != JsonValueKind.True)
            {
                throw new InvalidOperationException($"Setup-CM client network repair did not prove {name}.");
            }
        }
        var errors = document.RootElement.GetProperty("errors");
        if (errors.ValueKind != JsonValueKind.Array || errors.GetArrayLength() != 0)
        {
            throw new InvalidOperationException("Setup-CM client network repair returned errors.");
        }
        return document.RootElement.EnumerateObject().ToDictionary(
            property => property.Name,
            property => (object?)property.Value.Clone(),
            StringComparer.Ordinal);
    }

    public static string NormalizeContentLocationBoundaryValue(string clientSubnet)
    {
        return clientSubnet.Split('/', 2)[0];
    }

    public static string ExtractContentLocationDistributionPointHost(string serverNalPath)
    {
        var match = Regex.Match(
            serverNalPath,
            @"Display=\\\\(?<host>[^\""\\\]\[]+)",
            RegexOptions.CultureInvariant | RegexOptions.IgnoreCase);
        if (!match.Success)
        {
            throw new InvalidOperationException("ServerNALPath does not contain a Display host.");
        }
        return match.Groups["host"].Value.TrimEnd('.').ToLowerInvariant();
    }

    public static string FormatContentLocationRemediationFailure(
        int exitCode,
        string stdout,
        string stderr) =>
        $"Setup-CM content location remediation failed with exit code {exitCode}. "
        + $"stderr={TruncateFailureOutput(stderr)} stdout={TruncateFailureOutput(stdout)}";

    private static string TruncateFailureOutput(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return "(empty)";
        }
        return value.Length <= FailureOutputLimitChars
            ? value
            : value[..FailureOutputLimitChars] + "...[truncated]";
    }

    private static void RequireExactString(
        JsonElement parent,
        string name,
        string expected,
        StringComparison comparison = StringComparison.Ordinal)
    {
        if (!parent.TryGetProperty(name, out var value)
            || value.ValueKind != JsonValueKind.String
            || !string.Equals(value.GetString(), expected, comparison))
        {
            throw new InvalidOperationException($"Setup-CM remediation {name} did not match the request.");
        }
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

public sealed record SetupCmContentLocationRemediationRequest(
    string SiteCode,
    string ClientSubnet,
    string BoundaryGroupName,
    string DistributionPointFqdn);
