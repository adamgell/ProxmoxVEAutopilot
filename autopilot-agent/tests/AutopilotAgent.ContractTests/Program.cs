using System.Net;
using System.Text.Json;
using AutopilotAgent;

await AgentApiClientRegistersCloudOsdRunAsFullOsV2Agent();
await AgentApiClientTreatsPendingBootstrapAsTokenless();
await AgentApiClientPostsClaimableCapabilitiesOnHeartbeat();
VerifyDomainJoinMatcher();
VerifyBuildHostContracts();
VerifyOsDeployOutputSelectionRejectsStaleManifests();
Console.WriteLine("AutopilotAgent contract tests passed.");

static async Task AgentApiClientRegistersCloudOsdRunAsFullOsV2Agent()
{
    using var http = new HttpClient(new RecordingHandler(async request =>
    {
        Assert(request.Method == HttpMethod.Post, "v2 register should use POST");
        Assert(request.RequestUri?.AbsolutePath == "/osd/v2/agent/register", "unexpected v2 register path");
        Assert(request.Headers.Authorization is null, "v2 register should not use the v1 agent token");

        var json = await request.Content!.ReadAsStringAsync();
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        Assert(root.GetProperty("run_id").GetString() == "run-123", "run id was not posted");
        Assert(root.GetProperty("agent_id").GetString() == "agent-cloudosd", "agent id was not posted");
        Assert(root.GetProperty("phase").GetString() == "full_os", "CloudOSD should register as full_os for v2");
        Assert(root.GetProperty("computer_name").GetString() == "GELL-123-AD", "computer name was not posted");
        Assert(
            root.GetProperty("capabilities").EnumerateArray().Any(item => item.GetString() == "capture_autopilot_hash"),
            "supported v2 capability was not posted");

        return JsonSerializer.Serialize(new
        {
            run_id = "run-123",
            agent_id = "agent-cloudosd",
            phase = "full_os",
            bearer_token = "v2-run-token",
        });
    }))
    {
        BaseAddress = new Uri("https://autopilot.test"),
    };

    var api = new AgentApiClient(http);
    var config = new AgentConfig
    {
        ServerUrl = "https://autopilot.test/",
        AgentId = "agent-cloudosd",
        AgentToken = "v1-token",
        RunId = "run-123",
        Phase = "cloudosd",
    };
    var telemetry = Snapshot(domainName: "home.gell.one", domainJoined: true);

    var registered = await api.RegisterOsdV2AgentAsync(
        config,
        telemetry,
        ["capture_autopilot_hash"],
        CancellationToken.None);

    Assert(registered.BearerToken == "v2-run-token", "v2 bearer token was not returned");
    Assert(registered.Phase == "full_os", "v2 registration phase was not normalized");
}

static async Task AgentApiClientTreatsPendingBootstrapAsTokenless()
{
    using var http = new HttpClient(new RecordingHandler(async request =>
    {
        Assert(request.Method == HttpMethod.Post, "bootstrap should use POST");
        Assert(request.RequestUri?.AbsolutePath == "/api/agent/v1/bootstrap", "unexpected bootstrap path");
        Assert(request.Headers.Authorization?.Parameter == "fleet-bootstrap", "bootstrap bearer was not used");

        var json = await request.Content!.ReadAsStringAsync();
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        Assert(root.GetProperty("agent_id").GetString() == "buildhost-100", "agent id was not posted");
        Assert(root.GetProperty("phase").GetString() == "build-host", "build-host phase was not posted");

        return JsonSerializer.Serialize(new
        {
            schema_version = 1,
            agent_id = "buildhost-100",
            approval_status = "pending",
            poll_url = "/api/agent/v1/bootstrap/claim/approval-1",
            retry_after_seconds = 5,
        });
    }))
    {
        BaseAddress = new Uri("https://autopilot.test"),
    };

    var api = new AgentApiClient(http);
    var pending = await api.BootstrapAsync(
        new AgentConfig
        {
            ServerUrl = "https://autopilot.test/",
            AgentId = "buildhost-100",
            BootstrapToken = "fleet-bootstrap",
            Phase = "build-host",
        },
        Snapshot(domainName: "WORKGROUP", domainJoined: false),
        CancellationToken.None);

    Assert(pending.AgentToken is null, "pending bootstrap must not produce an agent token");
    Assert(pending.ApprovalStatus == "pending", "pending approval status did not deserialize");
    Assert(pending.RetryAfterSeconds == 5, "pending retry delay did not deserialize");
}

