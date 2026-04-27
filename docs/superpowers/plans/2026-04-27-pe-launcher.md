# PE Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a .NET 8 console app (launcher.exe) that replaces the cmd.exe/batch bootstrap chain in WinPE, owning the entire lifecycle from wpeinit through reboot with a structured status display.

**Architecture:** Single .NET 8 framework-dependent console app at `X:\autopilot\launcher.exe`. Handles networking (manifest, content download with progress, checkins) in C#; shells out to pwsh for Windows-native steps (partition, DISM, registry, bcdboot). Reads `Bootstrap.json` for config. Built on the Windows ARM64 host as part of the WIM pipeline.

**Tech Stack:** .NET 8 (net8.0), C#, System.Management (WMI), HttpClient, System.Text.Json

---

## File Structure

```
build/launcher/
├── Launcher.csproj           # .NET 8 console project targeting win-arm64
├── Program.cs                # Entry point — phase orchestration (0–8)
├── Display.cs                # Console UI: box drawing, progress bars, step list
├── Guard.cs                  # Volume scan for existing Windows install
├── WpeInit.cs                # wpeinit process + network wait (ipconfig parsing)
├── Identity.cs               # SMBIOS identity via WMI
├── Orchestrator.cs           # HTTP: manifest fetch, content download, checkins
├── StepRunner.cs             # Step dispatch loop — routes to C# or pwsh
├── PwshInvoker.cs            # Invoke pwsh.exe, capture stdout/stderr/exit code
├── Models/
│   ├── BootstrapConfig.cs    # Bootstrap.json deserialization
│   ├── Manifest.cs           # Manifest + Step + ContentRef JSON models
│   └── CheckinPayload.cs     # Checkin POST body
└── Tests/
    └── build/launcher-tests/
        ├── LauncherTests.csproj
        ├── ManifestTests.cs      # JSON round-trip tests
        ├── DisplayTests.cs       # Rendering output tests
        └── WpeInitTests.cs       # ipconfig parsing tests
```

**Modified files:**
- `build/Build-PeWim.ps1` — add launcher build phase, write winpeshl.ini, remove startnet.cmd override
- `tools/build-pe-wim.sh` — push launcher source to remote alongside payload

---

### Task 1: Project Scaffold + JSON Models

**Files:**
- Create: `build/launcher/Launcher.csproj`
- Create: `build/launcher/Models/BootstrapConfig.cs`
- Create: `build/launcher/Models/Manifest.cs`
- Create: `build/launcher/Models/CheckinPayload.cs`
- Create: `build/launcher/Program.cs`
- Create: `build/launcher-tests/LauncherTests.csproj`
- Create: `build/launcher-tests/ManifestTests.cs`

- [ ] **Step 1: Create the project file**

```xml
<!-- build/launcher/Launcher.csproj -->
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <RuntimeIdentifier>win-arm64</RuntimeIdentifier>
    <SelfContained>false</SelfContained>
    <RootNamespace>Autopilot.Launcher</RootNamespace>
    <AssemblyName>launcher</AssemblyName>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="System.Management" Version="8.0.0" />
  </ItemGroup>
</Project>
```

- [ ] **Step 2: Create BootstrapConfig model**

```csharp
// build/launcher/Models/BootstrapConfig.cs
using System.Text.Json.Serialization;

namespace Autopilot.Launcher.Models;

public sealed class BootstrapConfig
{
    [JsonPropertyName("version")]
    public int Version { get; set; } = 1;

    [JsonPropertyName("orchestratorUrl")]
    public string OrchestratorUrl { get; set; } = "";

    [JsonPropertyName("networkTimeoutSec")]
    public int NetworkTimeoutSec { get; set; } = 60;

    [JsonPropertyName("manifestRetries")]
    public int ManifestRetries { get; set; } = 3;

    [JsonPropertyName("manifestRetryBackoffSec")]
    public int ManifestRetryBackoffSec { get; set; } = 5;

    [JsonPropertyName("checkinRetries")]
    public int CheckinRetries { get; set; } = 2;

    [JsonPropertyName("debug")]
    public bool Debug { get; set; }
}
```

- [ ] **Step 3: Create Manifest models**

```csharp
// build/launcher/Models/Manifest.cs
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Autopilot.Launcher.Models;

public sealed class Manifest
{
    [JsonPropertyName("version")]
    public int Version { get; set; }

    [JsonPropertyName("vmUuid")]
    public string VmUuid { get; set; } = "";

    [JsonPropertyName("onError")]
    public string OnError { get; set; } = "halt";

    [JsonPropertyName("steps")]
    public List<Step> Steps { get; set; } = [];
}

public sealed class Step
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("type")]
    public string Type { get; set; } = "";

    [JsonPropertyName("content")]
    public ContentRef? Content { get; set; }

    [JsonPropertyName("target")]
    public string? Target { get; set; }

    [JsonPropertyName("index")]
    public int? Index { get; set; }

    [JsonPropertyName("layout")]
    public string? Layout { get; set; }

    [JsonPropertyName("hive")]
    public string? Hive { get; set; }

    [JsonPropertyName("keys")]
    public JsonElement? Keys { get; set; }

    [JsonPropertyName("windows")]
    public string? Windows { get; set; }

    [JsonPropertyName("esp")]
    public string? Esp { get; set; }

    [JsonPropertyName("message")]
    public string? Message { get; set; }

    [JsonPropertyName("name")]
    public string? Name { get; set; }

    [JsonPropertyName("taskXml")]
    public string? TaskXml { get; set; }
}

public sealed class ContentRef
{
    [JsonPropertyName("sha256")]
    public string Sha256 { get; set; } = "";

    [JsonPropertyName("size")]
    public long Size { get; set; }
}
```

- [ ] **Step 4: Create CheckinPayload model**

