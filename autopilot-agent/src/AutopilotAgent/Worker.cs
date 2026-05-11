using Microsoft.Extensions.Hosting;

namespace AutopilotAgent;

public sealed class Worker(
    AgentApiClient apiClient,
    TelemetryCollector telemetryCollector,
    HashCaptureService hashCaptureService,
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
                    var work = await apiClient.GetNextWorkAsync(
                        config,
                        ["capture_autopilot_hash"],
                        stoppingToken);
                    if (work is not null)
                    {
                        await ProcessWorkAsync(config, work, stoppingToken);
                    }
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

    private async Task ProcessWorkAsync(
        AgentConfig config,
        AgentWorkItem work,
        CancellationToken cancellationToken)
    {
        log.Info($"Starting work item {work.Id} ({work.Kind}).");
        try
        {
            switch (work.Kind)
            {
                case "capture_autopilot_hash":
                    await hashCaptureService.CaptureAsync(config, work, cancellationToken);
                    log.Info($"Completed work item {work.Id}.");
                    break;
                default:
                    await apiClient.FailWorkAsync(
                        config,
                        work.Id,
                        $"Unsupported work item kind: {work.Kind}",
                        cancellationToken);
                    log.Warning($"Unsupported work item {work.Id} kind={work.Kind}.");
                    break;
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            log.Error(ex, $"Work item {work.Id} failed.");
            await apiClient.FailWorkAsync(
                config,
                work.Id,
                ex.Message,
                cancellationToken);
        }
    }
}
