using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace AutopilotAgent;

public sealed class AgentApiClient(HttpClient httpClient)
{
    public async Task<BootstrapResponse> BootstrapAsync(
        AgentConfig config,
        TelemetrySnapshot snapshot,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.BootstrapToken))
        {
            throw new InvalidOperationException("BootstrapToken is not configured.");
        }

        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/api/agent/v1/bootstrap")
        {
            Content = JsonContent.Create(new
            {
                agent_id = config.AgentId,
                run_id = config.RunId,
                phase = config.Phase,
                vmid = config.Vmid,
                vm_uuid = config.VmUuid,
                computer_name = snapshot.ComputerName,
                serial_number = snapshot.SerialNumber,
                agent_version = ThisAssembly.Version,
            }),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            config.BootstrapToken);

        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<BootstrapResponse>(
            cancellationToken: cancellationToken)
            ?? throw new InvalidOperationException("Bootstrap response was empty.");
    }

    public async Task SendHeartbeatAsync(
        AgentConfig config,
        TelemetrySnapshot snapshot,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.AgentToken))
        {
            throw new InvalidOperationException("AgentToken is not configured.");
        }

        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/api/agent/v1/heartbeat")
        {
            Content = JsonContent.Create(new
            {
                agent_id = config.AgentId,
                vmid = config.Vmid,
                vm_uuid = config.VmUuid,
                computer_name = snapshot.ComputerName,
                serial_number = snapshot.SerialNumber,
                primary_ipv4 = snapshot.PrimaryIpv4,
                ip_addresses = snapshot.IpAddresses,
                nics = snapshot.Nics,
                os_name = snapshot.OsName,
                os_version = snapshot.OsVersion,
                os_build = snapshot.OsBuild,
                boot_time = snapshot.BootTime,
                uptime_seconds = snapshot.UptimeSeconds,
                qga_service_name = snapshot.QgaServiceName,
                qga_state = snapshot.QgaState,
                domain_name = snapshot.DomainName,
                domain_joined = snapshot.DomainJoined,
                entra_joined = snapshot.EntraJoined,
                tenant_id = snapshot.TenantId,
                current_run_id = config.RunId,
                current_phase = config.Phase,
                agent_version = ThisAssembly.Version,
            }),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            config.AgentToken);

        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
    }

    public async Task<AgentWorkItem?> GetNextWorkAsync(
        AgentConfig config,
        IReadOnlyCollection<string> supportedKinds,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.AgentToken))
        {
            throw new InvalidOperationException("AgentToken is not configured.");
        }

        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/api/agent/v1/work/next")
        {
            Content = JsonContent.Create(new
            {
                agent_id = config.AgentId,
                supported_kinds = supportedKinds,
            }),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            config.AgentToken);

        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
        var body = await response.Content.ReadFromJsonAsync<AgentWorkNextResponse>(
            cancellationToken: cancellationToken);
        return body?.WorkItem;
    }

    public async Task<string> DownloadHashScriptAsync(
        AgentConfig config,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.AgentToken))
        {
            throw new InvalidOperationException("AgentToken is not configured.");
        }

        var request = new HttpRequestMessage(
            HttpMethod.Get,
            $"{config.ServerUrl.TrimEnd('/')}/api/agent/v1/hash-script");
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            config.AgentToken);
        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadAsStringAsync(cancellationToken);
    }

    public async Task<HashUploadResponse> UploadHashAsync(
        AgentConfig config,
        string workItemId,
        string serialNumber,
        string productId,
        string hardwareHash,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.AgentToken))
        {
            throw new InvalidOperationException("AgentToken is not configured.");
        }

        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/api/agent/v1/hash")
        {
            Content = JsonContent.Create(new
            {
                work_item_id = workItemId,
                serial_number = serialNumber,
                product_id = productId,
                hardware_hash = hardwareHash,
            }),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            config.AgentToken);
        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<HashUploadResponse>(
            cancellationToken: cancellationToken)
            ?? throw new InvalidOperationException("Hash upload response was empty.");
    }

    public async Task CompleteWorkAsync(
        AgentConfig config,
        string workItemId,
        IReadOnlyDictionary<string, object?> result,
        CancellationToken cancellationToken)
    {
        await PostWorkResultAsync(
            config,
            workItemId,
            "complete",
            new { agent_id = config.AgentId, result },
            cancellationToken);
    }

    public async Task FailWorkAsync(
        AgentConfig config,
        string workItemId,
        string error,
        CancellationToken cancellationToken)
    {
        await PostWorkResultAsync(
            config,
            workItemId,
            "fail",
            new
            {
                agent_id = config.AgentId,
                error,
                result = new Dictionary<string, object?>(),
            },
            cancellationToken);
    }

    private async Task PostWorkResultAsync(
        AgentConfig config,
        string workItemId,
        string action,
        object body,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.AgentToken))
        {
            throw new InvalidOperationException("AgentToken is not configured.");
        }
        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/api/agent/v1/work/{workItemId}/{action}")
        {
            Content = JsonContent.Create(body),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            config.AgentToken);
        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
    }
}

public sealed record BootstrapResponse(
    [property: JsonPropertyName("agent_id")] string AgentId,
    [property: JsonPropertyName("agent_token")] string AgentToken,
    [property: JsonPropertyName("heartbeat_interval_seconds")] int HeartbeatIntervalSeconds);

public sealed record AgentWorkNextResponse(
    [property: JsonPropertyName("work_item")] AgentWorkItem? WorkItem);

public sealed record AgentWorkItem(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("request")] Dictionary<string, JsonElement> Request);

public sealed record HashUploadResponse(
    [property: JsonPropertyName("ok")] bool Ok,
    [property: JsonPropertyName("filename")] string Filename);

internal static class ThisAssembly
{
    public static string Version =>
        typeof(ThisAssembly).Assembly.GetName().Version?.ToString() ?? "0.1.0";
}
