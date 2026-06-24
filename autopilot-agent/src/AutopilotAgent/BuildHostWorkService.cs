using System.Diagnostics;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json;

namespace AutopilotAgent;

public sealed class BuildHostWorkService(
    AgentApiClient apiClient,
    AgentFileLog log)
{
    public static readonly string[] SupportedKinds =
    [
        "configure_build_host_role",
        "install_build_prerequisites",
        "fetch_source_bundle",
        "build_agent_msi",
        "build_winpe",
        "build_cloudosd",
        "build_osdeploy",
        "publish_artifacts",
    ];

    public async Task ProcessAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var result = work.Kind switch
        {
            "configure_build_host_role" => ConfigureBuildHostRole(config, work),
            "install_build_prerequisites" => await InstallPrerequisitesAsync(work, cancellationToken),
            "fetch_source_bundle" => await FetchSourceBundleAsync(config, work, cancellationToken),
            "build_agent_msi" => await BuildAgentMsiAsync(config, work, cancellationToken),
            "build_winpe" => await BuildWinPeAsync(config, work, cancellationToken),
            "build_cloudosd" => await BuildCloudOsdAsync(config, work, cancellationToken),
            "build_osdeploy" => await BuildOsDeployAsync(config, work, cancellationToken),
            "publish_artifacts" => await PublishArtifactsAsync(config, work, cancellationToken),
            _ => throw new InvalidOperationException($"Unsupported build-host work kind: {work.Kind}"),
        };
        await apiClient.CompleteWorkAsync(config, work.Id, result, cancellationToken);
    }

    private Dictionary<string, object?> ConfigureBuildHostRole(
        AgentConfig config,
        AgentWorkItem work)
    {
        var workRoot = WorkRoot(work);
        Directory.CreateDirectory(workRoot);
        config.Phase = "build-host";
        config.Role = "build-host";
        config.Capabilities = ReadStringArray(
            work.Request,
            "capabilities",
            SupportedKinds);
        config.Save();
        return new Dictionary<string, object?>
        {
            ["phase"] = config.Phase,
            ["role"] = config.Role,
            ["capabilities"] = config.Capabilities,
            ["work_root"] = workRoot,
            ["runtime"] = RuntimeInformation(),
        };
    }

    private async Task<Dictionary<string, object?>> InstallPrerequisitesAsync(
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var workRoot = WorkRoot(work);
        Directory.CreateDirectory(workRoot);
        var installAdk = ReadBool(work.Request, "install_adk", true);
        var adkUrl = ReadString(
            work.Request,
            "adk_url",
            "https://go.microsoft.com/fwlink/?linkid=2289980");
        var winpeUrl = ReadString(
            work.Request,
            "winpe_addon_url",
            "https://go.microsoft.com/fwlink/?linkid=2289981");
        var osDeployVersion = ReadString(work.Request, "osdeploy_version", "26.1.30.5");
        var osdBuilderVersion = ReadString(work.Request, "osdbuilder_version", "24.10.8.1");
        var script = $$"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
New-Item -ItemType Directory -Force -Path '{{PowerShellLiteral(workRoot)}}' | Out-Null

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue) -or -not (& dotnet --list-sdks 2>$null)) {
  $dotnetInstall = Join-Path '{{PowerShellLiteral(workRoot)}}' 'dotnet-install.ps1'
  Invoke-WebRequest -UseBasicParsing -Uri 'https://dot.net/v1/dotnet-install.ps1' -OutFile $dotnetInstall
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $dotnetInstall -Channel 8.0 -InstallDir "$env:ProgramFiles\dotnet"
}
$dotnetRoot = Join-Path $env:ProgramFiles 'dotnet'
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
if ($machinePath -notlike "*$dotnetRoot*") {
  [Environment]::SetEnvironmentVariable('Path', "$dotnetRoot;$machinePath", 'Machine')
}
$env:Path = "$dotnetRoot;$env:Path"
$dotnetVersion = (& dotnet --version)

if ({{(installAdk ? "$true" : "$false")}}) {
  $kits = "${env:ProgramFiles(x86)}\Windows Kits\10\Assessment and Deployment Kit"
  $oscdimg = Join-Path $kits 'Deployment Tools\amd64\Oscdimg\oscdimg.exe'
  $copype = Join-Path $kits 'Windows Preinstallation Environment\copype.cmd'
  if (-not (Test-Path -LiteralPath $oscdimg) -or -not (Test-Path -LiteralPath $copype)) {
    $adkSetup = Join-Path '{{PowerShellLiteral(workRoot)}}' 'adksetup.exe'
    $winpeSetup = Join-Path '{{PowerShellLiteral(workRoot)}}' 'adkwinpesetup.exe'
    Invoke-WebRequest -UseBasicParsing -Uri '{{PowerShellLiteral(adkUrl)}}' -OutFile $adkSetup
    Start-Process -FilePath $adkSetup -ArgumentList @('/features','OptionId.DeploymentTools','OptionId.ImagingAndConfigurationDesigner','/quiet','/ceip','off','/norestart') -Wait
    Invoke-WebRequest -UseBasicParsing -Uri '{{PowerShellLiteral(winpeUrl)}}' -OutFile $winpeSetup
    Start-Process -FilePath $winpeSetup -ArgumentList @('/features','OptionId.WindowsPreinstallationEnvironment','/quiet','/ceip','off','/norestart') -Wait
  }
}

