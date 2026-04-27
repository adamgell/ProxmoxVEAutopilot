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
