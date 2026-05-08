using System.Net.Http.Headers;
using System.Net.Http.Json;
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
}

public sealed record BootstrapResponse(
    [property: JsonPropertyName("agent_id")] string AgentId,
    [property: JsonPropertyName("agent_token")] string AgentToken,
    [property: JsonPropertyName("heartbeat_interval_seconds")] int HeartbeatIntervalSeconds);

internal static class ThisAssembly
{
    public static string Version =>
        typeof(ThisAssembly).Assembly.GetName().Version?.ToString() ?? "0.1.0";
}
