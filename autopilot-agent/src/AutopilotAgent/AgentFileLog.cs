namespace AutopilotAgent;

public sealed class AgentFileLog
{
    private readonly object _gate = new();

    public void Info(string message) => Write("INFO", message);

    public void Warning(string message) => Write("WARN", message);

    public void Error(Exception exception, string message) =>
        Write("ERROR", $"{message} {exception.GetType().Name}: {exception.Message}");

    private void Write(string level, string message)
    {
        Directory.CreateDirectory(AgentConfig.LogDirectory);
        var line = $"{DateTimeOffset.UtcNow:o} [{level}] {message}{Environment.NewLine}";
        var path = Path.Combine(
            AgentConfig.LogDirectory,
            $"autopilot-agent-{DateTimeOffset.UtcNow:yyyyMMdd}.log");
        lock (_gate)
        {
            File.AppendAllText(path, line);
        }
    }
}