```csharp
// build/launcher/Models/CheckinPayload.cs
using System.Text.Json.Serialization;

namespace Autopilot.Launcher.Models;

public sealed class CheckinPayload
{
    [JsonPropertyName("vmUuid")]
    public string VmUuid { get; set; } = "";

    [JsonPropertyName("stepId")]
    public string StepId { get; set; } = "";

    [JsonPropertyName("status")]
    public string Status { get; set; } = "";

    [JsonPropertyName("timestamp")]
    public string Timestamp { get; set; } = "";

    [JsonPropertyName("durationSec")]
    public double DurationSec { get; set; }

    [JsonPropertyName("logTail")]
    public string LogTail { get; set; } = "";

    [JsonPropertyName("errorMessage")]
    public string? ErrorMessage { get; set; }

    [JsonPropertyName("extra")]
    public Dictionary<string, object> Extra { get; set; } = [];
}
```

- [ ] **Step 5: Create stub Program.cs**

```csharp
// build/launcher/Program.cs
using System.Text.Json;
using Autopilot.Launcher.Models;

namespace Autopilot.Launcher;

public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        var configPath = @"X:\autopilot\Bootstrap.json";
        if (args.Length > 0) configPath = args[0];

        if (!File.Exists(configPath))
        {
            Console.Error.WriteLine($"Config not found: {configPath}");
            return 1;
        }

        var json = await File.ReadAllTextAsync(configPath);
        var config = JsonSerializer.Deserialize<BootstrapConfig>(json)!;

        Console.WriteLine($"Autopilot PE Launcher — orchestrator: {config.OrchestratorUrl}");
        return 0;
    }
}
```

- [ ] **Step 6: Create test project and manifest deserialization test**

```xml
<!-- build/launcher-tests/LauncherTests.csproj -->
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <IsPackable>false</IsPackable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.9.0" />
    <PackageReference Include="xunit" Version="2.7.0" />
    <PackageReference Include="xunit.runner.visualstudio" Version="2.5.7" />
  </ItemGroup>
  <ItemGroup>
    <ProjectReference Include="../launcher/Launcher.csproj" />
  </ItemGroup>
</Project>
```

```csharp
// build/launcher-tests/ManifestTests.cs
using System.Text.Json;
using Autopilot.Launcher.Models;

namespace Autopilot.Launcher.Tests;

public class ManifestTests
{
    private const string SampleManifest = """
    {
      "version": 1,
      "vmUuid": "9A0EB631-7299-4504-B45A-29C5F75BCF56",
      "onError": "halt",
      "steps": [
        {"id": "p1", "type": "partition", "layout": "uefi-standard"},
        {"id": "a1", "type": "apply-wim", "content": {"sha256": "abc123", "size": 6500000000}},
        {"id": "u1", "type": "write-unattend", "content": {"sha256": "def456", "size": 963}, "target": "W:\\Windows\\Panther\\unattend.xml"},
        {"id": "r1", "type": "set-registry", "hive": "SYSTEM", "target": "W:", "keys": [{"path": "Setup", "name": "ComputerName", "type": "REG_SZ", "value": "TEST-01"}]},
        {"id": "b1", "type": "bcdboot", "windows": "W:", "esp": "S:"},
        {"id": "rb", "type": "reboot"}
      ]
    }
    """;

    [Fact]
    public void Deserialize_FullManifest_AllFieldsParsed()
    {
        var m = JsonSerializer.Deserialize<Manifest>(SampleManifest)!;
        Assert.Equal("9A0EB631-7299-4504-B45A-29C5F75BCF56", m.VmUuid);
        Assert.Equal("halt", m.OnError);
        Assert.Equal(6, m.Steps.Count);
    }

    [Fact]
    public void Deserialize_ApplyWimStep_ContentRefParsed()
    {
        var m = JsonSerializer.Deserialize<Manifest>(SampleManifest)!;
        var step = m.Steps[1];
        Assert.Equal("apply-wim", step.Type);
        Assert.NotNull(step.Content);
        Assert.Equal("abc123", step.Content!.Sha256);
        Assert.Equal(6500000000L, step.Content.Size);
    }

    [Fact]
    public void Deserialize_BootstrapConfig_DefaultValues()
    {
        var json = """{"orchestratorUrl": "http://test:5050"}""";
        var cfg = JsonSerializer.Deserialize<BootstrapConfig>(json)!;
        Assert.Equal("http://test:5050", cfg.OrchestratorUrl);
        Assert.Equal(60, cfg.NetworkTimeoutSec);
        Assert.Equal(3, cfg.ManifestRetries);
    }
}
```

- [ ] **Step 7: Run tests**

Run: `cd build/launcher-tests && dotnet test -v normal`
Expected: 3 tests PASS

- [ ] **Step 8: Commit**

```bash
git add build/launcher/ build/launcher-tests/
git commit -m "feat(launcher): project scaffold + JSON models with tests"
```

---

### Task 2: Display Rendering

**Files:**
- Create: `build/launcher/Display.cs`
- Create: `build/launcher-tests/DisplayTests.cs`

- [ ] **Step 1: Write display rendering tests**

