using System.Text.Json;
using Autopilot.Launcher.Models;
using Xunit;

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