[Net.ServicePointManager]::SecurityProtocol =
  [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Read-StepLog {
  param([Parameter(Mandatory)][string] $Path)
  if (-not (Test-Path -LiteralPath $Path)) { return '' }
  $text = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
  if (-not $text) { return '' }
  if ($text.Length -gt 4000) {
    return $text.Substring(0, 4000) + "`n...[truncated]"
  }
  return $text
}

function Invoke-BoundedPowerShell {
  param(
    [Parameter(Mandatory)][string] $Name,
    [Parameter(Mandatory)][string] $Script,
    [int] $TimeoutSeconds = 600
  )
  $safeName = $Name -replace '[^A-Za-z0-9_.-]', '-'
  $scriptPath = Join-Path '{{PowerShellLiteral(workRoot)}}' "$safeName.ps1"
  $stdoutPath = Join-Path '{{PowerShellLiteral(workRoot)}}' "$safeName.stdout.log"
  $stderrPath = Join-Path '{{PowerShellLiteral(workRoot)}}' "$safeName.stderr.log"
  Set-Content -LiteralPath $scriptPath -Value $Script -Encoding UTF8
  Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue
  $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $startInfo.FileName = 'powershell.exe'
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true
  $escapedScriptPath = $scriptPath.Replace('"', '\"')
  $startInfo.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$escapedScriptPath`""
  $process = [System.Diagnostics.Process]::new()
  $process.StartInfo = $startInfo
  [void]$process.Start()
  $stdoutTask = $process.StandardOutput.ReadToEndAsync()
  $stderrTask = $process.StandardError.ReadToEndAsync()
  if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    $process.WaitForExit()
    $stdoutText = $stdoutTask.GetAwaiter().GetResult()
    $stderrText = $stderrTask.GetAwaiter().GetResult()
    Set-Content -LiteralPath $stdoutPath -Value $stdoutText -Encoding UTF8
    Set-Content -LiteralPath $stderrPath -Value $stderrText -Encoding UTF8
    throw "$Name timed out after $TimeoutSeconds seconds. stdout=$(Read-StepLog $stdoutPath) stderr=$(Read-StepLog $stderrPath)"
  }
  $process.WaitForExit()
  $stdoutText = $stdoutTask.GetAwaiter().GetResult()
  $stderrText = $stderrTask.GetAwaiter().GetResult()
  Set-Content -LiteralPath $stdoutPath -Value $stdoutText -Encoding UTF8
  Set-Content -LiteralPath $stderrPath -Value $stderrText -Encoding UTF8
  $exitCode = $process.ExitCode
  $process.Dispose()
  if ($exitCode -ne 0) {
    throw "$Name failed with exit code $exitCode. stdout=$(Read-StepLog $stdoutPath) stderr=$(Read-StepLog $stderrPath)"
  }
  return [pscustomobject]@{
    name = $Name
    stdout = (Read-StepLog $stdoutPath)
    stderr = (Read-StepLog $stderrPath)
  }
}

Invoke-BoundedPowerShell -Name 'install-nuget-provider' -TimeoutSeconds 600 -Script @'
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol =
  [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$nugetProvider = Get-PackageProvider -Name NuGet -ErrorAction SilentlyContinue
if (-not $nugetProvider) {
  Install-PackageProvider -Name NuGet -MinimumVersion '2.8.5.201' -ForceBootstrap -Force -Scope AllUsers -Confirm:$false
}
Import-PackageProvider -Name NuGet -Force | Out-Null
'@ | Out-Null
foreach ($moduleSpec in @(
  @{ Name = 'OSD'; RequiredVersion = '{{PowerShellLiteral(osDeployVersion)}}' },
  @{ Name = 'OSDBuilder'; RequiredVersion = '{{PowerShellLiteral(osdBuilderVersion)}}' }
)) {
  $installed = Get-Module -ListAvailable -Name $moduleSpec.Name |
    Where-Object { $_.Version.ToString() -eq $moduleSpec.RequiredVersion } |
    Select-Object -First 1
  if (-not $installed) {
    $moduleInstallScript = @"
`$ErrorActionPreference = 'Stop'
`$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol =
  [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
Import-PackageProvider -Name NuGet -Force | Out-Null
Install-Module -Name '$($moduleSpec.Name)' -RequiredVersion '$($moduleSpec.RequiredVersion)' -Scope AllUsers -Force -AllowClobber -Confirm:`$false
"@
    Invoke-BoundedPowerShell -Name "install-module-$($moduleSpec.Name)" -TimeoutSeconds 2700 -Script $moduleInstallScript | Out-Null
  }
}
$modules = Get-Module -ListAvailable -Name OSD,OSDBuilder |
  Sort-Object Name,Version -Descending |
  Select-Object Name,Version,ModuleBase

