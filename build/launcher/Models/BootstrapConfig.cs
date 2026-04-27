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
