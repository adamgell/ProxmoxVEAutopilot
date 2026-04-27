using System.Diagnostics;
using System.Management;
using System.Text;
using System.Text.Json;
using Autopilot.Launcher.Models;

namespace Autopilot.Launcher;

public static class AuditMode
{
    private static readonly string[] Phases =
    [
        "Wait for network",
        "Identify machine",
        "Collect hardware hash",
        "Write CSV",
        "POST hash to orchestrator",
        "Sysprep to OOBE",
    ];

    public static async Task<int> RunAsync(string configPath)
    {
        var logPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.Windows),
            "Temp", "autopilot-hwid.log");
        StreamWriter? transcript = null;
        try { transcript = new StreamWriter(logPath, append: true) { AutoFlush = true }; } catch { }

        void Log(string msg) => transcript?.WriteLine($"[{DateTime.UtcNow:o}] {msg}");

        BootstrapConfig? config = null;
        if (File.Exists(configPath))
            config = JsonSerializer.Deserialize<BootstrapConfig>(File.ReadAllText(configPath));

        Orchestrator? orchestrator = config != null ? new Orchestrator(config.OrchestratorUrl) : null;
        string? vmUuid = null;
        var phase = 0;
        var status = "Starting audit mode...";

        // 1000ms heartbeat — one-shot re-arm to prevent concurrent pile-up
        Timer? heartbeatTimer = null;
        heartbeatTimer = new Timer(async _ =>
        {
            try
            {
                if (orchestrator == null || vmUuid == null) return;
                await orchestrator.SendHeartbeatAsync(vmUuid, $"audit:{Phases[Math.Min(phase, Phases.Length - 1)]}", status);
            }
            catch { }
            finally
            {
                try { heartbeatTimer?.Change(1000, Timeout.Infinite); } catch { }
            }
        }, null, Timeout.Infinite, Timeout.Infinite);

        void Redraw() => Display.RenderBoot(phase, vmUuid, null, null, null, null,
            config?.OrchestratorUrl, status, "Autopilot Audit Mode", Phases);

        async Task Checkin(string stepId, string st, string logTail = "", string? error = null)
        {
            if (orchestrator == null || vmUuid == null) return;
            await orchestrator.SendCheckinAsync(new CheckinPayload
            {
                VmUuid = vmUuid, StepId = stepId, Status = st,
                Timestamp = DateTime.UtcNow.ToString("o"), LogTail = logTail, ErrorMessage = error,
            });
        }

        Console.Clear();
        Redraw();

        // Phase 0: Network
        status = "Waiting for network...";
        Redraw();
        Log(status);
        string? ip = null;
        var deadline = DateTime.UtcNow.AddSeconds(60);
        while (DateTime.UtcNow < deadline)
        {
            var output = CaptureProcess("ipconfig");
            ip = WpeInit.ParseFirstNonApipaIp(output);
            if (ip != null) break;
            Thread.Sleep(3000);
        }
        if (ip != null)
        {
            status = $"Network ready: {ip}";
            Log(status);
        }
        else
        {
            status = "WARNING: No network — will write CSV only";
            Log(status);
        }
        phase = 1;
        Redraw();

        // Phase 1: Identity
        status = "Identifying machine...";
        Redraw();
        Log(status);
        string manufacturer = "", model = "", serial = "";
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT UUID, Vendor, Name, IdentifyingNumber FROM Win32_ComputerSystemProduct");
            foreach (ManagementObject obj in searcher.Get())
            {
                vmUuid = obj["UUID"]?.ToString() ?? "UNKNOWN";
                manufacturer = obj["Vendor"]?.ToString() ?? "";
                model = obj["Name"]?.ToString() ?? "";
                serial = obj["IdentifyingNumber"]?.ToString() ?? "";
            }
        }
        catch { vmUuid ??= "UNKNOWN"; }

        Log($"UUID: {vmUuid}, Manufacturer: {manufacturer}, Model: {model}, Serial: {serial}");
        status = $"UUID: {vmUuid}";
        phase = 2;
        Redraw();

        // Start heartbeat now that we have identity + orchestrator
        if (orchestrator != null && vmUuid != null)
            heartbeatTimer.Change(1000, Timeout.Infinite);
        await Checkin("audit-identity", "ok", $"UUID={vmUuid} vendor={manufacturer}");

        // Phase 2: Collect hardware hash
        status = "Collecting hardware hash via MDM bridge...";
        Redraw();
        Log(status);
        await Checkin("audit-hash", "starting", "Collecting hardware hash...");
        var hardwareHash = "";
        try
        {
            using var searcher = new ManagementObjectSearcher(
                @"root\cimv2\mdm\dmmap",
                "SELECT DeviceHardwareData FROM MDM_DevDetail_Ext01 WHERE InstanceID='Ext' AND ParentID='./DevDetail'");
            foreach (ManagementObject obj in searcher.Get())
                hardwareHash = obj["DeviceHardwareData"]?.ToString() ?? "";
            status = $"Hardware hash: {hardwareHash.Length} chars";
            Log(status);
            await Checkin("audit-hash", "ok", $"Hash length: {hardwareHash.Length}");
        }
        catch (Exception ex)
        {
            status = $"MDM unavailable (expected in VMs): {ex.Message}";
            Log(status);
            await Checkin("audit-hash", "ok", "MDM unavailable (VM) — hash empty");
        }
        phase = 3;
        Redraw();

        // Phase 3: Write CSV
        status = "Writing CSV...";
        Redraw();
        var timestamp = DateTime.UtcNow.ToString("o");
        var csvPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.Windows),
            "Temp", "autopilot-hwid.csv");
        var csvContent = new StringBuilder();
        csvContent.AppendLine("\"Device Serial Number\",\"Windows Product ID\",\"Hardware Hash\",\"Manufacturer\",\"Model\",\"UUID\",\"Timestamp\",\"PostedToOrchestrator\"");
        csvContent.AppendLine($"\"{serial}\",\"\",\"{hardwareHash}\",\"{manufacturer}\",\"{model}\",\"{vmUuid}\",\"{timestamp}\",\"False\"");
        File.WriteAllText(csvPath, csvContent.ToString());
        status = $"CSV: {csvPath}";
        Log(status);
        phase = 4;
        Redraw();

        // Phase 4: POST to orchestrator
        var posted = false;
        if (orchestrator != null && ip != null)
        {
            status = "POSTing hash to orchestrator...";
            Redraw();
            Log(status);
            await Checkin("audit-post", "starting", "POSTing hash...");

            var body = JsonSerializer.Serialize(new
            {
                vmUuid, serial, hardwareHash, manufacturer, model, timestamp
            });

            using var hwidClient = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
            for (var attempt = 1; attempt <= 5; attempt++)
            {
                try
                {
                    using var content = new StringContent(body, Encoding.UTF8, "application/json");
                    var resp = await hwidClient
                        .PostAsync($"{config!.OrchestratorUrl.TrimEnd('/')}/winpe/hwid", content);
                    resp.EnsureSuccessStatusCode();
                    posted = true;
                    status = "Hash delivered to orchestrator";
                    Log(status);
                    await Checkin("audit-post", "ok", "Hash delivered");
                    break;
                }
                catch (Exception ex)
                {
                    status = $"POST attempt {attempt}/5 failed: {ex.Message}";
                    Redraw();
                    Log(status);
                    await Checkin("audit-post", "starting", $"Attempt {attempt}/5 failed");
                    if (attempt < 5) await Task.Delay(attempt * 5000);
                }
            }
            if (!posted)
                await Checkin("audit-post", "error", $"POST failed — CSV at {csvPath}", "POST failed after retries");
        }
        else
        {
            status = "No orchestrator/network — CSV only";
            Log(status);
        }

        // Update CSV with POST status
        csvContent.Clear();
        csvContent.AppendLine("\"Device Serial Number\",\"Windows Product ID\",\"Hardware Hash\",\"Manufacturer\",\"Model\",\"UUID\",\"Timestamp\",\"PostedToOrchestrator\"");
        csvContent.AppendLine($"\"{serial}\",\"\",\"{hardwareHash}\",\"{manufacturer}\",\"{model}\",\"{vmUuid}\",\"{timestamp}\",\"{posted}\"");
        File.WriteAllText(csvPath, csvContent.ToString());

        phase = 5;
        status = posted ? "Hash delivered — preparing sysprep..." : $"CSV at {csvPath} — preparing sysprep...";
        Redraw();
        Log(status);

        // Phase 5: Sysprep
        await Checkin("audit-sysprep", "starting", "Running sysprep /oobe /shutdown...");
        heartbeatTimer.Change(Timeout.Infinite, Timeout.Infinite);
        status = "Running sysprep /oobe /shutdown...";
        Redraw();
        Log(status);

        Process.Start(new ProcessStartInfo
        {
            FileName = @"C:\Windows\System32\Sysprep\sysprep.exe",
            Arguments = "/oobe /shutdown",
            UseShellExecute = false,
        })?.WaitForExit(300_000);

        transcript?.Dispose();
        orchestrator?.Dispose();
        return 0;
    }

    private static string CaptureProcess(string fileName)
    {
        try
        {
            var p = Process.Start(new ProcessStartInfo
            {
                FileName = fileName, UseShellExecute = false, RedirectStandardOutput = true,
            });
            if (p == null) return "";
            var output = p.StandardOutput.ReadToEnd();
            p.WaitForExit(10_000);
            return output;
        }
        catch { return ""; }
    }
}