[pscustomobject]@{
  dotnet_version = $dotnetVersion
  adk_requested = [bool]{{(installAdk ? "$true" : "$false")}}
  modules = $modules
  work_root = '{{PowerShellLiteral(workRoot)}}'
} | ConvertTo-Json -Compress
""";
        var output = await RunPowerShellAsync(script, TimeSpan.FromMinutes(90), cancellationToken);
        return new Dictionary<string, object?>
        {
            ["work_root"] = workRoot,
            ["stdout"] = Truncate(output.Stdout),
            ["stderr"] = Truncate(output.Stderr),
        };
    }

    private async Task<Dictionary<string, object?>> FetchSourceBundleAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var workRoot = WorkRoot(work);
        var sourceRoot = SourceRoot(work);
        Directory.CreateDirectory(workRoot);
        if (Directory.Exists(sourceRoot))
        {
            Directory.Delete(sourceRoot, recursive: true);
        }
        Directory.CreateDirectory(sourceRoot);
        var bundleUrl = ReadString(
            work.Request,
            "source_bundle_url",
            $"{config.ServerUrl?.TrimEnd('/')}/api/setup/v1/source-bundle.zip");
        var archive = Path.Combine(workRoot, "source-bundle.zip");
        using (var http = new HttpClient { Timeout = TimeSpan.FromMinutes(20) })
        await using (var input = await http.GetStreamAsync(bundleUrl, cancellationToken))
        await using (var output = File.Create(archive))
        {
            await input.CopyToAsync(output, cancellationToken);
        }
        ZipFile.ExtractToDirectory(archive, sourceRoot, overwriteFiles: true);
        var manifestPath = Path.Combine(sourceRoot, "autopilot-source-manifest.json");
        var manifest = File.Exists(manifestPath)
            ? JsonSerializer.Deserialize<Dictionary<string, object?>>(
                await File.ReadAllTextAsync(manifestPath, cancellationToken))
            : new Dictionary<string, object?>();
        return new Dictionary<string, object?>
        {
            ["source_root"] = sourceRoot,
            ["bundle_url"] = bundleUrl,
            ["manifest"] = manifest,
        };
    }

    private async Task<Dictionary<string, object?>> BuildAgentMsiAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var sourceRoot = SourceRoot(work);
        var outputRoot = Path.Combine(WorkRoot(work), "outputs", "agent");
        Directory.CreateDirectory(outputRoot);
        var scriptPath = Path.Combine(sourceRoot, "autopilot-agent", "scripts", "Build-AutopilotAgent.ps1");
        if (!File.Exists(scriptPath))
        {
            throw new FileNotFoundException("AutopilotAgent MSI build script is missing.", scriptPath);
        }
        var version = ReadString(work.Request, "agent_version", "0.1.2");
        var rids = ReadStringArray(work.Request, "runtime_identifiers", ["win-x64", "win-arm64"]);
        var ridLiteral = string.Join(",", rids.Select(item => $"'{PowerShellLiteral(item)}'"));
        var command = $"$env:Path = \"$env:ProgramFiles\\dotnet;$env:Path\"; & '{PowerShellLiteral(scriptPath)}' -Version '{PowerShellLiteral(version)}' -RuntimeIdentifiers @({ridLiteral}) -OutputRoot '{PowerShellLiteral(outputRoot)}'";
        var output = await RunPowerShellAsync(command, TimeSpan.FromMinutes(60), cancellationToken);
        var uploads = new List<Dictionary<string, object?>>();
        foreach (var msi in Directory.EnumerateFiles(outputRoot, "*.msi", SearchOption.AllDirectories))
        {
            uploads.Add(await UploadAsync(config, work, "agent-msi", msi, cancellationToken));
        }
        if (uploads.Count == 0)
        {
            throw new InvalidOperationException(
                "AutopilotAgent MSI build completed without producing an MSI. "
                + $"stdout={Truncate(output.Stdout, 4000)} stderr={Truncate(output.Stderr, 4000)}");
        }
        return new Dictionary<string, object?>
        {
            ["output_root"] = outputRoot,
            ["uploads"] = uploads,
            ["stdout"] = Truncate(output.Stdout),
            ["stderr"] = Truncate(output.Stderr),
        };
    }

    private async Task<string> StageVirtioDriversAsync(
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var workRoot = WorkRoot(work);
        var destination = Path.Combine(workRoot, "inputs", "virtio-win");
        var script = $$"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$destination = '{{PowerShellLiteral(destination)}}'
$requiredInf = @('vioscsi.inf', 'netkvm.inf', 'vioser.inf')

function Test-VirtioRoot {
  param([Parameter(Mandatory)][string] $Root)
  if (-not (Test-Path -LiteralPath $Root)) { return $false }
  foreach ($infName in $requiredInf) {
    if (-not (Get-ChildItem -LiteralPath $Root -Recurse -Filter $infName -ErrorAction SilentlyContinue | Select-Object -First 1)) {
      return $false
    }
  }
  return $true
}

if (-not (Test-VirtioRoot -Root $destination)) {
  New-Item -ItemType Directory -Force -Path $destination | Out-Null
  $sources = @()
  if ($env:AUTOPILOT_VIRTIO_ROOT) { $sources += $env:AUTOPILOT_VIRTIO_ROOT }
  $sources += @(
    'C:\BuildRoot\ProxmoxVEAutopilot\inputs\virtio-win',
    'C:\BuildRoot\ProxmoxVEAutopilot\inputs\virtio',
    'C:\BuildRoot\inputs\virtio-win',
    'C:\BuildRoot\inputs\virtio',
    'E:\BuildRoot\inputs\virtio-win',
    'E:\BuildRoot\inputs\virtio',
    'E:\',
    'D:\virtio',
    'D:\',
    'F:\BuildRoot\inputs\virtio-win',
    'F:\BuildRoot\inputs\virtio',
    'F:\'
  )
  $sources += Get-CimInstance Win32_LogicalDisk |
    Where-Object { $_.DriveType -eq 5 -and $_.VolumeName -match 'virtio' } |
    ForEach-Object { "$($_.DeviceID)\" }

  $copied = $false
  foreach ($source in ($sources | Where-Object { $_ } | Select-Object -Unique)) {
    if (-not (Test-VirtioRoot -Root $source)) { continue }
    Copy-Item -Path (Join-Path $source '*') -Destination $destination -Recurse -Force
    $copied = $true
    break
  }
  if (-not $copied -and -not (Test-VirtioRoot -Root $destination)) {
    throw "Unable to find VirtIO drivers from mounted media or staged inputs."
  }
}

if (-not (Test-VirtioRoot -Root $destination)) {
  throw "Staged VirtIO directory is missing required INF files: $destination"
}

Write-Output $destination
""";
        var output = await RunPowerShellAsync(script, TimeSpan.FromMinutes(20), cancellationToken);
        return output.Stdout
            .Split(Environment.NewLine, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .LastOrDefault()
            ?? destination;
    }

    private async Task<Dictionary<string, object?>> BuildWinPeAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var sourceRoot = SourceRoot(work);
        var outputRoot = Path.Combine(WorkRoot(work), "outputs", "winpe");
        Directory.CreateDirectory(outputRoot);
        var scriptPath = Path.Combine(sourceRoot, "tools", "winpe-build", "build-winpe.ps1");
        if (!File.Exists(scriptPath))
        {
            throw new FileNotFoundException("WinPE build script is missing.", scriptPath);
        }
        var preflight = await RunBuildHostPreflightAsync(
            work,
            "build_winpe",
            outputRoot,
            sourceMediaPath: "",
            cancellationToken);
        var virtioRoot = await StageVirtioDriversAsync(work, cancellationToken);
        var command = $"$ProgressPreference = 'SilentlyContinue'; $env:AUTOPILOT_VIRTIO_ROOT = '{PowerShellLiteral(virtioRoot)}'; & '{PowerShellLiteral(scriptPath)}' -Arch amd64 -OutputDir '{PowerShellLiteral(outputRoot)}'";
        var output = await RunPowerShellAsync(command, TimeSpan.FromHours(3), cancellationToken);
        var uploads = new List<Dictionary<string, object?>>();
        foreach (var file in Directory.EnumerateFiles(outputRoot, "*", SearchOption.TopDirectoryOnly))
        {
            var extension = Path.GetExtension(file).ToLowerInvariant();
            var kind = extension switch
            {
                ".iso" => "winpe-iso",
                ".wim" => "wim",
                ".json" => "manifest",
                _ => "",
            };
            if (kind.Length > 0)
            {
                uploads.Add(await UploadAsync(config, work, kind, file, cancellationToken));
            }
        }
        if (!uploads.Any(item => string.Equals(item.GetValueOrDefault("kind") as string, "winpe-iso", StringComparison.OrdinalIgnoreCase)))
        {
            throw new InvalidOperationException(
                "WinPE build completed without producing an ISO. "
                + $"stdout={Truncate(output.Stdout, 4000)} stderr={Truncate(output.Stderr, 4000)}");
        }
        return new Dictionary<string, object?>
        {
            ["output_root"] = outputRoot,
            ["preflight"] = preflight,
            ["uploads"] = uploads,
            ["stdout"] = Truncate(output.Stdout),
            ["stderr"] = Truncate(output.Stderr),
        };
    }

    private async Task<Dictionary<string, object?>> BuildCloudOsdAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var sourceRoot = SourceRoot(work);
        var outputRoot = Path.Combine(WorkRoot(work), "outputs", "cloudosd");
        Directory.CreateDirectory(outputRoot);
        var scriptPath = Path.Combine(sourceRoot, "autopilot-proxmox", "tools", "cloudosd-build", "build-cloudosd.ps1");
        if (!File.Exists(scriptPath))
        {
            throw new FileNotFoundException("CloudOSD build script is missing.", scriptPath);
        }
        var osdCloudVersion = ReadString(work.Request, "osdcloud_version", "26.4.17.1");
        var preflight = await RunBuildHostPreflightAsync(
            work,
            "build_cloudosd",
            outputRoot,
            sourceMediaPath: "",
            cancellationToken);
        var virtioRoot = await StageVirtioDriversAsync(work, cancellationToken);
        var command = $"$ProgressPreference = 'SilentlyContinue'; $env:AUTOPILOT_VIRTIO_ROOT = '{PowerShellLiteral(virtioRoot)}'; & '{PowerShellLiteral(scriptPath)}' -Arch amd64 -OutputDir '{PowerShellLiteral(outputRoot)}' -OSDCloudVersion '{PowerShellLiteral(osdCloudVersion)}'";
        var output = await RunPowerShellAsync(command, TimeSpan.FromHours(3), cancellationToken);
        var uploads = new List<Dictionary<string, object?>>();
        foreach (var file in Directory.EnumerateFiles(outputRoot, "*", SearchOption.TopDirectoryOnly))
        {
            var extension = Path.GetExtension(file).ToLowerInvariant();
            var kind = extension switch
            {
                ".iso" => "cloudosd-iso",
                ".wim" => "wim",
                ".json" => "manifest",
                _ => "",
            };
            if (kind.Length > 0)
            {
                uploads.Add(await UploadAsync(config, work, kind, file, cancellationToken));
            }
        }
        if (!uploads.Any(item => string.Equals(item.GetValueOrDefault("kind") as string, "cloudosd-iso", StringComparison.OrdinalIgnoreCase)))
        {
            throw new InvalidOperationException(
                "CloudOSD build completed without producing an ISO. "
                + $"stdout={Truncate(output.Stdout, 4000)} stderr={Truncate(output.Stderr, 4000)}");
        }
        return new Dictionary<string, object?>
        {
            ["output_root"] = outputRoot,
            ["preflight"] = preflight,
            ["uploads"] = uploads,
            ["stdout"] = Truncate(output.Stdout),
            ["stderr"] = Truncate(output.Stderr),
        };
    }

    private async Task<Dictionary<string, object?>> BuildOsDeployAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var sourceRoot = SourceRoot(work);
        var outputRoot = Path.Combine(WorkRoot(work), "outputs", "osdeploy");
        Directory.CreateDirectory(outputRoot);
        var scriptPath = Path.Combine(sourceRoot, "autopilot-proxmox", "tools", "osdeploy-build", "build-osdeploy.ps1");
        if (!string.IsNullOrWhiteSpace(ReadString(work.Request, "source_bundle_url", "")))
        {
            await FetchSourceBundleAsync(config, work, cancellationToken);
        }
        if (!File.Exists(scriptPath))
        {
            throw new FileNotFoundException("OSDeploy build script is missing.", scriptPath);
        }
        var osDeployVersion = ReadString(work.Request, "osdeploy_version", "26.1.30.5");
        var osdBuilderVersion = ReadString(work.Request, "osdbuilder_version", "24.10.8.1");
        var adkVersion = ReadString(work.Request, "adk_version", "10.1.26100.1");
        var sourceMediaPath = ReadString(work.Request, "source_media_path", "");
        if (string.IsNullOrWhiteSpace(sourceMediaPath))
        {
            // Operators stage the licensed base ISO once under inputs\media; resolve it
            // here so warming a server_image cache entry needs no per-build media path.
            // The factory mounts the ISO itself. The mounted-media fallback below still
            // applies when nothing is staged.
            sourceMediaPath = ResolveStagedSourceMediaIso(
                OsDeploySourceMediaDirectories(WorkRoot(work))) ?? "";
        }
        var imageName = ReadString(work.Request, "image_name", "Windows Server 2025 Datacenter");
        var imageIndex = ReadInt(work.Request, "image_index", 4);
        var osVersion = ReadString(work.Request, "os_version", "Windows Server 2025");
        var osEdition = ReadString(work.Request, "os_edition", "Datacenter");
        var osLanguage = ReadString(work.Request, "os_language", "en-us");
        var controllerUrl = ReadString(work.Request, "controller_url", config.ServerUrl ?? "").TrimEnd('/');
        var fallbackControllerUrl = ReadString(work.Request, "fallback_controller_url", "");
        var nativeMediaBuild = ReadBool(work.Request, "native_media_build", false);
        var preflight = await RunBuildHostPreflightAsync(
            work,
            "build_osdeploy",
            outputRoot,
            sourceMediaPath,
            cancellationToken);
        var command = $$"""
$ProgressPreference = 'SilentlyContinue'
$sourceMediaPath = '{{PowerShellLiteral(sourceMediaPath)}}'
if ([string]::IsNullOrWhiteSpace($sourceMediaPath)) {
  $sourceMediaPath = Get-PSDrive -PSProvider FileSystem |
    ForEach-Object { $_.Root } |
    Where-Object {
      (Test-Path -LiteralPath (Join-Path $_ 'sources\install.wim')) -or
      (Test-Path -LiteralPath (Join-Path $_ 'sources\install.esd'))
    } |
    Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($sourceMediaPath)) {
  throw 'OSDeploy source_media_path was not provided and no mounted Windows setup media was found.'
}
$osDeployArgs = @{
  Arch = 'amd64'
  OutputDir = '{{PowerShellLiteral(outputRoot)}}'
  OSDeployVersion = '{{PowerShellLiteral(osDeployVersion)}}'
  OSDBuilderVersion = '{{PowerShellLiteral(osdBuilderVersion)}}'
  ADKVersion = '{{PowerShellLiteral(adkVersion)}}'
  ImageName = '{{PowerShellLiteral(imageName)}}'
  ImageIndex = {{imageIndex}}
  OSVersion = '{{PowerShellLiteral(osVersion)}}'
  OSEdition = '{{PowerShellLiteral(osEdition)}}'
  OSLanguage = '{{PowerShellLiteral(osLanguage)}}'
  ControllerUrl = '{{PowerShellLiteral(controllerUrl)}}'
}
$fallbackControllerUrl = '{{PowerShellLiteral(fallbackControllerUrl)}}'
if (-not [string]::IsNullOrWhiteSpace($fallbackControllerUrl)) {
  $osDeployArgs.FallbackControllerUrl = $fallbackControllerUrl
}
if ({{(nativeMediaBuild ? "$true" : "$false")}}) {
  $osDeployArgs.NativeMediaBuild = $true
}
$osDeployArgs.SourceMediaPath = $sourceMediaPath
& '{{PowerShellLiteral(scriptPath)}}' @osDeployArgs
""";
        var buildStartedUtc = DateTime.UtcNow;
        var output = await RunPowerShellAsync(command, TimeSpan.FromHours(3), cancellationToken);
        var uploads = new List<Dictionary<string, object?>>();
        IReadOnlyList<string> selectedOutputs;
        try
        {
            selectedOutputs = SelectOsDeployBuildOutputs(outputRoot, output.Stdout, buildStartedUtc);
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException(
                ex.Message + " "
                + $"stdout={Truncate(output.Stdout, 4000)} stderr={Truncate(output.Stderr, 4000)}",
                ex);
        }
        foreach (var file in selectedOutputs)
        {
            var extension = Path.GetExtension(file).ToLowerInvariant();
            var kind = extension switch
            {
                ".iso" => "osdeploy-iso",
                ".wim" => "wim",
                ".json" => "manifest",
                _ => "",
            };
            if (kind.Length > 0)
            {
                uploads.Add(await UploadAsync(config, work, kind, file, cancellationToken));
            }
        }
        if (!uploads.Any(item => string.Equals(item.GetValueOrDefault("kind") as string, "osdeploy-iso", StringComparison.OrdinalIgnoreCase)))
        {
            throw new InvalidOperationException(
                "OSDeploy build completed without producing an ISO. "
                + $"stdout={Truncate(output.Stdout, 4000)} stderr={Truncate(output.Stderr, 4000)}");
        }
        return new Dictionary<string, object?>
        {
            ["output_root"] = outputRoot,
            ["preflight"] = preflight,
            ["uploads"] = uploads,
            ["stdout"] = Truncate(output.Stdout),
            ["stderr"] = Truncate(output.Stderr),
        };
    }

    private async Task<Dictionary<string, object?>> RunBuildHostPreflightAsync(
        AgentWorkItem work,
        string workload,
        string outputRoot,
        string sourceMediaPath,
        CancellationToken cancellationToken)
    {
        var workRoot = WorkRoot(work);
        var script = $$"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$checks = @()

function Add-Check {
  param(
    [Parameter(Mandatory)][string] $Name,
    [Parameter(Mandatory)][bool] $Ok,
    [string] $Detail = ''
  )
  $script:checks += [pscustomobject]@{
    name = $Name
    ok = $Ok
    detail = $Detail
  }
}

function Test-WritableDirectory {
  param([Parameter(Mandatory)][string] $Path)
  try {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    $probe = Join-Path $Path ".preflight-$([guid]::NewGuid().ToString('N')).tmp"
    Set-Content -LiteralPath $probe -Value 'ok' -Encoding ASCII
    Remove-Item -LiteralPath $probe -Force
    return $true
  } catch {
    return $false
  }
}

function Test-VirtioInput {
  $requiredInf = @('vioscsi.inf','netkvm.inf','vioser.inf')
  $roots = @()
  if ($env:AUTOPILOT_VIRTIO_ROOT) { $roots += $env:AUTOPILOT_VIRTIO_ROOT }
  $roots += @(
    'C:\BuildRoot\ProxmoxVEAutopilot\inputs\virtio-win',
    'C:\BuildRoot\ProxmoxVEAutopilot\inputs\virtio',
    'C:\BuildRoot\inputs\virtio-win',
    'C:\BuildRoot\inputs\virtio',
    'E:\BuildRoot\inputs\virtio-win',
    'E:\BuildRoot\inputs\virtio',
    'E:\',
    'D:\virtio',
    'D:\',
    'F:\BuildRoot\inputs\virtio-win',
    'F:\BuildRoot\inputs\virtio',
    'F:\'
  )
  foreach ($root in ($roots | Where-Object { $_ } | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    $missing = $false
    foreach ($inf in $requiredInf) {
      if (-not (Get-ChildItem -LiteralPath $root -Recurse -Filter $inf -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        $missing = $true
        break
      }
    }
    if (-not $missing) { return $root }
  }
  return ''
}

function Resolve-SourceMedia {
  param([string] $ConfiguredPath)
  if (-not [string]::IsNullOrWhiteSpace($ConfiguredPath) -and (Test-Path -LiteralPath $ConfiguredPath)) {
    return $ConfiguredPath
  }
  $mounted = Get-PSDrive -PSProvider FileSystem |
    ForEach-Object { $_.Root } |
    Where-Object {
      (Test-Path -LiteralPath (Join-Path $_ 'sources\install.wim')) -or
      (Test-Path -LiteralPath (Join-Path $_ 'sources\install.esd'))
    } |
    Select-Object -First 1
  if ($mounted) { return $mounted }
  return ''
}

$kitsRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\Assessment and Deployment Kit"
$oscdimg = Join-Path $kitsRoot 'Deployment Tools\amd64\Oscdimg\oscdimg.exe'
$copype = Join-Path $kitsRoot 'Windows Preinstallation Environment\copype.cmd'
$winpeWim = Join-Path $kitsRoot 'Windows Preinstallation Environment\amd64\en-us\winpe.wim'

Add-Check -Name 'ADK Deployment Tools' -Ok (Test-Path -LiteralPath $oscdimg) -Detail $oscdimg
Add-Check -Name 'WinPE add-on' -Ok ((Test-Path -LiteralPath $copype) -and (Test-Path -LiteralPath $winpeWim)) -Detail "$copype | $winpeWim"
Add-Check -Name 'oscdimg.exe' -Ok (Test-Path -LiteralPath $oscdimg) -Detail $oscdimg
Add-Check -Name 'copype.cmd' -Ok (Test-Path -LiteralPath $copype) -Detail $copype
Add-Check -Name 'PowerShell/module availability' -Ok ($PSVersionTable.PSVersion.Major -ge 5) -Detail $PSVersionTable.PSVersion.ToString()
Add-Check -Name 'writable work root' -Ok (Test-WritableDirectory -Path '{{PowerShellLiteral(workRoot)}}') -Detail '{{PowerShellLiteral(workRoot)}}'
Add-Check -Name 'writable output root' -Ok (Test-WritableDirectory -Path '{{PowerShellLiteral(outputRoot)}}') -Detail '{{PowerShellLiteral(outputRoot)}}'
$virtioRoot = Test-VirtioInput
Add-Check -Name 'VirtIO input' -Ok (-not [string]::IsNullOrWhiteSpace($virtioRoot)) -Detail $virtioRoot

$sourceMedia = Resolve-SourceMedia -ConfiguredPath '{{PowerShellLiteral(sourceMediaPath)}}'
if ('{{PowerShellLiteral(workload)}}' -eq 'build_osdeploy') {
  Add-Check -Name 'source media' -Ok (-not [string]::IsNullOrWhiteSpace($sourceMedia)) -Detail $sourceMedia
}

$blocking = @($checks | Where-Object { -not $_.ok } | ForEach-Object { $_.name })
[pscustomobject]@{
  event_type = 'osdeploy_build_host_preflight'
  workload = '{{PowerShellLiteral(workload)}}'
  ok = ($blocking.Count -eq 0)
  checks = $checks
  blocking_checks = $blocking
} | ConvertTo-Json -Depth 6 -Compress
""";
        var output = await RunPowerShellAsync(script, TimeSpan.FromMinutes(5), cancellationToken);
        var json = output.Stdout
            .Split(Environment.NewLine, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .LastOrDefault(line => line.StartsWith("{", StringComparison.Ordinal))
            ?? "{}";
        var parsed = JsonSerializer.Deserialize<Dictionary<string, object?>>(
            json,
            AgentConfig.JsonOptions()) ?? new Dictionary<string, object?>();
        parsed["stdout"] = Truncate(output.Stdout);
        parsed["stderr"] = Truncate(output.Stderr);
        return parsed;
    }

    internal static IReadOnlyList<string> OsDeploySourceMediaDirectories(string workRoot) =>
    [
        Path.Combine(workRoot, "inputs", "media"),
        @"C:\BuildRoot\ProxmoxVEAutopilot\inputs\media",
        @"C:\BuildRoot\inputs\media",
    ];

    internal static string? ResolveStagedSourceMediaIso(IEnumerable<string> searchDirectories)
    {
        string? newest = null;
        var newestWriteUtc = DateTime.MinValue;
        foreach (var directory in searchDirectories)
        {
            if (string.IsNullOrWhiteSpace(directory) || !Directory.Exists(directory))
            {
                continue;
            }
            foreach (var iso in Directory.EnumerateFiles(directory, "*.iso", SearchOption.TopDirectoryOnly))
            {
                var writeUtc = File.GetLastWriteTimeUtc(iso);
                if (newest is null || writeUtc > newestWriteUtc)
                {
                    newest = iso;
                    newestWriteUtc = writeUtc;
                }
            }
        }
        return newest;
    }

    internal static IReadOnlyList<string> SelectOsDeployBuildOutputs(
        string outputRoot,
        string stdout,
        DateTime buildStartedUtc)
    {
        var manifestPath = SelectPrintedOsDeployManifest(stdout);
        if (string.IsNullOrWhiteSpace(manifestPath))
        {
            manifestPath = Directory
                .EnumerateFiles(outputRoot, "osdeploy-server-*.json", SearchOption.TopDirectoryOnly)
                .Where(path => File.GetLastWriteTimeUtc(path) >= buildStartedUtc.AddSeconds(-5))
                .OrderByDescending(File.GetLastWriteTimeUtc)
                .FirstOrDefault();
        }
        if (string.IsNullOrWhiteSpace(manifestPath))
        {
            throw new InvalidOperationException(
                $"OSDeploy build completed without producing a current manifest in {outputRoot}.");
        }

        using var manifest = JsonDocument.Parse(File.ReadAllText(manifestPath));
        var root = manifest.RootElement;
        var paths = new List<string> { manifestPath };
        AddManifestPath(root, paths, "output_wim");
        AddManifestPath(root, paths, "output_iso");
        return paths
            .Where(path => !string.IsNullOrWhiteSpace(path))
            .Select(Path.GetFullPath)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();
    }

    private static string? SelectPrintedOsDeployManifest(string stdout)
    {
        foreach (var rawLine in stdout.Split(
            ['\r', '\n'],
            StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            var line = rawLine.Trim('"');
            if (!line.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                || !Path.GetFileName(line).StartsWith("osdeploy-server-", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            if (File.Exists(line))
            {
                return line;
            }
        }
        return null;
    }

    private static void AddManifestPath(
        JsonElement root,
        List<string> paths,
        string propertyName)
    {
        if (!root.TryGetProperty(propertyName, out var value)
            || value.ValueKind != JsonValueKind.String)
        {
            throw new InvalidOperationException($"OSDeploy manifest is missing {propertyName}.");
        }
        var path = value.GetString();
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            throw new FileNotFoundException($"OSDeploy manifest output is missing: {propertyName}", path);
        }
        paths.Add(path);
    }

    private static int ReadInt(
        IReadOnlyDictionary<string, JsonElement> values,
        string key,
        int defaultValue)
    {
        if (!values.TryGetValue(key, out var value))
        {
            return defaultValue;
        }
        if (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var number))
        {
            return number;
        }
        if (value.ValueKind == JsonValueKind.String && int.TryParse(value.GetString(), out number))
        {
            return number;
        }
        return defaultValue;
    }

    private async Task<Dictionary<string, object?>> PublishArtifactsAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        var response = await apiClient.PromoteSetupArtifactsAsync(config, cancellationToken);
        return new Dictionary<string, object?>
        {
            ["controller_promotion"] = JsonSerializer.Deserialize<Dictionary<string, object?>>(
                response.GetRawText()),
            ["work_root"] = WorkRoot(work),
        };
    }

    private async Task<Dictionary<string, object?>> UploadAsync(
        AgentConfig config,
        AgentWorkItem work,
        string kind,
        string path,
        CancellationToken cancellationToken)
    {
        var metadata = new Dictionary<string, object?>
        {
            ["kind"] = kind,
            ["sha256"] = Sha256(path),
            ["size_bytes"] = new FileInfo(path).Length,
            ["source_manifest"] = ReadObject(work.Request, "source_manifest"),
            ["rid_arch"] = RuntimeInformation(),
            ["producer_agent_id"] = config.AgentId,
        };
        var uploaded = await apiClient.UploadArtifactAsync(
            config,
            work.Id,
            kind,
            path,
            metadata,
            cancellationToken);
        return new Dictionary<string, object?>
        {
            ["kind"] = kind,
            ["path"] = path,
            ["sha256"] = metadata["sha256"],
            ["artifact"] = uploaded.Artifact,
        };
    }

    private static Dictionary<string, object?> RuntimeInformation() => new()
    {
        ["machine_name"] = Environment.MachineName,
        ["os_version"] = Environment.OSVersion.ToString(),
        ["process_architecture"] = System.Runtime.InteropServices.RuntimeInformation.ProcessArchitecture.ToString(),
        ["os_architecture"] = System.Runtime.InteropServices.RuntimeInformation.OSArchitecture.ToString(),
    };

    private static string WorkRoot(AgentWorkItem work) =>
        ReadString(work.Request, "work_root", @"C:\BuildRoot\ProxmoxVEAutopilot");

    private static string SourceRoot(AgentWorkItem work) =>
        Path.Combine(WorkRoot(work), "source");

    private static string ReadString(
        IReadOnlyDictionary<string, JsonElement> values,
        string key,
        string defaultValue = "")
    {
        if (!values.TryGetValue(key, out var value))
        {
            return defaultValue;
        }
        return value.ValueKind switch
        {
            JsonValueKind.String => value.GetString() ?? defaultValue,
            JsonValueKind.Number => value.ToString(),
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            _ => defaultValue,
        };
    }

    private static bool ReadBool(
        IReadOnlyDictionary<string, JsonElement> values,
        string key,
        bool defaultValue)
    {
        if (!values.TryGetValue(key, out var value))
        {
            return defaultValue;
        }
        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            JsonValueKind.String when bool.TryParse(value.GetString(), out var parsed) => parsed,
            _ => defaultValue,
        };
    }

    private static string[] ReadStringArray(
        IReadOnlyDictionary<string, JsonElement> values,
        string key,
        string[] defaultValue)
    {
        if (!values.TryGetValue(key, out var value) || value.ValueKind != JsonValueKind.Array)
        {
            return defaultValue;
        }
        var items = value.EnumerateArray()
            .Select(item => item.GetString())
            .Where(item => !string.IsNullOrWhiteSpace(item))
            .Cast<string>()
            .ToArray();
        return items.Length == 0 ? defaultValue : items;
    }

    private static object? ReadObject(
        IReadOnlyDictionary<string, JsonElement> values,
        string key)
    {
        if (!values.TryGetValue(key, out var value))
        {
            return null;
        }
        return JsonSerializer.Deserialize<object?>(value.GetRawText());
    }

    private static string PowerShellLiteral(string value) =>
        value.Replace("'", "''");

    private static string Sha256(string path)
    {
        using var stream = File.OpenRead(path);
        var hash = SHA256.HashData(stream);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    private static string Truncate(string value, int maxChars = 16000)
    {
        if (value.Length <= maxChars)
        {
            return value;
        }
        return value[..maxChars] + "\n...[truncated]";
    }

    private async Task<ProcessOutput> RunPowerShellAsync(
        string script,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        var scriptPath = Path.Combine(
            Path.GetTempPath(),
            $"autopilot-buildhost-{Guid.NewGuid():N}.ps1");
        await File.WriteAllTextAsync(scriptPath, script, cancellationToken);
        try
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            startInfo.ArgumentList.Add("-NoProfile");
            startInfo.ArgumentList.Add("-ExecutionPolicy");
            startInfo.ArgumentList.Add("Bypass");
            startInfo.ArgumentList.Add("-File");
            startInfo.ArgumentList.Add(scriptPath);
            using var process = Process.Start(startInfo)
                ?? throw new InvalidOperationException("Failed to start powershell.exe.");
            using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeoutCts.CancelAfter(timeout);
            var stdoutTask = process.StandardOutput.ReadToEndAsync(timeoutCts.Token);
            var stderrTask = process.StandardError.ReadToEndAsync(timeoutCts.Token);
            await process.WaitForExitAsync(timeoutCts.Token);
            var output = new ProcessOutput(
                await stdoutTask,
                await stderrTask,
                process.ExitCode);
            if (output.ExitCode != 0)
            {
                throw new InvalidOperationException(
                    $"PowerShell failed with exit {output.ExitCode}: {output.Stderr} {output.Stdout}".Trim());
            }
            log.Info($"Build-host PowerShell step completed: {Path.GetFileName(scriptPath)}");
            return output;
        }
        finally
        {
            try
            {
                File.Delete(scriptPath);
            }
            catch
            {
                // Best effort cleanup.
            }
        }
    }

    private sealed record ProcessOutput(string Stdout, string Stderr, int ExitCode);
}