static void VerifyDomainJoinMatcher()
{
    var telemetry = Snapshot(domainName: "home.gell.one", domainJoined: true);
    Assert(
        OsdV2WorkService.IsDomainJoinSatisfied(
            telemetry,
            new Dictionary<string, JsonElement>
            {
                ["acceptable_domain_names"] = JsonSerializer.SerializeToElement(
                    new[] { "HOME", "home.gell.one" }),
            }),
        "expected accepted FQDN to match");
    Assert(
        !OsdV2WorkService.IsDomainJoinSatisfied(
            Snapshot(domainName: "WORKGROUP", domainJoined: false),
            new Dictionary<string, JsonElement>
            {
                ["domain_fqdn"] = JsonSerializer.SerializeToElement("home.gell.one"),
            }),
        "workgroup telemetry must not satisfy domain join");
}

static async Task AgentApiClientPostsClaimableCapabilitiesOnHeartbeat()
{
    using var http = new HttpClient(new RecordingHandler(async request =>
    {
        Assert(request.Method == HttpMethod.Post, "heartbeat should use POST");
        Assert(request.RequestUri?.AbsolutePath == "/api/agent/v1/heartbeat", "unexpected heartbeat path");

        var json = await request.Content!.ReadAsStringAsync();
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        Assert(
            root.GetProperty("capabilities").EnumerateArray()
                .Any(item => item.GetString() == "configure_build_host_role"),
            "heartbeat did not post build-host activation capability");

        return JsonSerializer.Serialize(new
        {
            status = "ok",
            heartbeat_interval_seconds = 30,
        });
    }))
    {
        BaseAddress = new Uri("https://autopilot.test"),
    };

    var api = new AgentApiClient(http);
    await api.SendHeartbeatAsync(
        new AgentConfig
        {
            ServerUrl = "https://autopilot.test/",
            AgentId = "agent-builder",
            AgentToken = "agent-token",
            Phase = "bootstrap",
        },
        Snapshot(domainName: "WORKGROUP", domainJoined: false),
        ["capture_autopilot_hash", "configure_build_host_role"],
        CancellationToken.None);
}

static void VerifyBuildHostContracts()
{
    var supported = BuildHostWorkService.SupportedKinds.ToHashSet(StringComparer.Ordinal);
    foreach (var kind in new[]
    {
        "install_build_prerequisites",
        "fetch_source_bundle",
        "build_agent_msi",
        "build_winpe",
        "build_cloudosd",
        "build_osdeploy",
        "publish_artifacts",
    })
    {
        Assert(supported.Contains(kind), $"build-host work kind is not registered: {kind}");
    }

    var config = new AgentConfig
    {
        Role = "build-host",
        Capabilities = ["build_agent_msi", "build_cloudosd", "build_osdeploy"],
    };
    var json = JsonSerializer.Serialize(config, AgentConfig.JsonOptions());
    var roundTrip = JsonSerializer.Deserialize<AgentConfig>(
        json,
        AgentConfig.JsonOptions());
    Assert(roundTrip is not null, "build-host config did not deserialize");
    Assert(roundTrip?.Role == "build-host", "build-host role did not round-trip");
    Assert(
        roundTrip!.Capabilities.Contains("build_agent_msi"),
        "build-host capabilities did not round-trip");
    Assert(
        roundTrip!.Capabilities.Contains("build_osdeploy"),
        "build-host OSDeploy capability did not round-trip");

    var program = File.ReadAllText(
        Path.Combine(
            Directory.GetCurrentDirectory(),
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "Program.cs"));
    Assert(
        program.Contains("client.Timeout = TimeSpan.FromHours(12);", StringComparison.Ordinal),
        "agent HTTP client timeout must allow large build artifact uploads");

    var worker = File.ReadAllText(
        Path.Combine(
            Directory.GetCurrentDirectory(),
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "Worker.cs"));
    foreach (var fragment in new[]
    {
        "string.IsNullOrWhiteSpace(bootstrap.AgentToken)",
        "Bootstrap approval pending",
        "bootstrap.RetryAfterSeconds",
    })
    {
        Assert(
            worker.Contains(fragment, StringComparison.Ordinal),
            $"agent worker is missing pending bootstrap handling: {fragment}");
    }

    var buildHostWorker = File.ReadAllText(
        Path.Combine(
            Directory.GetCurrentDirectory(),
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "BuildHostWorkService.cs"));
    foreach (var fragment in new[]
    {
        "configure_build_host_role",
        "config.Phase = \"build-host\"",
        "config.Role = \"build-host\"",
        "config.Save()",
        "source_media_path",
        "SourceMediaPath",
        "OSDeploy source_media_path was not provided and no mounted Windows setup media was found.",
        "sources\\install.esd",
        "$osDeployArgs.NativeMediaBuild = $true",
        "$osDeployArgs.SourceMediaPath = $sourceMediaPath",
        "ImageName =",
        "ImageIndex =",
        "OSVersion =",
        "OSEdition =",
        "OSLanguage =",
        "controller_url",
        "fallback_controller_url",
        "ControllerUrl =",
        "$osDeployArgs.FallbackControllerUrl = $fallbackControllerUrl",
        "FetchSourceBundleAsync(config, work, cancellationToken);",
        "Invoke-BoundedPowerShell -Name 'install-nuget-provider' -TimeoutSeconds 600",
        "$startInfo.Arguments =",
        "ReadToEndAsync()",
        "Install-PackageProvider -Name NuGet -MinimumVersion '2.8.5.201' -ForceBootstrap",
        "Invoke-BoundedPowerShell -Name \"install-module-$($moduleSpec.Name)\" -TimeoutSeconds 2700",
        "Install-Module -Name '$($moduleSpec.Name)' -RequiredVersion '$($moduleSpec.RequiredVersion)' -Scope AllUsers -Force -AllowClobber",
        "OSDeploy build completed without producing an ISO",
        "RunBuildHostPreflightAsync",
        "osdeploy_build_host_preflight",
        "ADK Deployment Tools",
        "WinPE add-on",
        "oscdimg.exe",
        "copype.cmd",
        "VirtIO input",
        "source media",
        "[\"preflight\"] = preflight",
    })
    {
        Assert(
            buildHostWorker.Contains(fragment, StringComparison.Ordinal),
            $"build-host OSDeploy worker is missing contract fragment: {fragment}");
    }
}

