using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace AutopilotAgent;

public sealed class SetupCmModulePublishWorkService(AgentApiClient apiClient, AgentFileLog log)
{
    private const long MaximumArchiveBytes = 64L * 1024 * 1024;
    private const string ModuleRoot = @"C:\SetupCm\Modules";
    private static readonly Regex Sha256Pattern = new("^[0-9a-fA-F]{64}$", RegexOptions.CultureInvariant);
    private static readonly Regex CommitPattern = new("^[0-9a-fA-F]{40}$", RegexOptions.CultureInvariant);
    private static readonly string[] RequiredEntries =
    [
        "scripts/Invoke-SetupCm.ps1",
        "scripts/Invoke-SetupCmClient.ps1",
        "src/SetupCm/SetupCm.psd1",
        "src/SetupCm/SetupCm.psm1",
    ];

    public const string SupportedKind = "publish_setup_cm_module";

    public async Task ProcessAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var request = ValidateRequest(work.Request);
        var downloadDirectory = Path.Combine(AgentConfig.ProgramDataRoot, "setup-cm", "downloads");
        Directory.CreateDirectory(downloadDirectory);
        var downloadPath = Path.Combine(downloadDirectory, $"{Guid.NewGuid():N}.zip");
        var archiveTarget = Path.Combine(ModuleRoot, "setup-cm.zip");
        var manifestTarget = Path.Combine(ModuleRoot, "setup-cm.manifest.json");
        var archiveTemporary = archiveTarget + ".next";
        var manifestTemporary = manifestTarget + ".next";

        try
        {
            await apiClient.DownloadSetupCmModuleArtifactAsync(
                config,
                request.ArtifactId,
                downloadPath,
                cancellationToken);
            ValidateArchive(downloadPath, request);

            Directory.CreateDirectory(ModuleRoot);
            File.Copy(downloadPath, archiveTemporary, overwrite: true);
            File.WriteAllText(manifestTemporary, BuildVaultManifest(request));
            File.Move(archiveTemporary, archiveTarget, overwrite: true);
            File.Move(manifestTemporary, manifestTarget, overwrite: true);

            await apiClient.CompleteWorkAsync(
                config,
                work.Id,
                new Dictionary<string, object?>
                {
                    ["artifact_id"] = request.ArtifactId,
                    ["archive_sha256"] = request.ArchiveSha256,
                    ["source_commit"] = request.SourceCommit,
                    ["archive_filename"] = "setup-cm.zip",
                    ["manifest_filename"] = "setup-cm.manifest.json",
                },
                cancellationToken);
            log.Info($"Setup-CM module publication completed ({work.Id}).");
        }
        finally
        {
            File.Delete(downloadPath);
            File.Delete(archiveTemporary);
            File.Delete(manifestTemporary);
        }
    }

    public static SetupCmModulePublishRequest ValidateRequest(
        IReadOnlyDictionary<string, JsonElement> values)
    {
        RequireOnlyFields(values, "artifact_id", "archive_sha256", "source_commit");
        var artifactId = RequiredString(values, "artifact_id");
        var archiveSha256 = RequiredString(values, "archive_sha256").ToLowerInvariant();
        var sourceCommit = RequiredString(values, "source_commit").ToLowerInvariant();
        if (!Guid.TryParse(artifactId, out _))
        {
            throw new InvalidOperationException("artifact_id must be a UUID.");
        }
        if (!Sha256Pattern.IsMatch(archiveSha256))
        {
            throw new InvalidOperationException("archive_sha256 must be a 64-character hexadecimal SHA-256 value.");
        }
        if (!CommitPattern.IsMatch(sourceCommit))
        {
            throw new InvalidOperationException("source_commit must be a 40-character hexadecimal Git SHA.");
        }
        return new SetupCmModulePublishRequest(artifactId, archiveSha256, sourceCommit);
    }

    public static void ValidateArchive(string archivePath, SetupCmModulePublishRequest request)
    {
        var info = new FileInfo(archivePath);
        if (!info.Exists || info.Length == 0 || info.Length > MaximumArchiveBytes)
        {
            throw new InvalidOperationException("Setup-CM module archive size is invalid.");
        }
        using (var stream = File.OpenRead(archivePath))
        {
            var actualHash = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
            if (!string.Equals(actualHash, request.ArchiveSha256, StringComparison.Ordinal))
            {
                throw new InvalidOperationException("Setup-CM module archive SHA-256 validation failed.");
            }
        }

        var entryNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using var archive = ZipFile.OpenRead(archivePath);
        foreach (var entry in archive.Entries)
        {
            var name = entry.FullName.Replace('\\', '/');
            if (string.IsNullOrWhiteSpace(name)
                || name.StartsWith("/", StringComparison.Ordinal)
                || name.Split('/').Any(part => part is "." or "..")
                || !entryNames.Add(name))
            {
                throw new InvalidOperationException("Setup-CM module archive contains an unsafe ZIP entry.");
            }
        }
        foreach (var requiredEntry in RequiredEntries)
        {
            if (!entryNames.Contains(requiredEntry))
            {
                throw new InvalidOperationException($"Setup-CM module archive is missing {requiredEntry}.");
            }
        }
    }

    public static string BuildVaultManifest(SetupCmModulePublishRequest request) =>
        JsonSerializer.Serialize(new
        {
            schema_version = 1,
            filename = "setup-cm.zip",
            sha256 = request.ArchiveSha256.ToUpperInvariant(),
            source_commit = request.SourceCommit,
            published_at_utc = DateTimeOffset.UtcNow.ToString("O"),
        });

    private static void RequireOnlyFields(
        IReadOnlyDictionary<string, JsonElement> values,
        params string[] allowed)
    {
        var unexpected = values.Keys.FirstOrDefault(key => !allowed.Contains(key, StringComparer.Ordinal));
        if (unexpected is not null)
        {
            throw new InvalidOperationException($"Unexpected Setup-CM module publication field: {unexpected}");
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
}

public sealed record SetupCmModulePublishRequest(
    string ArtifactId,
    string ArchiveSha256,
    string SourceCommit);