```csharp
// build/launcher-tests/DisplayTests.cs
using Autopilot.Launcher;

namespace Autopilot.Launcher.Tests;

public class DisplayTests
{
    [Fact]
    public void FormatDuration_UnderMinute_ShowsSeconds()
    {
        Assert.Equal("7s", Display.FormatDuration(TimeSpan.FromSeconds(7)));
    }

    [Fact]
    public void FormatDuration_OverMinute_ShowsMinSec()
    {
        Assert.Equal("2:37", Display.FormatDuration(TimeSpan.FromSeconds(157)));
    }

    [Fact]
    public void FormatBytes_Gigabytes()
    {
        Assert.Equal("6.5 GB", Display.FormatBytes(6949212989));
    }

    [Fact]
    public void FormatBytes_Megabytes()
    {
        Assert.Equal("963 B", Display.FormatBytes(963));
    }

    [Fact]
    public void ProgressBar_HalfFull()
    {
        var bar = Display.ProgressBar(50, 20);
        Assert.Equal("██████████░░░░░░░░░░", bar);
    }

    [Fact]
    public void ProgressBar_Full()
    {
        var bar = Display.ProgressBar(100, 10);
        Assert.Equal("██████████", bar);
    }

    [Fact]
    public void ProgressBar_Empty()
    {
        var bar = Display.ProgressBar(0, 10);
        Assert.Equal("░░░░░░░░░░", bar);
    }

    [Fact]
    public void StepIcon_AllStates()
    {
        Assert.Equal("[ ]", Display.StepIcon(StepState.Pending));
        Assert.Equal("[▸]", Display.StepIcon(StepState.Active));
        Assert.Equal("[✓]", Display.StepIcon(StepState.Done));
        Assert.Equal("[✗]", Display.StepIcon(StepState.Error));
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd build/launcher-tests && dotnet test -v normal`
Expected: FAIL — Display class not found

- [ ] **Step 3: Implement Display.cs**

```csharp
// build/launcher/Display.cs
using Autopilot.Launcher.Models;

namespace Autopilot.Launcher;

public enum StepState { Pending, Active, Done, Error }

public static class Display
{
    private const int Width = 64;

    public static string FormatDuration(TimeSpan ts)
    {
        if (ts.TotalMinutes >= 1)
            return $"{(int)ts.TotalMinutes}:{ts.Seconds:D2}";
        return $"{(int)ts.TotalSeconds}s";
    }

    public static string FormatBytes(long bytes)
    {
        if (bytes >= 1_000_000_000) return $"{bytes / 1_000_000_000.0:F1} GB";
        if (bytes >= 1_000_000) return $"{bytes / 1_000_000.0:F1} MB";
        if (bytes >= 1_000) return $"{bytes / 1_000.0:F1} KB";
        return $"{bytes} B";
    }

    public static string ProgressBar(int percent, int width)
    {
        var filled = (int)(percent / 100.0 * width);
        return new string('█', filled) + new string('░', width - filled);
    }

    public static string StepIcon(StepState state) => state switch
    {
        StepState.Pending => "[ ]",
        StepState.Active  => "[▸]",
        StepState.Done    => "[✓]",
        StepState.Error   => "[✗]",
        _ => "[ ]",
    };

    public static string StepTypeName(string type) => type switch
    {
        "partition"     => "Partition",
        "apply-wim"     => "Apply WIM",
        "write-unattend"=> "Write unattend",
        "set-registry"  => "Set registry",
        "bcdboot"       => "Configure boot",
        "stage-files"   => "Stage files",
        "inject-driver" => "Inject drivers",
        "schedule-task" => "Schedule task",
        "reboot"        => "Reboot",
        "shutdown"       => "Shutdown",
        "log"           => "Log",
        _ => type,
    };

    private static void Line(string text)
    {
        var padded = text.PadRight(Width);
        Console.WriteLine($"║  {padded}║");
    }

    private static void Separator()
    {
        Console.WriteLine($"╠{"".PadRight(Width + 2, '═')}╣");
    }

    private static void TopBorder()
    {
        Console.WriteLine($"╔{"".PadRight(Width + 2, '═')}╗");
    }

    private static void BottomBorder()
    {
        Console.WriteLine($"╚{"".PadRight(Width + 2, '═')}╝");
    }

    public static void RenderFull(
        string? uuid, string? vendor, string? model,
        string? ip, string? hostname, string? server,
        List<Step> steps, StepState[] states,
        TimeSpan[] elapsed, int? downloadPercent,
        long? downloadedBytes, string statusMessage)
    {
        Console.SetCursorPosition(0, 0);
        TopBorder();
        Line("Autopilot PE Bootstrap");
        Separator();
        Line($"UUID:    {uuid ?? "..."}");
        Line($"Vendor:  {vendor ?? "..."} │ Model: {model ?? "..."}");
        Line($"IP:      {ip ?? "..."} │ Host: {hostname ?? "..."}");
        Line($"Server:  {server ?? "..."}");
        Separator();

        for (var i = 0; i < steps.Count; i++)
        {
            var icon = StepIcon(states[i]);
            var name = StepTypeName(steps[i].Type);
            var dur = states[i] is StepState.Done or StepState.Active
                ? FormatDuration(elapsed[i]).PadLeft(6)
                : "      ";

            var detail = "";
            if (states[i] == StepState.Active && downloadPercent.HasValue)
            {
                var bar = ProgressBar(downloadPercent.Value, 18);
                var pct = $"{downloadPercent.Value}%".PadLeft(4);
                var bytes = downloadedBytes.HasValue ? FormatBytes(downloadedBytes.Value) : "";
                detail = $" {bar} {pct} {bytes}";
            }

            Line($"{icon} {name,-18}{dur}{detail}");
        }

        Separator();
        Line(statusMessage);
        BottomBorder();
    }

    public static void ShowGuardShutdown(string driveLetter)
    {
        Console.Clear();
        Console.WriteLine();
        Console.WriteLine($"  Windows already installed on {driveLetter}");
        Console.WriteLine("  Shutting down PE...");
        Console.WriteLine();
    }

    public static void ShowError(string message)
    {
        Console.ForegroundColor = ConsoleColor.Red;
        Console.Error.WriteLine($"  ERROR: {message}");
        Console.ResetColor();
    }

    public static void ShowPhase(string message)
    {
        Console.WriteLine($"  {message}");
    }
}
```

- [ ] **Step 4: Run tests**

