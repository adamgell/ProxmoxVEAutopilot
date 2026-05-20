using System.IO.Compression;
using System.Text.Json;

namespace AutopilotAgent;

public sealed class LogCollectionService(
    AgentApiClient apiClient,
    AgentFileLog log)
{
    private const long MaxFileBytes = 256L * 1024L * 1024L;

    public async Task CollectAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var outputDir = Path.Combine(AgentConfig.ProgramDataRoot, "log-bundles");
        Directory.CreateDirectory(outputDir);
        var safeWorkId = SafeName(work.Id);
        var bundlePath = Path.Combine(outputDir, $"{safeWorkId}_logs.zip");
        if (System.IO.File.Exists(bundlePath))
        {
            System.IO.File.Delete(bundlePath);
        }

        var manifest = CreateBundle(bundlePath, BuildKnownWindowsLogSources(), cancellationToken);
        var upload = await apiClient.UploadArtifactAsync(
            config,
            work.Id,
            "log",
            bundlePath,
            new Dictionary<string, object?>
            {
                ["source"] = "agent-v1",
                ["bundle_type"] = "known-windows-logs",
                ["known_sources"] = "cmtraceopen-windows",
                ["file_count"] = manifest.Files.Count,
                ["missing_source_count"] = manifest.MissingSources.Count,
                ["skipped_file_count"] = manifest.SkippedFiles.Count,
            },
            cancellationToken);

        await apiClient.CompleteWorkAsync(
            config,
            work.Id,
            new Dictionary<string, object?>
            {
                ["source"] = "agent-v1",
                ["bundle_type"] = "known-windows-logs",
                ["filename"] = Path.GetFileName(bundlePath),
                ["artifact"] = upload.Artifact,
                ["file_count"] = manifest.Files.Count,
                ["missing_sources"] = manifest.MissingSources,
                ["skipped_files"] = manifest.SkippedFiles,
            },
            cancellationToken);
        log.Info($"Collected {manifest.Files.Count} log files for work item {work.Id}: {bundlePath}");
    }

    public static IReadOnlyList<KnownLogSource> BuildKnownWindowsLogSources() =>
    [
        Folder("windows-intune-ime-logs", @"C:\ProgramData\Microsoft\IntuneManagementExtension\Logs", "Intune IME Logs Folder", ["IntuneManagementExtension.log", "AppWorkload.log", "AppActionProcessor.log", "AgentExecutor.log", "HealthScripts.log", "*.log"]),
        File("windows-intune-ime-intunemanagementextension-log", @"C:\ProgramData\Microsoft\IntuneManagementExtension\Logs\IntuneManagementExtension.log", "Intune IME: IntuneManagementExtension.log", ["IntuneManagementExtension*.log"]),
        File("windows-intune-ime-appworkload-log", @"C:\ProgramData\Microsoft\IntuneManagementExtension\Logs\AppWorkload.log", "Intune IME: AppWorkload.log", ["AppWorkload*.log"]),
        File("windows-intune-ime-agentexecutor-log", @"C:\ProgramData\Microsoft\IntuneManagementExtension\Logs\AgentExecutor.log", "Intune IME: AgentExecutor.log", ["AgentExecutor*.log"]),
        Folder("windows-dmclient-logs", @"C:\Windows\System32\config\systemprofile\AppData\Local\mdm", "DMClient Local Logs", ["*.log"]),
        Folder("windows-configmgr-ccm-logs", @"C:\Windows\CCM\Logs", "CCM Logs Folder", ["*.log"]),
        Folder("windows-configmgr-ccmsetup-logs", @"C:\Windows\ccmsetup\Logs", "ccmsetup Logs Folder", ["*.log"]),
        Folder("windows-configmgr-swmtr", @"C:\Windows\System32\SWMTRReporting", "Software Metering Logs", ["*.log"]),
        File("windows-panther-setupact-log", @"C:\Windows\Panther\setupact.log", "setupact.log (Panther)", ["setupact.log"]),
        File("windows-panther-setuperr-log", @"C:\Windows\Panther\setuperr.log", "setuperr.log (Panther)", ["setuperr.log"]),
        File("windows-cbs-log", @"C:\Windows\Logs\CBS\CBS.log", "CBS.log", ["CBS.log"]),
        File("windows-dism-log", @"C:\Windows\Logs\DISM\dism.log", "DISM.log", ["dism.log"]),
        File("windows-reporting-events-log", @"C:\Windows\SoftwareDistribution\ReportingEvents.log", "ReportingEvents.log", ["ReportingEvents.log"]),
        Folder("windows-iis-logs", @"C:\inetpub\logs\LogFiles", "IIS Logs", ["u_ex*.log", "*.log"]),
        Folder("windows-deployment-logs-software", @"C:\Windows\Logs\Software", "Software Logs Folder", ["*.log"]),
        Folder("windows-deployment-ccmcache", @"C:\Windows\ccmcache", "ccmcache Folder", ["*.log"]),
        Folder("windows-deployment-psadt", @"C:\Windows\Logs\Software", "PSADT Logs Folder", ["*_PSAppDeployToolkit*.log", "*Deploy-Application*.log", "*.log"]),
        Folder("windows-deployment-msi-log", @"C:\Windows\Temp", "MSI Verbose Log Folder", ["MSI*.LOG", "MSI*.log"]),
        Folder("windows-deployment-patchmypc-logs", @"C:\ProgramData\PatchMyPC\Logs", "PatchMyPC Logs Folder", ["*.log"]),
        Folder("windows-deployment-patchmypc-install-logs", @"C:\ProgramData\PatchMyPCInstallLogs", "PatchMyPC Install Logs", ["*.log"]),
        Folder("windows-deployment-patchmypc-intune-logs", @"C:\ProgramData\PatchMyPCIntuneLogs", "PatchMyPC Intune Logs", ["*.log"]),
        Folder("autopilotagent-logs", AgentConfig.LogDirectory, "AutopilotAgent Logs", ["*.log"]),
    ];

    private static CollectionManifest CreateBundle(
        string bundlePath,
        IReadOnlyList<KnownLogSource> sources,
        CancellationToken cancellationToken)
    {
        var manifest = new CollectionManifest();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using var archive = ZipFile.Open(bundlePath, ZipArchiveMode.Create);
        foreach (var source in sources)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var files = DiscoverFiles(source).ToList();
            if (files.Count == 0)
            {
                manifest.MissingSources.Add(new SourceStatus(source.Id, source.Path, "missing_or_empty"));
                continue;
            }
            foreach (var file in files)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (!seen.Add(file))
                {
                    continue;
                }
                try
                {
                    var info = new FileInfo(file);
                    if (!info.Exists)
                    {
                        continue;
                    }
                    if (info.Length > MaxFileBytes)
                    {
                        manifest.SkippedFiles.Add(new SkippedFile(file, "too_large", info.Length));
                        continue;
                    }
                    var entryName = $"sources/{SafeName(source.Id)}/{RelativeName(source, file)}";
                    archive.CreateEntryFromFile(file, entryName, CompressionLevel.SmallestSize);
                    manifest.Files.Add(new CollectedFile(source.Id, file, entryName, info.Length));
                }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
                {
                    manifest.SkippedFiles.Add(new SkippedFile(file, ex.GetType().Name, null));
                }
            }
        }

        var manifestEntry = archive.CreateEntry("manifest.json", CompressionLevel.SmallestSize);
        using var writer = new StreamWriter(manifestEntry.Open());
        writer.Write(JsonSerializer.Serialize(manifest, AgentConfig.JsonOptions()));
        return manifest;
    }

    private static IEnumerable<string> DiscoverFiles(KnownLogSource source)
    {
        if (source.PathKind == KnownSourcePathKind.File)
        {
            if (System.IO.File.Exists(source.Path))
            {
                yield return source.Path;
            }
            var parent = Path.GetDirectoryName(source.Path);
            if (string.IsNullOrWhiteSpace(parent) || !Directory.Exists(parent))
            {
                yield break;
            }
            foreach (var pattern in source.Patterns)
            {
                foreach (var file in SafeEnumerate(parent, pattern, SearchOption.TopDirectoryOnly))
                {
                    yield return file;
                }
            }
            yield break;
        }

        if (!Directory.Exists(source.Path))
        {
            yield break;
        }
        foreach (var pattern in source.Patterns.DefaultIfEmpty("*"))
        {
            foreach (var file in SafeEnumerate(source.Path, pattern, SearchOption.AllDirectories))
            {
                yield return file;
            }
        }
    }

    private static IEnumerable<string> SafeEnumerate(
        string path,
        string pattern,
        SearchOption searchOption)
    {
        try
        {
            return Directory.EnumerateFiles(path, pattern, searchOption);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return [];
        }
    }

    private static string RelativeName(KnownLogSource source, string file)
    {
        var root = source.PathKind == KnownSourcePathKind.Folder
            ? source.Path
            : Path.GetDirectoryName(source.Path) ?? source.Path;
        try
        {
            return Path.GetRelativePath(root, file).Replace('\\', '/');
        }
        catch
        {
            return Path.GetFileName(file);
        }
    }

    private static string SafeName(string value) =>
        string.Concat(value.Select(ch => char.IsLetterOrDigit(ch) || ch is '-' or '_' or '.' ? ch : '_'));

    private static KnownLogSource File(string id, string path, string label, string[] patterns) =>
        new(id, label, KnownSourcePathKind.File, path, patterns);

    private static KnownLogSource Folder(string id, string path, string label, string[] patterns) =>
        new(id, label, KnownSourcePathKind.Folder, path, patterns);
}

public sealed record KnownLogSource(
    string Id,
    string Label,
    KnownSourcePathKind PathKind,
    string Path,
    IReadOnlyList<string> Patterns);

public enum KnownSourcePathKind
{
    File,
    Folder,
}

public sealed class CollectionManifest
{
    public List<CollectedFile> Files { get; } = [];
    public List<SourceStatus> MissingSources { get; } = [];
    public List<SkippedFile> SkippedFiles { get; } = [];
}

public sealed record CollectedFile(string SourceId, string Path, string EntryName, long Bytes);
public sealed record SourceStatus(string SourceId, string Path, string Status);
public sealed record SkippedFile(string Path, string Reason, long? Bytes);
