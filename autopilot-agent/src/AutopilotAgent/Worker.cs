using Microsoft.Extensions.Hosting;

namespace AutopilotAgent;

public sealed class Worker(
    AgentApiClient apiClient,
    TelemetryCollector telemetryCollector,
    HashCaptureService hashCaptureService,
    OsdV2WorkService osdV2WorkService,
    BuildHostWorkService buildHostWorkService,
    AgentFileLog log) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        log.Info("AutopilotAgent starting.");

        while (!stoppingToken.IsCancellationRequested)
        {
            var delay = TimeSpan.FromSeconds(30);
            var bootstrapPending = false;
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
                    if (string.IsNullOrWhiteSpace(bootstrap.AgentToken))
                    {
                        bootstrapPending = true;
                        delay = TimeSpan.FromSeconds(
                            Math.Max(10, bootstrap.RetryAfterSeconds ?? config.HeartbeatIntervalSeconds));
                        log.Info(
                            $"Bootstrap approval pending (status={bootstrap.ApprovalStatus ?? "pending"}).");
                    }
                    else
                    {
                        config.AgentId = bootstrap.AgentId;
                        config.AgentToken = bootstrap.AgentToken;
                        config.HeartbeatIntervalSeconds = bootstrap.HeartbeatIntervalSeconds;
                        config.Save();
                        log.Info("Bootstrap completed.");
                    }
                }

                if (!string.IsNullOrWhiteSpace(config.AgentToken))
                {
                    var supportedKinds = SupportedWorkKinds(config);
                    await apiClient.SendHeartbeatAsync(
                        config,
                        telemetry,
                        supportedKinds,
                        stoppingToken);
                    log.Info("Heartbeat sent.");
                    await osdV2WorkService.ProcessOnceAsync(
                        config,
                        telemetry,
                        stoppingToken);
                    var work = await apiClient.GetNextWorkAsync(
                        config,
                        supportedKinds,
                        stoppingToken);
                    if (work is not null)
                    {
                        await ProcessWorkAsync(config, work, stoppingToken);
                    }
                }
                else
                {
                    if (!bootstrapPending)
                    {
                        log.Warning("AgentToken is missing; heartbeat skipped.");
                    }
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

    private static List<string> SupportedWorkKinds(AgentConfig config)
    {
        var supportedKinds = new List<string>
        {
            "capture_autopilot_hash",
            "configure_build_host_role",
        };
        if (string.Equals(config.Phase, "build-host", StringComparison.OrdinalIgnoreCase)
            || string.Equals(config.Role, "build-host", StringComparison.OrdinalIgnoreCase))
        {
            supportedKinds.AddRange(
                BuildHostWorkService.SupportedKinds.Where(
                    kind => !supportedKinds.Contains(kind, StringComparer.Ordinal)));
        }
        return supportedKinds;
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
                case var kind when BuildHostWorkService.SupportedKinds.Contains(
                    kind,
                    StringComparer.Ordinal):
                    await buildHostWorkService.ProcessAsync(config, work, cancellationToken);
                    log.Info($"Completed build-host work item {work.Id}.");
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
