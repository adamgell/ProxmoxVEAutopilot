using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Autopilot.Launcher.Models;

namespace Autopilot.Launcher;

public sealed class Orchestrator : IDisposable
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromMinutes(30) };
    private readonly string _baseUrl;
    internal string? _heartbeatVmUuid;
    internal string? _heartbeatPhase;

    public Orchestrator(string baseUrl)
    {
        _baseUrl = baseUrl.TrimEnd('/');
    }

    public void SetHeartbeatContext(string vmUuid, string phase)
    {
        _heartbeatVmUuid = vmUuid;
        _heartbeatPhase = phase;
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
        var lastHeartbeat = DateTime.UtcNow;
        while ((read = await stream.ReadAsync(buffer, ct)) > 0)
        {
            await file.WriteAsync(buffer.AsMemory(0, read), ct);
            totalRead += read;
            onProgress(totalRead, expectedSize);
            // Inline heartbeat every 1s during download
            if (_heartbeatVmUuid != null && (DateTime.UtcNow - lastHeartbeat).TotalMilliseconds >= 1000)
            {
                lastHeartbeat = DateTime.UtcNow;
                await SendHeartbeatAsync(_heartbeatVmUuid, _heartbeatPhase ?? "download", $"downloading {totalRead}/{expectedSize}");
            }
        }

        await file.FlushAsync(ct);
        file.Position = 0;
        var hash = Convert.ToHexString(await SHA256.HashDataAsync(file, ct)).ToLowerInvariant();
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
        catch { }
    }

    public async Task SendHeartbeatAsync(string vmUuid, string phase, string detail = "")
    {
        await SendCheckinAsync(new CheckinPayload
        {
            VmUuid = vmUuid,
            StepId = "heartbeat",
            Status = "ok",
            Timestamp = DateTime.UtcNow.ToString("o"),
            LogTail = detail,
            Extra = new Dictionary<string, object> { ["phase"] = phase },
        });
    }

    public void Dispose() => _http.Dispose();
}