static void VerifyOsDeployOutputSelectionRejectsStaleManifests()
{
    var root = Path.Combine(Path.GetTempPath(), $"osdeploy-output-{Guid.NewGuid():N}");
    Directory.CreateDirectory(root);
    try
    {
        var oldWim = Path.Combine(root, "osdeploy-server-amd64-old.wim");
        var oldIso = Path.Combine(root, "osdeploy-server-amd64-old.iso");
        var oldManifest = Path.Combine(root, "osdeploy-server-amd64-old.json");
        File.WriteAllText(oldWim, "old-wim");
        File.WriteAllText(oldIso, "old-iso");
        File.WriteAllText(
            oldManifest,
            JsonSerializer.Serialize(new
            {
                output_wim = oldWim,
                output_iso = oldIso,
            }));
        File.SetLastWriteTimeUtc(oldManifest, DateTime.UtcNow.AddHours(-1));

        AssertThrows<InvalidOperationException>(
            () => BuildHostWorkService.SelectOsDeployBuildOutputs(
                root,
                stdout: "",
                buildStartedUtc: DateTime.UtcNow).ToArray(),
            "OSDeploy output selection accepted a stale manifest");

        var newWim = Path.Combine(root, "osdeploy-server-amd64-new.wim");
        var newIso = Path.Combine(root, "osdeploy-server-amd64-new.iso");
        var newManifest = Path.Combine(root, "osdeploy-server-amd64-new.json");
        File.WriteAllText(newWim, "new-wim");
        File.WriteAllText(newIso, "new-iso");
        File.WriteAllText(
            newManifest,
            JsonSerializer.Serialize(new
            {
                output_wim = newWim,
                output_iso = newIso,
            }));

        var selected = BuildHostWorkService.SelectOsDeployBuildOutputs(
            root,
            stdout: $"noise{Environment.NewLine}{newManifest}{Environment.NewLine}",
            buildStartedUtc: DateTime.UtcNow).ToArray();
        Assert(selected.SequenceEqual([newManifest, newWim, newIso]), "OSDeploy output selection ignored the printed manifest path");
    }
    finally
    {
        Directory.Delete(root, recursive: true);
    }
}


static TelemetrySnapshot Snapshot(string? domainName, bool? domainJoined) => new(
    "GELL-123-AD",
    "SERIAL-123",
    "192.168.2.123",
    ["192.168.2.123"],
    [],
    "Microsoft Windows 11 Enterprise",
    "10.0.26100",
    "26100",
    "2026-05-13T18:00:00Z",
    600,
    "QEMU-GA",
    "Running",
    domainName,
    domainJoined,
    false,
    null);

static void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

static void AssertThrows<TException>(Action action, string message)
    where TException : Exception
{
    try
    {
        action();
    }
    catch (TException)
    {
        return;
    }
    throw new InvalidOperationException(message);
}

internal sealed class RecordingHandler(
    Func<HttpRequestMessage, Task<string>> callback) : HttpMessageHandler
{
    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        var body = await callback(request);
        return new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(body),
        };
    }
}
