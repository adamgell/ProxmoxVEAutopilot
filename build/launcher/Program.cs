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