Run: `cd build/launcher-tests && dotnet test -v normal`
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add build/launcher/Display.cs build/launcher-tests/DisplayTests.cs
git commit -m "feat(launcher): display rendering with box drawing + progress bars"
```

---

### Task 3: Guard (Volume Scan)

**Files:**
- Create: `build/launcher/Guard.cs`

- [ ] **Step 1: Implement Guard.cs**

```csharp
// build/launcher/Guard.cs
using System.Diagnostics;
using System.Management;

namespace Autopilot.Launcher;

public static class Guard
{
    public static string? FindExistingWindows()
    {
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT DriveLetter, Label, DriveType FROM Win32_Volume WHERE Label = 'Windows' AND DriveType = 3");
            foreach (ManagementObject vol in searcher.Get())
            {
                var letter = vol["DriveLetter"]?.ToString();
                if (string.IsNullOrEmpty(letter))
                {
                    // Volume has no drive letter — try to assign W:
                    vol["DriveLetter"] = "W:";
                    try { vol.Put(); letter = "W:"; } catch { continue; }
                }
                var ntoskrnl = Path.Combine(letter, "Windows", "System32", "ntoskrnl.exe");
                if (File.Exists(ntoskrnl))
                    return letter;
            }
        }
        catch
        {
            // WMI unavailable — skip guard
        }
        return null;
    }

    public static void Shutdown()
    {
        Process.Start(new ProcessStartInfo
        {
            FileName = "wpeutil",
            Arguments = "shutdown",
            UseShellExecute = false,
        })?.WaitForExit(30_000);
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add build/launcher/Guard.cs
git commit -m "feat(launcher): guard — scan volumes for existing Windows install"
```

---

### Task 4: WpeInit + Network Wait

**Files:**
- Create: `build/launcher/WpeInit.cs`
- Create: `build/launcher-tests/WpeInitTests.cs`

- [ ] **Step 1: Write ipconfig parsing tests**

```csharp
// build/launcher-tests/WpeInitTests.cs
using Autopilot.Launcher;

namespace Autopilot.Launcher.Tests;

public class WpeInitTests
{
    [Fact]
    public void ParseIpConfig_ValidIp_Found()
    {
        var output = """
        Windows IP Configuration
        
        Ethernet adapter Ethernet:
           IPv4 Address. . . . . . . . . . . : 192.168.64.20
           Subnet Mask . . . . . . . . . . . : 255.255.255.0
        """;
        var ip = WpeInit.ParseFirstNonApipaIp(output);
        Assert.Equal("192.168.64.20", ip);
    }

    [Fact]
    public void ParseIpConfig_ApipaOnly_ReturnsNull()
    {
        var output = """
        Ethernet adapter Ethernet:
           IPv4 Address. . . . . . . . . . . : 169.254.12.34
        """;
        Assert.Null(WpeInit.ParseFirstNonApipaIp(output));
    }

    [Fact]
    public void ParseIpConfig_NoIp_ReturnsNull()
    {
        var output = "Windows IP Configuration\n\n";
        Assert.Null(WpeInit.ParseFirstNonApipaIp(output));
    }

    [Fact]
    public void ParseIpConfig_Localhost_Ignored()
    {
        var output = """
        Ethernet adapter Loopback:
           IPv4 Address. . . . . . . . . . . : 127.0.0.1
        """;
        Assert.Null(WpeInit.ParseFirstNonApipaIp(output));
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd build/launcher-tests && dotnet test -v normal`
Expected: FAIL — WpeInit class not found

- [ ] **Step 3: Implement WpeInit.cs**

```csharp
// build/launcher/WpeInit.cs
using System.Diagnostics;
using System.Text.RegularExpressions;

namespace Autopilot.Launcher;

public static partial class WpeInit
{
    [GeneratedRegex(@"IPv4 Address.*?:\s*([\d\.]+)")]
    private static partial Regex Ipv4Regex();

    public static string? ParseFirstNonApipaIp(string ipconfigOutput)
    {
        foreach (Match m in Ipv4Regex().Matches(ipconfigOutput))
        {
            var ip = m.Groups[1].Value;
            if (ip.StartsWith("169.254.") || ip == "127.0.0.1")
                continue;
            return ip;
        }
        return null;
    }

    public static void RunWpeInit(Action<string> onStatus)
    {
        onStatus("Running wpeinit...");
        var p = Process.Start(new ProcessStartInfo
        {
            FileName = "wpeinit",
            UseShellExecute = false,
            RedirectStandardOutput = true,
        });
        p?.WaitForExit();
        onStatus("wpeinit complete.");
    }

    public static string WaitForNetwork(int timeoutSeconds, Action<string> onStatus)
    {
        var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        while (DateTime.UtcNow < deadline)
        {
            onStatus("Initializing network...");
            RunProcess("wpeutil", "InitializeNetwork");

            var output = CaptureProcess("ipconfig");
            var ip = ParseFirstNonApipaIp(output);
            if (ip != null)
                return ip;

            Thread.Sleep(3000);
        }
        throw new TimeoutException($"No non-APIPA IPv4 address after {timeoutSeconds}s");
    }

    private static void RunProcess(string fileName, string args)
    {
        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = fileName,
                Arguments = args,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            })?.WaitForExit(15_000);
        }
        catch { }
    }

    private static string CaptureProcess(string fileName)
    {
        try
        {
            var p = Process.Start(new ProcessStartInfo
            {
                FileName = fileName,
                UseShellExecute = false,
                RedirectStandardOutput = true,
            });
            if (p == null) return "";
            var output = p.StandardOutput.ReadToEnd();
            p.WaitForExit(10_000);
            return output;
        }
        catch { return ""; }
    }
}
```

- [ ] **Step 4: Run tests**

Run: `cd build/launcher-tests && dotnet test -v normal`
Expected: 4 tests PASS (ipconfig parsing), 11 total PASS

- [ ] **Step 5: Commit**

```bash
git add build/launcher/WpeInit.cs build/launcher-tests/WpeInitTests.cs
git commit -m "feat(launcher): wpeinit + network wait with ipconfig parsing"
```

---

### Task 5: Identity (SMBIOS WMI)

**Files:**
- Create: `build/launcher/Identity.cs`

- [ ] **Step 1: Implement Identity.cs**

```csharp
// build/launcher/Identity.cs
using System.Management;

