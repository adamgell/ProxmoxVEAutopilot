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

// Boot-phase display state — shown from the very first frame
string? bUuid = null, bVendor = null, bModel = null, bIp = null, bHost = null, bServer = null;
var bootPhase = 0;
var bootStatus = "Starting...";

void BootRedraw()
{
    Display.RenderBoot(bootPhase, bUuid, bVendor, bModel, bIp, bHost, bServer, bootStatus);
}

void Log(string msg)
{
    transcript?.WriteLine($"[{DateTime.UtcNow:o}] {msg}");
}

Console.Clear();
BootRedraw();

// Phase 0: Guard
bootStatus = "Checking for existing Windows installation...";
BootRedraw();
Log(bootStatus);
var existingDrive = Guard.FindExistingWindows();
if (existingDrive != null)
{
    Log($"Windows found on {existingDrive} — shutting down PE.");
    Display.ShowGuardShutdown(existingDrive);
    Guard.Shutdown();
    return 0;
}
bootPhase = 1;

// Phase 1: wpeinit
bootStatus = "Running wpeinit (initializing hardware)...";
BootRedraw();
Log(bootStatus);
WpeInit.RunWpeInit(s => { bootStatus = s; BootRedraw(); Log(s); });
bootPhase = 2;

// Phase 2: Config
bootStatus = "Loading config...";
BootRedraw();
var configPath = args.Length > 0 ? args[0] : @"X:\autopilot\Bootstrap.json";
if (!File.Exists(configPath))
{
    bootStatus = $"ERROR: Config not found: {configPath}";
    BootRedraw();
    Log(bootStatus);
    Console.ReadLine();
    return 1;
}
var config = JsonSerializer.Deserialize<BootstrapConfig>(File.ReadAllText(configPath))!;
bServer = config.OrchestratorUrl;
Log($"Orchestrator: {config.OrchestratorUrl}");
bootPhase = 3;

// Phase 3: Network
bootStatus = "Waiting for network...";
BootRedraw();
string ip;
try
{
    ip = WpeInit.WaitForNetwork(config.NetworkTimeoutSec, s =>
    {
        bootStatus = s;
        BootRedraw();
        Log(s);
    });
    bIp = ip;
    Log($"Got IP: {ip}");
}
catch (TimeoutException ex)
{
    bootStatus = $"ERROR: {ex.Message}";
    BootRedraw();
    Log(bootStatus);
    Console.ReadLine();
    return 1;
}
bootPhase = 4;

// Phase 4: Identity
bootStatus = "Identifying machine...";
BootRedraw();
var identity = Identity.GetSmbios();
var hostname = Environment.MachineName;
bUuid = identity.Uuid;
bVendor = identity.Vendor;
bModel = identity.Model;
bHost = hostname;
Log($"Identity: {identity.Uuid} (vendor={identity.Vendor} model={identity.Model})");
bootPhase = 5;

// Phase 5: Manifest
bootStatus = "Fetching manifest...";
BootRedraw();
using var orchestrator = new Orchestrator(config.OrchestratorUrl);
Manifest manifest;
try
{
    manifest = await orchestrator.FetchManifestAsync(
        identity.Uuid, config.ManifestRetries, config.ManifestRetryBackoffSec);
    Log($"Manifest: {manifest.Steps.Count} steps, onError={manifest.OnError}");
}
catch (Exception ex)
{
    bootStatus = $"ERROR: Manifest fetch failed — {ex.Message}";
    BootRedraw();
    Log(bootStatus);
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
    // Stage agent if present
    var agentSrc = @"X:\autopilot\agent";
    if (Directory.Exists(agentSrc))
    {
        var agentDst = @"W:\autopilot\agent";
        Directory.CreateDirectory(agentDst);
        foreach (var f in Directory.GetFiles(agentSrc, "*", SearchOption.AllDirectories))
        {
            var rel = Path.GetRelativePath(agentSrc, f);
            var dst = Path.Combine(agentDst, rel);
            Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
            File.Copy(f, dst, overwrite: true);
        }
        Log($"Staged agent to {agentDst}");
    }
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
