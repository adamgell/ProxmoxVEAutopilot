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
        IReadOnlyCollection<string> capabilities,
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
                server_url = config.ServerUrl,
                capabilities,
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

    public async Task<OsdV2RegisterResponse> RegisterOsdV2AgentAsync(
        AgentConfig config,
        TelemetrySnapshot snapshot,
        IReadOnlyCollection<string> capabilities,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.RunId))
        {
            throw new InvalidOperationException("RunId is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.AgentId))
        {
            throw new InvalidOperationException("AgentId is not configured.");
        }

        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/osd/v2/agent/register")
        {
            Content = JsonContent.Create(new
            {
                run_id = config.RunId,
                agent_id = config.AgentId,
                phase = OsdV2WorkService.FullOsPhase,
                computer_name = snapshot.ComputerName,
                build_sha = ThisAssembly.Version,
                capabilities,
            }),
        };

        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<OsdV2RegisterResponse>(
            cancellationToken: cancellationToken)
            ?? throw new InvalidOperationException("OSD v2 register response was empty.");
    }

    public async Task<OsdV2NextResponse> GetNextOsdV2ActionAsync(
        AgentConfig config,
        string bearerToken,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.RunId))
        {
            throw new InvalidOperationException("RunId is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.AgentId))
        {
            throw new InvalidOperationException("AgentId is not configured.");
        }

        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/osd/v2/agent/next")
        {
            Content = JsonContent.Create(new
            {
                run_id = config.RunId,
                agent_id = config.AgentId,
                phase = OsdV2WorkService.FullOsPhase,
                batch_size = 1,
            }),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            bearerToken);

        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<OsdV2NextResponse>(
            cancellationToken: cancellationToken)
            ?? throw new InvalidOperationException("OSD v2 next response was empty.");
    }

    public async Task UploadOsdV2HashAsync(
        AgentConfig config,
        string bearerToken,
        string serialNumber,
        string productId,
        string hardwareHash,
        string groupTag,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }

        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/osd/v2/agent/hash")
        {
            Content = JsonContent.Create(new
            {
                serial_number = serialNumber,
                product_id = productId,
                hardware_hash = hardwareHash,
                group_tag = groupTag,
            }),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            bearerToken);
        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
    }

    public async Task PostOsdV2StepLogAsync(
        AgentConfig config,
        string bearerToken,
        string stepId,
        string stream,
        string content,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.RunId))
        {
            throw new InvalidOperationException("RunId is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.AgentId))
        {
            throw new InvalidOperationException("AgentId is not configured.");
        }

        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/osd/v2/agent/step/{stepId}/logs")
        {
            Content = JsonContent.Create(new
            {
                run_id = config.RunId,
                agent_id = config.AgentId,
                stream,
                content,
            }),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            bearerToken);
        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
    }

    public Task CompleteOsdV2StepAsync(
        AgentConfig config,
        string bearerToken,
        string stepId,
        string message,
        IReadOnlyDictionary<string, object?> data,
        CancellationToken cancellationToken) =>
        PostOsdV2StepResultAsync(
            config,
            bearerToken,
            stepId,
            "success",
            message,
            data,
            cancellationToken);

    public Task FailOsdV2StepAsync(
        AgentConfig config,
        string bearerToken,
        string stepId,
        string message,
        IReadOnlyDictionary<string, object?> data,
        CancellationToken cancellationToken) =>
        PostOsdV2StepResultAsync(
            config,
            bearerToken,
            stepId,
            "failed",
            message,
            data,
            cancellationToken);

    private async Task PostOsdV2StepResultAsync(
        AgentConfig config,
        string bearerToken,
        string stepId,
        string status,
        string message,
        IReadOnlyDictionary<string, object?> data,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.RunId))
        {
            throw new InvalidOperationException("RunId is not configured.");
        }
        if (string.IsNullOrWhiteSpace(config.AgentId))
        {
            throw new InvalidOperationException("AgentId is not configured.");
        }

        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/osd/v2/agent/step/{stepId}/result")
        {
            Content = JsonContent.Create(new
            {
                run_id = config.RunId,
                agent_id = config.AgentId,
                phase = OsdV2WorkService.FullOsPhase,
                status,
                message,
                data,
            }),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            bearerToken);
        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
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

    public async Task<ArtifactUploadResponse> UploadArtifactAsync(
        AgentConfig config,
        string workItemId,
        string artifactKind,
        string filePath,
        IReadOnlyDictionary<string, object?> metadata,
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
        await using var stream = File.OpenRead(filePath);
        using var content = new MultipartFormDataContent();
        content.Add(new StringContent(workItemId), "work_item_id");
        content.Add(new StringContent(artifactKind), "artifact_kind");
        content.Add(
            new StringContent(JsonSerializer.Serialize(metadata)),
            "metadata_json");
        var fileContent = new StreamContent(stream);
        fileContent.Headers.ContentType = new MediaTypeHeaderValue("application/octet-stream");
        content.Add(fileContent, "file", Path.GetFileName(filePath));

        var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"{config.ServerUrl.TrimEnd('/')}/api/agent/v1/artifacts")
        {
            Content = content,
        };
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer",
            config.AgentToken);
        var response = await httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<ArtifactUploadResponse>(
            cancellationToken: cancellationToken)
            ?? throw new InvalidOperationException("Artifact upload response was empty.");
    }

    public async Task<JsonElement> PromoteSetupArtifactsAsync(
        AgentConfig config,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidOperationException("ServerUrl is not configured.");
        }
        var response = await httpClient.PostAsJsonAsync(
            $"{config.ServerUrl.TrimEnd('/')}/api/setup/v1/artifacts/promote",
            new Dictionary<string, object?>(),
            cancellationToken);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<JsonElement>(
            cancellationToken: cancellationToken);
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
    [property: JsonPropertyName("agent_token")] string? AgentToken,
    [property: JsonPropertyName("heartbeat_interval_seconds")] int HeartbeatIntervalSeconds,
    [property: JsonPropertyName("approval_status")] string? ApprovalStatus = null,
    [property: JsonPropertyName("poll_url")] string? PollUrl = null,
    [property: JsonPropertyName("retry_after_seconds")] int? RetryAfterSeconds = null);