namespace Autopilot.Launcher;

public sealed record MachineIdentity(string Uuid, string Vendor, string Model);

public static class Identity
{
    public static MachineIdentity GetSmbios()
    {
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT UUID, Vendor, Name FROM Win32_ComputerSystemProduct");
            foreach (ManagementObject obj in searcher.Get())
            {
                return new MachineIdentity(
                    Uuid: obj["UUID"]?.ToString() ?? "UNKNOWN",
                    Vendor: obj["Vendor"]?.ToString() ?? "UNKNOWN",
                    Model: obj["Name"]?.ToString() ?? "UNKNOWN"
                );
            }
        }
        catch { }
        return new MachineIdentity("UNKNOWN", "UNKNOWN", "UNKNOWN");
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add build/launcher/Identity.cs
git commit -m "feat(launcher): SMBIOS identity via WMI"
```

---

### Task 6: Orchestrator (HTTP Client)

**Files:**
- Create: `build/launcher/Orchestrator.cs`

- [ ] **Step 1: Implement Orchestrator.cs**

```csharp
// build/launcher/Orchestrator.cs
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Autopilot.Launcher.Models;

namespace Autopilot.Launcher;

public sealed class Orchestrator : IDisposable
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromMinutes(30) };
    private readonly string _baseUrl;

    public Orchestrator(string baseUrl)
    {
        _baseUrl = baseUrl.TrimEnd('/');
    }

    public async Task<Manifest> FetchManifestAsync(string vmUuid, int retries, int backoffSec)
    {
        var url = $"{_baseUrl}/winpe/manifest/{vmUuid}";
        for (var attempt = 1; attempt <= retries; attempt++)
        {
            try
            {
                var json = await _http.GetStringAsync(url);
                return JsonSerializer.Deserialize<Manifest>(json)
                    ?? throw new InvalidOperationException("Manifest deserialized to null");
            }
            catch when (attempt < retries)
            {
                await Task.Delay(backoffSec * attempt * 1000);
            }
        }
        throw new HttpRequestException($"Failed to fetch manifest after {retries} attempts");
    }

    public async Task DownloadContentAsync(
        string sha256, string outPath, long expectedSize,
        Action<long, long> onProgress, CancellationToken ct = default)
    {
        var url = $"{_baseUrl}/winpe/content/{sha256}";
        using var response = await _http.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, ct);
        response.EnsureSuccessStatusCode();

        var dir = Path.GetDirectoryName(outPath);
        if (dir != null && !Directory.Exists(dir))
            Directory.CreateDirectory(dir);

        await using var stream = await response.Content.ReadAsStreamAsync(ct);
        await using var file = File.Create(outPath);
        var buffer = new byte[256 * 1024];
        long totalRead = 0;
        int read;
        while ((read = await stream.ReadAsync(buffer, ct)) > 0)
        {
            await file.WriteAsync(buffer.AsMemory(0, read), ct);
            totalRead += read;
            onProgress(totalRead, expectedSize);
        }

        // Verify sha256
        file.Position = 0;
        var hash = Convert.ToHexStringLower(await SHA256.HashDataAsync(file, ct));
        if (hash != sha256)
        {
            file.Close();
            File.Delete(outPath);
            throw new InvalidDataException($"SHA256 mismatch: expected {sha256}, got {hash}");
        }
    }

    public async Task SendCheckinAsync(CheckinPayload checkin)
    {
        try
        {
            var json = JsonSerializer.Serialize(checkin);
            using var content = new StringContent(json, Encoding.UTF8, "application/json");
            await _http.PostAsync($"{_baseUrl}/winpe/checkin", content);
        }
        catch
        {
            // Fire-and-forget — server unreachable does not block PE
        }
    }

    public void Dispose() => _http.Dispose();
}
```

- [ ] **Step 2: Commit**

```bash
git add build/launcher/Orchestrator.cs
git commit -m "feat(launcher): HTTP orchestrator — manifest, content download with progress, checkins"
```

---

### Task 7: PwshInvoker

**Files:**
- Create: `build/launcher/PwshInvoker.cs`

- [ ] **Step 1: Implement PwshInvoker.cs**

```csharp
// build/launcher/PwshInvoker.cs
using System.Diagnostics;
using System.Text;

namespace Autopilot.Launcher;

public sealed record PwshResult(int ExitCode, string Stdout, string Stderr);

public static class PwshInvoker
{
    private const string PwshPath = @"X:\Program Files\PowerShell\7\pwsh.exe";
    private const string ModulePath = @"X:\autopilot\Modules";

