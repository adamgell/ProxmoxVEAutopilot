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
