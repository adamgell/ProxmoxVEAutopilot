using Microsoft.Extensions.Hosting;

namespace AutopilotAgent;

public sealed class Worker(
    AgentApiClient apiClient,
    TelemetryCollector telemetryCollector,
    AgentFileLog log) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        log.Info("AutopilotAgent starting.");

        while (!stoppingToken.IsCancellationRequested)
        {
            var delay = TimeSpan.FromSeconds(30);
            try
            {
                var config = AgentConfig.LoadOrCreate();
                delay = TimeSpan.FromSeconds(Math.Max(10, config.HeartbeatIntervalSeconds));
                var telemetry = telemetryCollector.Collect();

                if (string.IsNullOrWhiteSpace(config.AgentToken)
                    && !string.IsNullOrWhiteSpace(config.BootstrapToken))
                {
                    var bootstrap = await apiClient.BootstrapAsync(
                        config,
                        telemetry,
                        stoppingToken);
                    config.AgentId = bootstrap.AgentId;
                    config.AgentToken = bootstrap.AgentToken;
                    config.HeartbeatIntervalSeconds = bootstrap.HeartbeatIntervalSeconds;
                    config.Save();
                    log.Info("Bootstrap completed.");
                }

                if (!string.IsNullOrWhiteSpace(config.AgentToken))
                {
                    await apiClient.SendHeartbeatAsync(config, telemetry, stoppingToken);
                    log.Info("Heartbeat sent.");
                }
                else
                {
                    log.Warning("AgentToken is missing; heartbeat skipped.");
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                log.Error(ex, "Agent loop failed.");
            }

            await Task.Delay(delay, stoppingToken);
        }

        log.Info("AutopilotAgent stopping.");
    }
}