    public static PwshResult Invoke(string command, int timeoutMs = 600_000)
    {
        var fullCommand = $"$env:PSModulePath = '{ModulePath};' + $env:PSModulePath; {command}";
        var psi = new ProcessStartInfo
        {
            FileName = PwshPath,
            Arguments = $"-NoProfile -NonInteractive -Command \"{fullCommand.Replace("\"", "\\\"")}\"",
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };

        var stdout = new StringBuilder();
        var stderr = new StringBuilder();

        using var p = Process.Start(psi)
            ?? throw new InvalidOperationException("Failed to start pwsh.exe");

        p.OutputDataReceived += (_, e) => { if (e.Data != null) stdout.AppendLine(e.Data); };
        p.ErrorDataReceived += (_, e) => { if (e.Data != null) stderr.AppendLine(e.Data); };
        p.BeginOutputReadLine();
        p.BeginErrorReadLine();

        if (!p.WaitForExit(timeoutMs))
        {
            p.Kill(entireProcessTree: true);
            throw new TimeoutException($"pwsh command timed out after {timeoutMs}ms");
        }

        return new PwshResult(p.ExitCode, stdout.ToString(), stderr.ToString());
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add build/launcher/PwshInvoker.cs
git commit -m "feat(launcher): pwsh invoker — run PowerShell commands, capture output"
```

---

### Task 8: StepRunner

**Files:**
- Create: `build/launcher/StepRunner.cs`

- [ ] **Step 1: Implement StepRunner.cs**

```csharp
// build/launcher/StepRunner.cs
using System.Diagnostics;
using System.Text.Json;
using Autopilot.Launcher.Models;

namespace Autopilot.Launcher;

public sealed class StepRunner
{
    private readonly Orchestrator _orchestrator;
    private readonly string _vmUuid;

    public StepRunner(Orchestrator orchestrator, string vmUuid)
    {
        _orchestrator = orchestrator;
        _vmUuid = vmUuid;
    }

