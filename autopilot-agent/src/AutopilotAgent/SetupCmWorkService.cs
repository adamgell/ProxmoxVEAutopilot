using System.Diagnostics;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace AutopilotAgent;

public sealed class SetupCmWorkService(AgentApiClient apiClient, AgentFileLog log)
{
    private const string SetupCmRoot = @"C:\ProgramData\SetupCm\";
    private const string VaultRoot = @"\\LABZ1-DC02\SetupCm\";
    private const int OutputLimitBytes = 256 * 1024;

    public static readonly string[] SupportedKinds =
    [
        "setup_cm_acquire",
        "setup_cm_sql",
        "setup_cm_mecm",
        "setup_cm_health",
        "setup_cm_client_install",
    ];

    public async Task ProcessAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var request = ValidateRequest(work.Kind, work.Request);
        var workRoot = Path.Combine(
            AgentConfig.ProgramDataRoot,
            "setup-cm",
            Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(work.Id)))[..16]);
        var archivePath = Path.Combine(workRoot, "setup-cm.zip");
        var sourceRoot = Path.Combine(workRoot, "source");
        Directory.CreateDirectory(workRoot);
        File.Copy(request.ModuleArchivePath, archivePath, overwrite: true);
        VerifySha256(archivePath, request.ModuleArchiveSha256);
        if (Directory.Exists(sourceRoot))
        {
            Directory.Delete(sourceRoot, recursive: true);
        }
        ZipFile.ExtractToDirectory(archivePath, sourceRoot);
        var entryScript = request.Stage == "Client" ? "Invoke-SetupCmClient.ps1" : "Invoke-SetupCm.ps1";
        ValidateExtractedModule(sourceRoot, entryScript);

        var entryPoint = Path.Combine(sourceRoot, "scripts", entryScript);
        ProcessOutput output;
        if (request.Stage == "Client")
        {
            var manifestPath = Path.Combine(workRoot, "client-manifest.json");
            File.WriteAllText(
                manifestPath,
                JsonSerializer.Serialize(new
                {
                    siteCode = request.SiteCode,
                    managementPointFqdn = request.ManagementPointFqdn,
                    evidenceRoot = request.EvidenceRoot,
                }));
            output = await RunPowerShellAsync(
                entryPoint,
                ["-ManifestPath", manifestPath],
                request.Stage,
                cancellationToken);
        }
        else
        {
            output = await RunPowerShellAsync(
                entryPoint,
                ["-ConfigPath", request.ConfigPath!, "-Mode", "Unattended", "-Stage", request.Stage],
                request.Stage,
                cancellationToken);
        }
        var result = new Dictionary<string, object?>
        {
            ["stage"] = request.Stage,
            ["archive_sha256"] = request.ModuleArchiveSha256,
            ["evidence_root"] = request.EvidenceRoot,
            ["stdout"] = Truncate(output.Stdout),
            ["stderr"] = Truncate(output.Stderr),
        };
        await apiClient.CompleteWorkAsync(config, work.Id, result, cancellationToken);
        log.Info($"Setup-CM stage completed: {request.Stage} ({work.Id}).");
    }

    public static SetupCmWorkRequest ValidateRequest(
        string kind,
        IReadOnlyDictionary<string, JsonElement> values)
    {
        if (kind == "setup_cm_client_install")
        {
            return ValidateClientInstallRequest(values);
        }
        var stage = kind switch
        {
            "setup_cm_acquire" => "Acquire",
            "setup_cm_sql" => "Sql",
            "setup_cm_mecm" => "Mecm",
            "setup_cm_health" => "Health",
            _ => throw new InvalidOperationException($"Unsupported Setup-CM work kind: {kind}"),
        };
        var configPath = RequiredString(values, "config_path");
        var evidenceRoot = RequiredString(values, "evidence_root");
        var moduleArchivePath = RequiredString(values, "module_archive_path");
        var moduleArchiveSha256 = RequiredString(values, "module_archive_sha256").ToLowerInvariant();
        if (!IsInside(configPath, SetupCmRoot) || !configPath.EndsWith(".yaml", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("config_path must be a YAML file below C:\\ProgramData\\SetupCm.");
        }
        if (!IsInside(evidenceRoot, SetupCmRoot))
        {
            throw new InvalidOperationException("evidence_root must be below C:\\ProgramData\\SetupCm.");
        }
        if ((!IsInside(moduleArchivePath, SetupCmRoot) && !IsInside(moduleArchivePath, VaultRoot))
            || !moduleArchivePath.EndsWith(".zip", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("module_archive_path must be a ZIP below an approved Setup-CM root.");
        }
        if (moduleArchiveSha256.Length != 64 || !moduleArchiveSha256.All(Uri.IsHexDigit))
        {
            throw new InvalidOperationException("module_archive_sha256 must be a 64-character hexadecimal SHA-256 value.");
        }
        return new SetupCmWorkRequest(stage, configPath, evidenceRoot, moduleArchivePath, moduleArchiveSha256, null, null);
    }

    public static void ValidateExtractedModule(string sourceRoot, string entryScript = "Invoke-SetupCm.ps1")
    {
        foreach (var relativePath in new[]
        {
            Path.Combine("scripts", entryScript),
            Path.Combine("src", "SetupCm", "SetupCm.psd1"),
            Path.Combine("src", "SetupCm", "SetupCm.psm1"),
        })
        {
            var path = Path.Combine(sourceRoot, relativePath);
            if (!File.Exists(path))
            {
                throw new InvalidOperationException($"Setup-CM module archive is missing {relativePath}.");
            }
        }
    }

    private static SetupCmWorkRequest ValidateClientInstallRequest(
        IReadOnlyDictionary<string, JsonElement> values)
    {
        RequireOnlyFields(
            values,
            "site_code",
            "management_point_fqdn",
            "evidence_root",
            "module_archive_path",
            "module_archive_sha256");
        var siteCode = RequiredString(values, "site_code");
        var managementPointFqdn = RequiredString(values, "management_point_fqdn");
        var evidenceRoot = RequiredString(values, "evidence_root");
        var moduleArchivePath = RequiredString(values, "module_archive_path");
        var moduleArchiveSha256 = RequiredString(values, "module_archive_sha256").ToLowerInvariant();
        if (!string.Equals(siteCode, "LAB", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("site_code must be LAB.");
        }
        if (!string.Equals(managementPointFqdn, "LABZ1-CM01.test.gell.one", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("management_point_fqdn must be LABZ1-CM01.test.gell.one.");
        }
        if (!IsInside(evidenceRoot, SetupCmRoot))
        {
            throw new InvalidOperationException("evidence_root must be below C:\\ProgramData\\SetupCm.");
        }
        if ((!IsInside(moduleArchivePath, SetupCmRoot) && !IsInside(moduleArchivePath, VaultRoot))
            || !moduleArchivePath.EndsWith(".zip", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("module_archive_path must be a ZIP below an approved Setup-CM root.");
        }
        if (moduleArchiveSha256.Length != 64 || !moduleArchiveSha256.All(Uri.IsHexDigit))
        {
            throw new InvalidOperationException("module_archive_sha256 must be a 64-character hexadecimal SHA-256 value.");
        }
        return new SetupCmWorkRequest(
            "Client",
            null,
            evidenceRoot,
            moduleArchivePath,
            moduleArchiveSha256,
            siteCode,
            managementPointFqdn);
    }

    private static bool IsInside(string candidate, string root)
    {
        var normalizedCandidate = candidate.Replace('/', '\\').Trim();
        var normalizedRoot = root.Replace('/', '\\').TrimEnd('\\') + "\\";
        if (!normalizedCandidate.StartsWith(normalizedRoot, StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }
        return normalizedCandidate
            .Split('\\', StringSplitOptions.RemoveEmptyEntries)
            .All(part => part is not "." and not "..");
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

    private static void RequireOnlyFields(
        IReadOnlyDictionary<string, JsonElement> values,
        params string[] allowed)
    {
        var unexpected = values.Keys.FirstOrDefault(key => !allowed.Contains(key, StringComparer.Ordinal));
        if (unexpected is not null)
        {
            throw new InvalidOperationException($"Unexpected Setup-CM client request field: {unexpected}");
        }
    }

    private static void VerifySha256(string path, string expected)
    {
        using var stream = File.OpenRead(path);
        var actual = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
        if (!string.Equals(actual, expected, StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Setup-CM module archive SHA-256 validation failed.");
        }
    }

    private static async Task<ProcessOutput> RunPowerShellAsync(
        string entryPoint,
        IEnumerable<string> arguments,
        string stage,
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
        startInfo.ArgumentList.Add(entryPoint);
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }
        using var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Failed to start pwsh.exe for Setup-CM work.");
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromHours(3));
        var stdoutTask = process.StandardOutput.ReadToEndAsync(timeout.Token);
        var stderrTask = process.StandardError.ReadToEndAsync(timeout.Token);
        await process.WaitForExitAsync(timeout.Token);
        var output = new ProcessOutput(
            await stdoutTask,
            await stderrTask,
            process.ExitCode);
        if (output.ExitCode != 0)
        {
            throw new InvalidOperationException($"Setup-CM {stage} stage failed with exit code {output.ExitCode}: {Truncate(output.Stderr)}");
        }
        return output;
    }

    private static string Truncate(string value) =>
        value.Length <= OutputLimitBytes ? value : value[..OutputLimitBytes] + "\n...[truncated]";

    private sealed record ProcessOutput(string Stdout, string Stderr, int ExitCode);
}

public sealed record SetupCmWorkRequest(
    string Stage,
    string? ConfigPath,
    string EvidenceRoot,
    string ModuleArchivePath,
    string ModuleArchiveSha256,
    string? SiteCode,
    string? ManagementPointFqdn);
