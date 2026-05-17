using System.Text.Json;
using System.Text.Json.Serialization;

namespace AutopilotAgent;

public sealed class AgentConfig
{
    public const string RelativeConfigPath = @"ProxmoxVEAutopilot\AutopilotAgent\agent.json";
    public const string RelativeLogPath = @"ProxmoxVEAutopilot\AutopilotAgent\logs";

    public string? ServerUrl { get; set; }
    public string? BootstrapToken { get; set; }
    public string? AgentId { get; set; }
    public string? AgentToken { get; set; }
    public string? RunId { get; set; }
    public string? Phase { get; set; }
    public string? Role { get; set; }
    public string[] Capabilities { get; set; } = [];
    public int? Vmid { get; set; }
    public string? VmUuid { get; set; }
    public int HeartbeatIntervalSeconds { get; set; } = 30;

    [JsonIgnore]
    public static string ProgramDataRoot =>
        Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            @"ProxmoxVEAutopilot\AutopilotAgent");

    [JsonIgnore]
    public static string ConfigPath =>
        Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            RelativeConfigPath);

    [JsonIgnore]
    public static string LogDirectory =>
        Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            RelativeLogPath);

    public static AgentConfig LoadOrCreate()
    {
        Directory.CreateDirectory(ProgramDataRoot);
        Directory.CreateDirectory(LogDirectory);
        if (!File.Exists(ConfigPath))
        {
            var created = new AgentConfig
            {
                AgentId = $"agent-{Environment.MachineName.ToLowerInvariant()}",
            };
            created.Save();
            return created;
        }

        var json = File.ReadAllText(ConfigPath);
        var config = JsonSerializer.Deserialize<AgentConfig>(
            json,
            JsonOptions()) ?? new AgentConfig();
        config.AgentId ??= $"agent-{Environment.MachineName.ToLowerInvariant()}";
        return config;
    }

    public void Save()
    {
        Directory.CreateDirectory(ProgramDataRoot);
        Directory.CreateDirectory(LogDirectory);
        var json = JsonSerializer.Serialize(this, JsonOptions());
        File.WriteAllText(ConfigPath, json);
    }

    public static JsonSerializerOptions JsonOptions() => new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = true,
    };
}