public sealed record AgentWorkNextResponse(
    [property: JsonPropertyName("work_item")] AgentWorkItem? WorkItem);

public sealed record AgentWorkItem(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("request")] Dictionary<string, JsonElement> Request);

public sealed record OsdV2RegisterResponse(
    [property: JsonPropertyName("run_id")] string RunId,
    [property: JsonPropertyName("agent_id")] string AgentId,
    [property: JsonPropertyName("phase")] string Phase,
    [property: JsonPropertyName("bearer_token")] string BearerToken);

public sealed record OsdV2NextResponse(
    [property: JsonPropertyName("run_id")] string RunId,
    [property: JsonPropertyName("phase")] string Phase,
    [property: JsonPropertyName("actions")] IReadOnlyList<OsdV2Action> Actions,
    [property: JsonPropertyName("bearer_token")] string? BearerToken);

public sealed record OsdV2Action(
    [property: JsonPropertyName("step_id")] string StepId,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("phase")] string Phase,
    [property: JsonPropertyName("attempt")] int Attempt,
    [property: JsonPropertyName("retry_count")] int RetryCount,
    [property: JsonPropertyName("retry_delay_seconds")] int RetryDelaySeconds,
    [property: JsonPropertyName("timeout_seconds")] int? TimeoutSeconds,
    [property: JsonPropertyName("reboot_behavior")] string RebootBehavior,
    [property: JsonPropertyName("params")] Dictionary<string, JsonElement> Params,
    [property: JsonPropertyName("content")] IReadOnlyList<JsonElement> Content);

public sealed record HashUploadResponse(
    [property: JsonPropertyName("ok")] bool Ok,
    [property: JsonPropertyName("filename")] string Filename);

public sealed record ArtifactUploadResponse(
    [property: JsonPropertyName("ok")] bool Ok,
    [property: JsonPropertyName("artifact")] Dictionary<string, JsonElement> Artifact);

internal static class ThisAssembly
{
    public static string Version =>
        typeof(ThisAssembly).Assembly.GetName().Version?.ToString() ?? "0.1.0";
}
