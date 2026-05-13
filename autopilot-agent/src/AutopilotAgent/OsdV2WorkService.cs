using System.Text.Json;

namespace AutopilotAgent;

public sealed class OsdV2WorkService(
    AgentApiClient apiClient,
    HashCaptureService hashCaptureService,
    AgentFileLog log)
{
    public const string FullOsPhase = "full_os";

    public static readonly string[] SupportedKinds =
    [
        "capture_autopilot_hash",
        "verify_ad_domain_join",
        "wait_agent_heartbeat",
    ];

    public async Task ProcessOnceAsync(
        AgentConfig config,
        TelemetrySnapshot telemetry,
        CancellationToken cancellationToken)
    {
        if (!IsCloudOsdV2Eligible(config))
        {
            return;
        }

        var registered = await apiClient.RegisterOsdV2AgentAsync(
            config,
            telemetry,
            SupportedKinds,
            cancellationToken);
        var bearerToken = registered.BearerToken;
        var next = await apiClient.GetNextOsdV2ActionAsync(
            config,
            bearerToken,
            cancellationToken);
        foreach (var action in next.Actions)
        {
            await ProcessActionAsync(
                config,
                telemetry,
                bearerToken,
                action,
                cancellationToken);
        }
    }

    public static bool IsCloudOsdV2Eligible(AgentConfig config) =>
        !string.IsNullOrWhiteSpace(config.RunId)
        && string.Equals(config.Phase, "cloudosd", StringComparison.OrdinalIgnoreCase);

    public static bool IsDomainJoinSatisfied(
        TelemetrySnapshot telemetry,
        IReadOnlyDictionary<string, JsonElement> parameters)
    {
        if (telemetry.DomainJoined != true)
        {
            return false;
        }

        var reported = (telemetry.DomainName ?? string.Empty).Trim();
        if (string.IsNullOrWhiteSpace(reported))
        {
            return false;
        }

        var accepted = ReadStringArray(parameters, "acceptable_domain_names")
            .Concat(ReadOptionalString(parameters, "domain_fqdn"))
            .Concat(ReadOptionalString(parameters, "credential_domain"))
            .SelectMany(ExpandDomainAliases)
            .Where(value => !string.IsNullOrWhiteSpace(value))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();

        if (accepted.Length == 0)
        {
            return true;
        }

        return ExpandDomainAliases(reported)
            .Any(alias => accepted.Contains(alias, StringComparer.OrdinalIgnoreCase));
    }

    private async Task ProcessActionAsync(
        AgentConfig config,
        TelemetrySnapshot telemetry,
        string bearerToken,
        OsdV2Action action,
        CancellationToken cancellationToken)
    {
        log.Info($"Starting OSD v2 step {action.StepId} ({action.Kind}).");
        await apiClient.PostOsdV2StepLogAsync(
            config,
            bearerToken,
            action.StepId,
            "stdout",
            $"AutopilotAgent starting {action.Kind}",
            cancellationToken);

        try
        {
            switch (action.Kind)
            {
                case "capture_autopilot_hash":
                    await hashCaptureService.CaptureOsdV2Async(
                        config,
                        action,
                        bearerToken,
                        cancellationToken);
                    await apiClient.CompleteOsdV2StepAsync(
                        config,
                        bearerToken,
                        action.StepId,
                        "Autopilot hardware hash uploaded through AutopilotAgent v2",
                        new Dictionary<string, object?>
                        {
                            ["source"] = "autopilotagent-v2",
                            ["group_tag"] = ReadString(action.Params, "group_tag"),
                        },
                        cancellationToken);
                    break;

                case "wait_agent_heartbeat":
                    await apiClient.CompleteOsdV2StepAsync(
                        config,
                        bearerToken,
                        action.StepId,
                        "AutopilotAgent heartbeat confirmed from full OS",
                        TelemetryData(telemetry),
                        cancellationToken);
                    break;

                case "verify_ad_domain_join":
                    if (IsDomainJoinSatisfied(telemetry, action.Params))
                    {
                        await apiClient.CompleteOsdV2StepAsync(
                            config,
                            bearerToken,
                            action.StepId,
                            "AD domain membership verified by AutopilotAgent v2",
                            TelemetryData(telemetry),
                            cancellationToken);
                    }
                    else
                    {
                        await apiClient.FailOsdV2StepAsync(
                            config,
                            bearerToken,
                            action.StepId,
                            "AD domain membership is not present in full-OS telemetry yet",
                            TelemetryData(telemetry),
                            cancellationToken);
                    }
                    break;

                default:
                    await apiClient.FailOsdV2StepAsync(
                        config,
                        bearerToken,
                        action.StepId,
                        $"Unsupported OSD v2 step kind: {action.Kind}",
                        new Dictionary<string, object?>(),
                        cancellationToken);
                    break;
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            log.Error(ex, $"OSD v2 step {action.StepId} failed.");
            await apiClient.FailOsdV2StepAsync(
                config,
                bearerToken,
                action.StepId,
                ex.Message,
                new Dictionary<string, object?> { ["exception_type"] = ex.GetType().Name },
                cancellationToken);
        }
    }

    private static Dictionary<string, object?> TelemetryData(TelemetrySnapshot telemetry) => new()
    {
        ["computer_name"] = telemetry.ComputerName,
        ["serial_number"] = telemetry.SerialNumber,
        ["primary_ipv4"] = telemetry.PrimaryIpv4,
        ["domain_name"] = telemetry.DomainName,
        ["domain_joined"] = telemetry.DomainJoined,
        ["qga_state"] = telemetry.QgaState,
    };

    private static string ReadString(
        IReadOnlyDictionary<string, JsonElement> values,
        string key)
    {
        if (!values.TryGetValue(key, out var value))
        {
            return string.Empty;
        }
        return value.ValueKind switch
        {
            JsonValueKind.String => value.GetString() ?? string.Empty,
            JsonValueKind.Number => value.ToString(),
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            _ => string.Empty,
        };
    }

    private static IEnumerable<string> ReadOptionalString(
        IReadOnlyDictionary<string, JsonElement> values,
        string key)
    {
        var value = ReadString(values, key);
        if (!string.IsNullOrWhiteSpace(value))
        {
            yield return value;
        }
    }

    private static IEnumerable<string> ReadStringArray(
        IReadOnlyDictionary<string, JsonElement> values,
        string key)
    {
        if (!values.TryGetValue(key, out var value))
        {
            yield break;
        }
        if (value.ValueKind == JsonValueKind.String)
        {
            var single = value.GetString();
            if (!string.IsNullOrWhiteSpace(single))
            {
                yield return single;
            }
            yield break;
        }
        if (value.ValueKind != JsonValueKind.Array)
        {
            yield break;
        }
        foreach (var item in value.EnumerateArray())
        {
            if (item.ValueKind == JsonValueKind.String)
            {
                var text = item.GetString();
                if (!string.IsNullOrWhiteSpace(text))
                {
                    yield return text;
                }
            }
        }
    }

    private static IEnumerable<string> ExpandDomainAliases(string value)
    {
        var trimmed = value.Trim();
        if (string.IsNullOrWhiteSpace(trimmed))
        {
            yield break;
        }
        yield return trimmed;
        var dot = trimmed.IndexOf('.');
        if (dot > 0)
        {
            yield return trimmed[..dot];
        }
    }
}