    public async Task<(string LogTail, Dictionary<string, object> Extra)> ExecuteStepAsync(
        Step step, Action<long, long> onDownloadProgress)
    {
        return step.Type switch
        {
            "log"           => ExecuteLog(step),
            "partition"     => ExecutePwshStep("Invoke-PartitionStep", $"-Layout '{step.Layout}'"),
            "apply-wim"     => await ExecuteApplyWimAsync(step, onDownloadProgress),
            "write-unattend"=> await ExecuteWriteContentAsync(step, onDownloadProgress),
            "stage-files"   => await ExecuteWriteContentAsync(step, onDownloadProgress),
            "set-registry"  => ExecuteSetRegistry(step),
            "bcdboot"       => ExecutePwshStep("Invoke-BcdbootStep", $"-Windows '{step.Windows}' -Esp '{step.Esp}'"),
            "inject-driver" => await ExecuteInjectDriverAsync(step, onDownloadProgress),
            "schedule-task" => ExecuteScheduleTask(step),
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

        var result = PwshInvoker.Invoke(
            $"Expand-WindowsImage -ImagePath '{tmpPath}' -Index {index} -ApplyPath '{target}' -ErrorAction Stop");
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

    private static (string, Dictionary<string, object>) ExecuteSetRegistry(Step step)
    {
        var keysJson = step.Keys?.GetRawText() ?? "[]";
        // Escape single quotes in JSON for pwsh command line
        var escaped = keysJson.Replace("'", "''");
        var result = PwshInvoker.Invoke(
            $"Import-Module Autopilot.PESteps; Invoke-SetRegistryStep -Hive '{step.Hive}' -Target '{step.Target}' -Keys ('{escaped}' | ConvertFrom-Json)");
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

        var result = PwshInvoker.Invoke(
            $"Expand-Archive -Path '{tmpZip}' -DestinationPath '{tmpDir}' -Force; " +
            $"Add-WindowsDriver -Path '{target}' -Driver '{tmpDir}' -Recurse -ForceUnsigned");
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"InjectDriverStep failed: {result.Stderr}");

        try { File.Delete(tmpZip); Directory.Delete(tmpDir, true); } catch { }

        return ($"injected drivers → {target}",
            new Dictionary<string, object> { ["target"] = target });
    }

    private static (string, Dictionary<string, object>) ExecuteScheduleTask(Step step)
    {
        var result = PwshInvoker.Invoke(
            $"Import-Module Autopilot.PESteps; Invoke-ScheduleTaskStep -Target '{step.Target}' -Name '{step.Name}' -TaskXml '{step.TaskXml?.Replace("'", "''")}'");
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"ScheduleTaskStep failed: {result.Stderr}");

        return ($"scheduled task {step.Name}",
            new Dictionary<string, object> { ["task"] = step.Name! });
    }

    private static (string, Dictionary<string, object>) ExecutePwshStep(string cmdlet, string args)
    {
        var result = PwshInvoker.Invoke($"Import-Module Autopilot.PESteps; {cmdlet} {args}");
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
```

- [ ] **Step 2: Commit**

```bash
git add build/launcher/StepRunner.cs
git commit -m "feat(launcher): step runner — routes steps to C# or pwsh dispatch"
```

---

### Task 9: Program.cs (Main Orchestration)

**Files:**
- Modify: `build/launcher/Program.cs`

- [ ] **Step 1: Implement the full phase orchestration**

```csharp
// build/launcher/Program.cs
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using Autopilot.Launcher;
using Autopilot.Launcher.Models;

// Phase 0: Window setup
try
{
    Console.Title = "Autopilot PE Bootstrap";
    Console.OutputEncoding = System.Text.Encoding.UTF8;
    if (RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
    {
        Console.WindowWidth = Math.Max(Console.WindowWidth, 72);
        Console.BufferWidth = Math.Max(Console.BufferWidth, 72);
        // Maximize via P/Invoke
        var handle = GetConsoleWindow();
        if (handle != IntPtr.Zero) ShowWindow(handle, 3); // SW_MAXIMIZE
    }
    Console.CursorVisible = false;
    Console.Clear();
}
catch { }

var logPath = @"X:\Windows\Temp\autopilot-pe.log";
StreamWriter? transcript = null;
try { transcript = new StreamWriter(logPath, append: true) { AutoFlush = true }; } catch { }

void Log(string msg)
{
    transcript?.WriteLine($"[{DateTime.UtcNow:o}] {msg}");
    Display.ShowPhase(msg);
}

// Phase 1: Guard
Log("Checking for existing Windows installation...");
var existingDrive = Guard.FindExistingWindows();
if (existingDrive != null)
{
    Log($"Windows found on {existingDrive} — shutting down PE.");
    Display.ShowGuardShutdown(existingDrive);
    Guard.Shutdown();
    return 0;
}

// Phase 2: wpeinit
WpeInit.RunWpeInit(Log);

// Phase 3: Config
var configPath = args.Length > 0 ? args[0] : @"X:\autopilot\Bootstrap.json";
if (!File.Exists(configPath))
{
    Log($"Config not found: {configPath}");
    Display.ShowError($"Config not found: {configPath}");
    Console.ReadLine();
    return 1;
}
var config = JsonSerializer.Deserialize<BootstrapConfig>(File.ReadAllText(configPath))!;
Log($"Orchestrator: {config.OrchestratorUrl}");

// Phase 3b: Network
Log("Waiting for network...");
string ip;
try
{
    ip = WpeInit.WaitForNetwork(config.NetworkTimeoutSec, Log);
    Log($"Got IP: {ip}");
}
catch (TimeoutException ex)
{
    Log($"Network timeout: {ex.Message}");
    Display.ShowError(ex.Message);
    Console.ReadLine();
    return 1;
}

// Phase 4: Identity
var identity = Identity.GetSmbios();
var hostname = Environment.MachineName;
Log($"Identity: {identity.Uuid} (vendor={identity.Vendor} model={identity.Model})");

// Phase 5: Manifest
using var orchestrator = new Orchestrator(config.OrchestratorUrl);
Log("Fetching manifest...");
Manifest manifest;
try
{
    manifest = await orchestrator.FetchManifestAsync(
        identity.Uuid, config.ManifestRetries, config.ManifestRetryBackoffSec);
    Log($"Manifest: {manifest.Steps.Count} steps, onError={manifest.OnError}");
}
catch (Exception ex)
{
    Log($"Manifest fetch failed: {ex.Message}");
    Display.ShowError(ex.Message);
    Console.ReadLine();
    return 1;
}

// Phase 6: Execute steps
var runner = new StepRunner(orchestrator, identity.Uuid);
var states = new StepState[manifest.Steps.Count];
var elapsed = new TimeSpan[manifest.Steps.Count];
Array.Fill(states, StepState.Pending);
int? dlPercent = null;
long? dlBytes = null;
var statusMsg = "Starting...";

void Redraw() => Display.RenderFull(
    identity.Uuid, identity.Vendor, identity.Model,
    ip, hostname, config.OrchestratorUrl,
    manifest.Steps, states, elapsed, dlPercent, dlBytes, statusMsg);

Console.Clear();
Redraw();

for (var i = 0; i < manifest.Steps.Count; i++)
{
    var step = manifest.Steps[i];
    states[i] = StepState.Active;
    dlPercent = null;
    dlBytes = null;
    statusMsg = $"{Display.StepTypeName(step.Type)}...";
    Redraw();

    var ts = DateTime.UtcNow.ToString("o");
    await orchestrator.SendCheckinAsync(new CheckinPayload
    {
        VmUuid = identity.Uuid, StepId = step.Id,
        Status = "starting", Timestamp = ts,
    });

    var sw = Stopwatch.StartNew();
    try
    {
        var (logTail, extra) = await runner.ExecuteStepAsync(step, (bytesRead, total) =>
        {
            dlBytes = bytesRead;
            dlPercent = total > 0 ? (int)(bytesRead * 100 / total) : 0;
            elapsed[i] = sw.Elapsed;
            Redraw();
        });

        sw.Stop();
        elapsed[i] = sw.Elapsed;
        states[i] = StepState.Done;
        statusMsg = $"{Display.StepTypeName(step.Type)} done ({Display.FormatDuration(sw.Elapsed)})";
        Redraw();

        await orchestrator.SendCheckinAsync(new CheckinPayload
        {
            VmUuid = identity.Uuid, StepId = step.Id,
            Status = "ok", Timestamp = DateTime.UtcNow.ToString("o"),
            DurationSec = sw.Elapsed.TotalSeconds, LogTail = logTail, Extra = extra,
        });
    }
    catch (Exception ex)
    {
        sw.Stop();
        elapsed[i] = sw.Elapsed;
        states[i] = StepState.Error;
        statusMsg = $"FAILED: {step.Id} — {ex.Message}";
        Redraw();

        await orchestrator.SendCheckinAsync(new CheckinPayload
        {
            VmUuid = identity.Uuid, StepId = step.Id,
            Status = "error", Timestamp = DateTime.UtcNow.ToString("o"),
            DurationSec = sw.Elapsed.TotalSeconds,
            LogTail = $"step {step.Id} failed: {ex.Message}",
            ErrorMessage = ex.Message,
        });

        Log($"Step {step.Id} failed: {ex.Message}");
        if (manifest.OnError == "halt")
        {
            Display.ShowError($"Halted on step {step.Id}: {ex.Message}");
            Log("onError=halt — blocking forever. SSH in to debug.");
            Thread.Sleep(Timeout.Infinite);
        }
    }
}

// Phase 7: Stage payload
Log("Staging first-boot payload to W:\\autopilot\\...");
statusMsg = "Staging payload...";
Redraw();
try
{
    Directory.CreateDirectory(@"W:\autopilot");
    File.Copy(configPath, @"W:\autopilot\Bootstrap.json", overwrite: true);
    var hwidScript = @"X:\autopilot\Collect-HardwareHash.ps1";
    if (File.Exists(hwidScript))
        File.Copy(hwidScript, @"W:\autopilot\Collect-HardwareHash.ps1", overwrite: true);
    Log("Staged payload to W:\\autopilot\\");
}
catch (Exception ex) { Log($"Staging warning: {ex.Message}"); }

// Phase 8: Reboot
Log("Rebooting...");
statusMsg = "Rebooting into Windows...";
Redraw();
Thread.Sleep(2000);
Process.Start(new ProcessStartInfo { FileName = "wpeutil", Arguments = "reboot", UseShellExecute = false });
transcript?.Dispose();
return 0;

// P/Invoke for window maximize
[DllImport("kernel32.dll")]
static extern IntPtr GetConsoleWindow();

[DllImport("user32.dll")]
static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
```

- [ ] **Step 2: Run tests to verify nothing broke**

Run: `cd build/launcher-tests && dotnet test -v normal`
Expected: All 15 tests PASS

- [ ] **Step 3: Commit**

```bash
git add build/launcher/Program.cs
git commit -m "feat(launcher): full phase orchestration with structured display"
```

---

### Task 10: Build Integration

**Files:**
- Modify: `build/Build-PeWim.ps1`
- Modify: `tools/build-pe-wim.sh`

- [ ] **Step 1: Update build wrapper to push launcher source**

In `tools/build-pe-wim.sh`, after the pe-payload scp, add launcher source push:

```bash
# In tools/build-pe-wim.sh, after the existing scp lines (around line 40-41):
# Add after: scp "$REPO_ROOT/build/Build-PeWim.ps1" ...

scp -r "$REPO_ROOT/build/launcher/." "${BUILD_USER}@${BUILD_HOST}:${BUILD_ROOT}/src/build/launcher/"
```

- [ ] **Step 2: Add launcher build phase to Build-PeWim.ps1**

Insert after Phase 4 (.NET runtime extraction), before Phase 5 (strip PS 5.1). Find the line `Log 'Info' "Extracted pwsh 7 → $pwshTarget"` and add after the OpenSSH optional block:

```powershell
        # ---- Phase 4c: Build launcher ----
        $launcherSrc = Join-Path (Split-Path $config.payloadDir) 'launcher'
        if (Test-Path $launcherSrc) {
            $launcherOut = Join-Path $workDir 'launcher-publish'
            if (Test-Path $launcherOut) { Remove-Item $launcherOut -Recurse -Force }
            Log 'Info' "Building launcher from $launcherSrc"
            & dotnet publish $launcherSrc -c Release -r win-arm64 --no-self-contained -o $launcherOut 2>&1 | ForEach-Object { Log 'Info' "dotnet: $_" }
            if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed with exit code $LASTEXITCODE" }
            Copy-Item (Join-Path $launcherOut '*') -Destination (Join-Path $config.payloadDir '.') -Force -Recurse
            Log 'Info' "Launcher built and staged"
        } else {
            Log 'Warning' "Launcher source not found at $launcherSrc — skipping"
        }
```

- [ ] **Step 3: Replace startnet.cmd with winpeshl.ini in Build-PeWim.ps1**

Replace the existing startnet.cmd override block (around lines 287-301) with:

```powershell
        # Remove winpeshl.ini from payload if present
        $winpeshlPayload = Join-Path $payloadTarget 'winpeshl.ini'
        if (Test-Path $winpeshlPayload) { Remove-Item $winpeshlPayload -Force }

        # Write winpeshl.ini to System32 — launches launcher.exe directly
        $winpeshlPath = Join-Path $peMount 'Windows\System32\winpeshl.ini'
        @(
            '[LaunchApps]'
            'X:\autopilot\launcher.exe'
        ) | Set-Content -Path $winpeshlPath -Encoding ascii

        Log 'Info' "Staged payload at X:\autopilot, rendered unattend.xml, wrote winpeshl.ini"
```

- [ ] **Step 4: Commit**

```bash
git add build/Build-PeWim.ps1 tools/build-pe-wim.sh
git commit -m "feat(launcher): build integration — dotnet publish in WIM pipeline + winpeshl.ini"
```

---

### Task 11: E2E Smoke Test

**Files:** None (runtime test)

- [ ] **Step 1: Ensure .NET 8 SDK on build host**

SSH to the build host and verify:

```bash
ssh adam_admin@10.211.55.6 "dotnet --list-sdks"
```

If no SDK, install .NET 8 SDK (arm64) on the build host. Download from https://dotnet.microsoft.com/download/dotnet/8.0 — the Windows ARM64 SDK installer.

- [ ] **Step 2: Build PE WIM with launcher**

```bash
cd /path/to/worktree && ./tools/build-pe-wim.sh 2>&1 | tee /tmp/launcher-build.log
```

Verify: `BUILD OK` in output, no dotnet publish errors.

- [ ] **Step 3: Verify launcher.exe in WIM**

Mount WIM on build host and confirm `X:\autopilot\launcher.exe` and `X:\autopilot\launcher.dll` exist.

- [ ] **Step 4: Run full E2E**

Same flow as the proven smoke test:
1. Fresh VM disk + ISO + quarantine fix
2. Boot → launcher.exe starts (no blue desktop, no cmd.exe)
3. Display shows structured status with progress bars
4. Pipeline completes → stages payload → reboots
5. Second boot → guard detects Windows → auto-shutdown
6. Remove CD → boot Windows 11 → OOBE skips region/keyboard
7. First login → Collect-HardwareHash.ps1 fires → POST to orchestrator

- [ ] **Step 5: Commit any E2E fixes**

```bash
git add -A
git commit -m "fix(launcher): E2E smoke test fixes"
```

---

## Verification

After all tasks complete:

1. `dotnet test` in `build/launcher-tests/` — all 15 tests pass
2. `build-pe-wim.sh` produces a WIM with `launcher.exe` at `X:\autopilot\`
3. Full E2E smoke test passes: PE → structured display → 6 steps → auto-shutdown → Windows OOBE → hardware hash collected
4. No cmd.exe, no batch files, no blue desktop — launcher.exe owns the window from boot to reboot
