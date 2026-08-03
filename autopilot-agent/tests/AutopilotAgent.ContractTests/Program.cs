using System.Net;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json;
using AutopilotAgent;

await AgentApiClientRegistersCloudOsdRunAsFullOsV2Agent();
await AgentApiClientTreatsPendingBootstrapAsTokenless();
VerifyAgentUpdateCheckResponseContract();
await AgentApiClientPostsClaimableCapabilitiesOnHeartbeat();
VerifyDomainJoinMatcher();
VerifyOsDeployRoleAutomationContracts();
VerifyBuildHostContracts();
VerifyBuildHostVirtioRootsMatchOsDeployScript();
VerifyOsDeployOutputSelectionRejectsStaleManifests();
VerifyOsDeployResolvesStagedSourceMedia();
VerifySetupCmWorkContracts();
VerifySetupCmModulePublicationContracts();
VerifySetupCmDiagnosticsContracts();
VerifySetupCmContentLocationDiagnosticsContracts();
VerifySetupCmContentLocationRemediationContracts();
VerifySetupCmClientNetworkRepairContracts();
VerifySetupCmHealthClientTargetReconciliationContracts();
VerifySetupCmConsoleDomainAdminsContracts();
VerifySetupCmConsolePrincipalContracts();
VerifySetupCmConsoleConnectivityDiagnosticsContracts();
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

static void VerifyAgentUpdateCheckResponseContract()
{
    var updateJson = """
    {
      "schema_version": 1,
      "status": "upgrade_available",
      "published_version": "0.1.3",
      "runtime_identifier": "win-x64",
      "download_url": "/api/cloudosd/assets/autopilotagent.msi",
      "sha256": "abc123",
      "size_bytes": 4096
    }
    """;
    var update = JsonSerializer.Deserialize<AgentUpdateCheckResponse>(
        updateJson,
        AgentConfig.JsonOptions());
    Assert(update is not null, "update check response deserializes");
    Assert(update.Status == "upgrade_available", "update status preserved");
    Assert(update.DownloadUrl == "/api/cloudosd/assets/autopilotagent.msi", "download url preserved");
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

static void VerifyOsDeployRoleAutomationContracts()
{
    Assert(
        OsdV2WorkService.IsOsdV2Eligible(new AgentConfig
        {
            RunId = "run-osdeploy",
            Phase = "full_os",
            Role = "file_server",
        }),
        "OSDeploy full-OS agents must process OSD v2 role steps");
    Assert(
        OsdV2WorkService.SupportedKinds.Contains("configure_file_server_role"),
        "file server role step kind is not registered");
    Assert(
        OsdV2WorkService.SupportedKinds.Contains("join_domain_role"),
        "lab domain join role step kind is not registered");
    Assert(
        OsdV2WorkService.SupportedKinds.Contains("configure_isolated_domain_controller_role"),
        "isolated domain controller role step kind is not registered");
    Assert(
        OsdV2WorkService.SupportedKinds.Contains("verify_isolated_domain_controller_role"),
        "isolated domain controller verify step kind is not registered");
    Assert(
        OsdV2WorkService.SupportedKinds.Contains("configure_mecm_prereq_role"),
        "MECM prereq role step kind is not registered");

    var fileServerScript = OsDeployRoleWorkService.BuildFileServerScript(
        new Dictionary<string, JsonElement>
        {
            ["share_name"] = JsonSerializer.SerializeToElement("Shared"),
            ["share_path"] = JsonSerializer.SerializeToElement(@"C:\Shares\Shared"),
            ["full_access_principals"] = JsonSerializer.SerializeToElement(new[] { @"HOME\Domain Admins" }),
            ["change_access_principals"] = JsonSerializer.SerializeToElement(new[] { @"HOME\Domain Users" }),
            ["read_access_principals"] = JsonSerializer.SerializeToElement(Array.Empty<string>()),
        });
    Assert(fileServerScript.Contains("Install-WindowsFeature -Name FS-FileServer", StringComparison.Ordinal), "file server script must install FS-FileServer");
    Assert(fileServerScript.Contains("New-SmbShare", StringComparison.Ordinal), "file server script must create or update an SMB share");

    var joinScript = OsDeployRoleWorkService.BuildJoinDomainScript(
        new Dictionary<string, JsonElement>
        {
            ["domain_fqdn"] = JsonSerializer.SerializeToElement("lab.gell.one"),
            ["domain_join_username"] = JsonSerializer.SerializeToElement(@"LAB\joiner"),
            ["domain_join_password"] = JsonSerializer.SerializeToElement("secret"),
            ["domain_controller_ipv4"] = JsonSerializer.SerializeToElement("192.168.2.120"),
        });
    Assert(joinScript.Contains("Add-Computer -DomainName", StringComparison.Ordinal), "lab child domain join must call Add-Computer");
    Assert(joinScript.Contains("Set-DnsClientServerAddress", StringComparison.Ordinal), "lab child domain join must pin DNS to the isolated DC when provided");
    Assert(joinScript.Contains("will not be moved", StringComparison.Ordinal), "domain join must not move an already domain-joined server");

    var dcScript = OsDeployRoleWorkService.BuildIsolatedDomainControllerScript(
        new Dictionary<string, JsonElement>
        {
            ["forest_fqdn"] = JsonSerializer.SerializeToElement("lab.gell.one"),
            ["netbios_name"] = JsonSerializer.SerializeToElement("LAB"),
            ["forest_admin_username"] = JsonSerializer.SerializeToElement(@"LAB\Administrator"),
            ["forest_admin_password"] = JsonSerializer.SerializeToElement("secret"),
            ["dsrm_password"] = JsonSerializer.SerializeToElement("secret"),
        });
    Assert(dcScript.Contains("SetPassword", StringComparison.Ordinal), "DC role must set the local Administrator password before promotion");
    Assert(dcScript.Contains("Install-ADDSForest", StringComparison.Ordinal), "DC role must promote a new isolated forest");
    Assert(!dcScript.Contains("Add-Computer", StringComparison.Ordinal), "DC role must not join or mutate an existing domain");

    var replicaScript = OsDeployRoleWorkService.BuildIsolatedDomainControllerScript(
        new Dictionary<string, JsonElement>
        {
            ["dc_mode"] = JsonSerializer.SerializeToElement("additional_dc"),
            ["forest_fqdn"] = JsonSerializer.SerializeToElement("home.gell.one"),
            ["forest_admin_username"] = JsonSerializer.SerializeToElement(@"HOME\Administrator"),
            ["forest_admin_password"] = JsonSerializer.SerializeToElement("secret"),
            ["dsrm_password"] = JsonSerializer.SerializeToElement("secret"),
        });
    Assert(replicaScript.Contains("Install-ADDSDomainController", StringComparison.Ordinal), "additional_dc mode must promote a replica DC into the existing domain");
    Assert(!replicaScript.Contains("Install-ADDSForest", StringComparison.Ordinal), "additional_dc mode must not create a new forest");
    Assert(!replicaScript.Contains("SetPassword", StringComparison.Ordinal), "additional_dc mode must not reset the local Administrator");

    var mecmScript = OsDeployRoleWorkService.BuildMecmPrereqScript(
        new Dictionary<string, JsonElement>
        {
            ["prereq_profile"] = JsonSerializer.SerializeToElement("site_server_foundation"),
            ["content_root"] = JsonSerializer.SerializeToElement(@"C:\MECMContent"),
        });
    Assert(mecmScript.Contains("Web-WebServer", StringComparison.Ordinal), "MECM prereq script must include the IIS parent feature");
    Assert(mecmScript.Contains("Web-Common-Http", StringComparison.Ordinal), "MECM prereq script must include the IIS common HTTP baseline");
    Assert(mecmScript.Contains("Web-ISAPI-Ext", StringComparison.Ordinal), "MECM prereq script must include the management point ISAPI extension");
    Assert(mecmScript.Contains("Web-Metabase", StringComparison.Ordinal), "MECM prereq script must include IIS 6 metabase compatibility");
    Assert(!mecmScript.Contains("Web-Asp-Net45", StringComparison.Ordinal), "MECM prereq baseline must not add retired-role ASP.NET dependencies");
    Assert(!mecmScript.Contains("SQL", StringComparison.OrdinalIgnoreCase), "MECM prereq baseline must not install SQL");
    Assert(
        OsdV2WorkService.ShouldRequestReboot("required", "success"),
        "successful required-reboot OSD v2 steps must request a reboot");
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

static void VerifySetupCmWorkContracts()
{
    var kinds = SetupCmWorkService.SupportedKinds;
    foreach (var kind in new[]
    {
        "setup_cm_acquire",
        "setup_cm_sql",
        "setup_cm_mecm",
        "setup_cm_health",
        "setup_cm_client_install",
    })
    {
        Assert(kinds.Contains(kind), $"Setup-CM work kind is not registered: {kind}");
    }

    var valid = new Dictionary<string, JsonElement>
    {
        ["config_path"] = JsonSerializer.SerializeToElement(@"C:\ProgramData\SetupCm\labz1.local.yaml"),
        ["evidence_root"] = JsonSerializer.SerializeToElement(@"C:\ProgramData\SetupCm\artifacts"),
        ["module_archive_path"] = JsonSerializer.SerializeToElement(@"\\LABZ1-DC02\SetupCm\Modules\setup-cm.zip"),
        ["module_archive_sha256"] = JsonSerializer.SerializeToElement(new string('a', 64)),
    };
    var request = SetupCmWorkService.ValidateRequest("setup_cm_sql", valid);
    Assert(request.Stage == "Sql", "SQL work must invoke the Sql stage");
    Assert(request.ModuleArchiveSha256 == new string('a', 64), "archive hash must round-trip");

    AssertThrows<InvalidOperationException>(
        () => SetupCmWorkService.ValidateRequest(
            "setup_cm_sql",
            new Dictionary<string, JsonElement>(valid)
            {
                ["module_archive_path"] = JsonSerializer.SerializeToElement(@"C:\Windows\Temp\setup-cm.zip"),
            }),
        "Setup-CM work accepted an archive outside approved roots");
    AssertThrows<InvalidOperationException>(
        () => SetupCmWorkService.ValidateRequest(
            "setup_cm_sql",
            new Dictionary<string, JsonElement>(valid)
            {
                ["module_archive_sha256"] = JsonSerializer.SerializeToElement("not-a-sha256"),
            }),
        "Setup-CM work accepted an invalid archive hash");

    var clientValid = new Dictionary<string, JsonElement>
    {
        ["site_code"] = JsonSerializer.SerializeToElement("LAB"),
        ["management_point_fqdn"] = JsonSerializer.SerializeToElement("LABZ1-CM01.test.gell.one"),
        ["evidence_root"] = JsonSerializer.SerializeToElement(@"C:\ProgramData\SetupCm\artifacts"),
        ["module_archive_path"] = JsonSerializer.SerializeToElement(@"\\LABZ1-DC02\SetupCm\Modules\setup-cm.zip"),
        ["module_archive_sha256"] = JsonSerializer.SerializeToElement(new string('b', 64)),
    };
    var clientRequest = SetupCmWorkService.ValidateRequest("setup_cm_client_install", clientValid);
    Assert(clientRequest.Stage == "Client", "Client work must invoke the Client stage");
    AssertThrows<InvalidOperationException>(
        () => SetupCmWorkService.ValidateRequest(
            "setup_cm_client_install",
            new Dictionary<string, JsonElement>(clientValid)
            {
                ["management_point_fqdn"] = JsonSerializer.SerializeToElement("server.example.com"),
            }),
        "Client work accepted a non-LABZ1 management point");
    AssertThrows<InvalidOperationException>(
        () => SetupCmWorkService.ValidateRequest(
            "setup_cm_client_install",
            new Dictionary<string, JsonElement>(clientValid)
            {
                ["site_code"] = JsonSerializer.SerializeToElement("LABZ"),
            }),
        "Client work accepted a four-character site code");
    AssertThrows<InvalidOperationException>(
        () => SetupCmWorkService.ValidateRequest(
            "setup_cm_client_install",
            new Dictionary<string, JsonElement>(clientValid)
            {
                ["site_code"] = JsonSerializer.SerializeToElement("XYZ"),
            }),
        "Client work accepted a non-LAB site code");
    AssertThrows<InvalidOperationException>(
        () => SetupCmWorkService.ValidateRequest(
            "setup_cm_client_install",
            new Dictionary<string, JsonElement>(clientValid)
            {
                ["product_key"] = JsonSerializer.SerializeToElement("must-not-be-accepted"),
            }),
        "Client work accepted an unknown request field");

    var moduleRoot = Path.Combine(Path.GetTempPath(), $"setup-cm-module-{Guid.NewGuid():N}");
    Directory.CreateDirectory(Path.Combine(moduleRoot, "src", "SetupCm"));
    Directory.CreateDirectory(Path.Combine(moduleRoot, "scripts"));
    try
    {
        File.WriteAllText(Path.Combine(moduleRoot, "scripts", "Invoke-SetupCm.ps1"), "# entry point");
        File.WriteAllText(Path.Combine(moduleRoot, "src", "SetupCm", "SetupCm.psd1"), "# manifest");
        AssertThrows<InvalidOperationException>(
            () => SetupCmWorkService.ValidateExtractedModule(moduleRoot),
            "Setup-CM work accepted a module archive without the root module");
        File.WriteAllText(Path.Combine(moduleRoot, "src", "SetupCm", "SetupCm.psm1"), "# root module");
        SetupCmWorkService.ValidateExtractedModule(moduleRoot);
        AssertThrows<InvalidOperationException>(
            () => SetupCmWorkService.ValidateExtractedModule(moduleRoot, "Invoke-SetupCmClient.ps1"),
            "Setup-CM client work accepted a module archive without its entry point");
        File.WriteAllText(Path.Combine(moduleRoot, "scripts", "Invoke-SetupCmClient.ps1"), "# client entry point");
        SetupCmWorkService.ValidateExtractedModule(moduleRoot, "Invoke-SetupCmClient.ps1");
    }
    finally
    {
        Directory.Delete(moduleRoot, recursive: true);
    }
}

static void VerifySetupCmModulePublicationContracts()
{
    Assert(
        SetupCmModulePublishWorkService.SupportedKind == "publish_setup_cm_module",
        "Setup-CM module publication kind is not registered");

    var valid = new Dictionary<string, JsonElement>
    {
        ["artifact_id"] = JsonSerializer.SerializeToElement("00000000-0000-0000-0000-000000000001"),
        ["archive_sha256"] = JsonSerializer.SerializeToElement(new string('a', 64)),
        ["source_commit"] = JsonSerializer.SerializeToElement(new string('b', 40)),
    };
    var request = SetupCmModulePublishWorkService.ValidateRequest(valid);
    Assert(request.ArtifactId == "00000000-0000-0000-0000-000000000001", "publication artifact id did not round-trip");
    AssertThrows<InvalidOperationException>(
        () => SetupCmModulePublishWorkService.ValidateRequest(
            new Dictionary<string, JsonElement>(valid)
            {
                ["destination_path"] = JsonSerializer.SerializeToElement(@"C:\\Windows\\Temp\\setup-cm.zip"),
            }),
        "module publication accepted an arbitrary destination");

    var worker = File.ReadAllText(
        Path.Combine(
            Directory.GetCurrentDirectory(),
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "Worker.cs"));
    Assert(
        worker.Contains("SetupCmModulePublishWorkService.SupportedKind", StringComparison.Ordinal),
        "Agent worker does not route Setup-CM module publication work");

    var program = File.ReadAllText(
        Path.Combine(
            Directory.GetCurrentDirectory(),
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "Program.cs"));
    Assert(
        program.Contains("AddSingleton<SetupCmModulePublishWorkService>", StringComparison.Ordinal),
        "Agent host does not register Setup-CM module publication work");

    var archivePath = Path.Combine(Path.GetTempPath(), $"setup-cm-publication-{Guid.NewGuid():N}.zip");
    try
    {
        using (var archive = ZipFile.Open(archivePath, ZipArchiveMode.Create))
        {
            foreach (var entryName in new[]
            {
                "scripts/Invoke-SetupCm.ps1",
                "scripts/Invoke-SetupCmClient.ps1",
                "src/SetupCm/SetupCm.psd1",
                "src/SetupCm/SetupCm.psm1",
            })
            {
                using var writer = new StreamWriter(archive.CreateEntry(entryName).Open());
                writer.Write("# runtime");
            }
        }
        var hash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(archivePath))).ToLowerInvariant();
        var validated = SetupCmModulePublishWorkService.ValidateRequest(
            new Dictionary<string, JsonElement>(valid)
            {
                ["archive_sha256"] = JsonSerializer.SerializeToElement(hash),
            });
        SetupCmModulePublishWorkService.ValidateArchive(archivePath, validated);

        using (var archive = ZipFile.Open(archivePath, ZipArchiveMode.Update))
        {
            using var writer = new StreamWriter(archive.CreateEntry("../outside.txt").Open());
            writer.Write("blocked");
        }
        var unsafeHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(archivePath))).ToLowerInvariant();
        var unsafeRequest = SetupCmModulePublishWorkService.ValidateRequest(
            new Dictionary<string, JsonElement>(valid)
            {
                ["archive_sha256"] = JsonSerializer.SerializeToElement(unsafeHash),
            });
        AssertThrows<InvalidOperationException>(
            () => SetupCmModulePublishWorkService.ValidateArchive(archivePath, unsafeRequest),
            "module publication accepted a traversal ZIP entry");
    }
    finally
    {
        File.Delete(archivePath);
    }
}

static void VerifySetupCmDiagnosticsContracts()
{
    Assert(
        SetupCmDiagnosticsWorkService.SupportedKinds.Contains("setup_cm_diagnostics"),
        "Setup-CM source diagnostic kind is not registered");
    Assert(
        SetupCmDiagnosticsWorkService.SupportedKinds.Contains("setup_cm_source_access"),
        "Setup-CM source access remediation kind is not registered");
    Assert(
        SetupCmDiagnosticsWorkService.DiagnosticScriptResourceName
            == "AutopilotAgent.SetupCmSourceDiagnostics.ps1",
        "diagnostic work does not use the fixed packaged script");

    var valid = new Dictionary<string, JsonElement>
    {
        ["site_code"] = JsonSerializer.SerializeToElement("LAB"),
        ["target_computer_name"] = JsonSerializer.SerializeToElement("RING0IVY24-01"),
    };
    var request = SetupCmDiagnosticsWorkService.ValidateRequest(valid);
    Assert(request.SiteCode == "LAB", "diagnostic site code must round-trip");
    Assert(
        request.TargetComputerName == "RING0IVY24-01",
        "diagnostic target computer name must round-trip");

    AssertThrows<InvalidOperationException>(
        () => SetupCmDiagnosticsWorkService.ValidateRequest(
            new Dictionary<string, JsonElement>(valid)
            {
                ["site_code"] = JsonSerializer.SerializeToElement("lab"),
            }),
        "diagnostic work accepted a lower-case site code");
    AssertThrows<InvalidOperationException>(
        () => SetupCmDiagnosticsWorkService.ValidateRequest(
            new Dictionary<string, JsonElement>(valid)
            {
                ["target_computer_name"] = JsonSerializer.SerializeToElement(@"..\bad"),
            }),
        "diagnostic work accepted a path-like target computer name");
    AssertThrows<InvalidOperationException>(
        () => SetupCmDiagnosticsWorkService.ValidateRequest(
            new Dictionary<string, JsonElement>(valid)
            {
                ["script"] = JsonSerializer.SerializeToElement("must-not-be-accepted"),
            }),
        "diagnostic work accepted an arbitrary script field");
}

static void VerifySetupCmContentLocationDiagnosticsContracts()
{
    Assert(
        SetupCmDiagnosticsWorkService.SupportedKinds.Contains(
            "setup_cm_content_location_diagnostics"),
        "Setup-CM content location diagnostic kind is not registered");
    Assert(
        SetupCmDiagnosticsWorkService.ContentLocationDiagnosticScriptResourceName
            == "AutopilotAgent.SetupCmContentLocationDiagnostics.ps1",
        "content location work does not use the fixed packaged script");
    var diagnosticScript = File.ReadAllText(
        Path.Combine(
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "SetupCmContentLocationDiagnostics.ps1"));
    Assert(
        diagnosticScript.Contains("$clientSubnetValue = $clientSubnet.Split('/')[0]", StringComparison.Ordinal),
        "content location diagnostics did not normalize the CIDR for SMS_Boundary matching");
    Assert(
        diagnosticScript.Contains("-eq $clientSubnetValue", StringComparison.Ordinal),
        "content location diagnostics compared the SMS_Boundary value to the unnormalized CIDR");

    var valid = new Dictionary<string, JsonElement>
    {
        ["site_code"] = JsonSerializer.SerializeToElement("LAB"),
        ["target_computer_name"] = JsonSerializer.SerializeToElement("RING0IVY24-01"),
        ["client_ipv4"] = JsonSerializer.SerializeToElement("192.168.16.103"),
    };
    var request = SetupCmDiagnosticsWorkService.ValidateContentLocationRequest(valid);
    Assert(request.SiteCode == "LAB", "content location site code must round-trip");
    Assert(
        request.TargetComputerName == "RING0IVY24-01",
        "content location target computer name must round-trip");
    Assert(request.ClientIpv4 == "192.168.16.103", "client IPv4 must round-trip");

    AssertThrows<InvalidOperationException>(
        () => SetupCmDiagnosticsWorkService.ValidateContentLocationRequest(
            new Dictionary<string, JsonElement>(valid)
            {
                ["client_ipv4"] = JsonSerializer.SerializeToElement("not-an-ip"),
            }),
        "content location diagnostics accepted a non-IPv4 client address");
    AssertThrows<InvalidOperationException>(
        () => SetupCmDiagnosticsWorkService.ValidateContentLocationRequest(
            new Dictionary<string, JsonElement>(valid)
            {
                ["script"] = JsonSerializer.SerializeToElement("Get-ChildItem"),
            }),
        "content location diagnostics accepted an arbitrary script field");
}

static void VerifySetupCmContentLocationRemediationContracts()
{
    Assert(
        SetupCmDiagnosticsWorkService.SupportedKinds.Contains(
            "setup_cm_content_location_remediation"),
        "Setup-CM content location remediation kind is not registered");
    Assert(
        SetupCmDiagnosticsWorkService.ContentLocationRemediationScriptResourceName
            == "AutopilotAgent.SetupCmContentLocationRemediation.ps1",
        "content location remediation does not use the fixed packaged script");
    var remediationScript = File.ReadAllText(
        Path.Combine(
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "SetupCmContentLocationRemediation.ps1"));
    Assert(
        remediationScript.Contains(
            "$modulePath = Join-Path (Split-Path -Parent $adminUiPath) 'ConfigurationManager.psd1'",
            StringComparison.Ordinal),
        "content remediation did not resolve ConfigurationManager.psd1 from AdminConsole bin");
    Assert(
        remediationScript.Contains(
            "New-PSDrive -Name $SiteCode -PSProvider CMSite -Root $DistributionPointFqdn",
            StringComparison.Ordinal),
        "content remediation did not create the fixed CMSite drive for a non-console session");
    Assert(
        remediationScript.Contains(
            "New-PSDrive -Name $SiteCode -PSProvider CMSite -Root $DistributionPointFqdn -ErrorAction Stop",
            StringComparison.Ordinal),
        "content remediation used unsupported CMSite drive parameters");
    Assert(
        !remediationScript.Contains("-Description $BoundaryGroupName", StringComparison.Ordinal),
        "content remediation passed an unsupported Description parameter to New-CMBoundary");
    Assert(
        remediationScript.Contains("$boundaryValue = $ClientSubnet.Split('/')[0]", StringComparison.Ordinal),
        "content remediation did not normalize the requested CIDR for SMS_Boundary readback");
    Assert(
        remediationScript.Contains("Value = '$boundaryValue'", StringComparison.Ordinal),
        "content remediation queried SMS_Boundary with the unnormalized CIDR value");
    Assert(
        remediationScript.Contains("(?i)Display=", StringComparison.Ordinal),
        "content remediation did not parse case-variant ServerNALPath Display keys");

    var valid = new Dictionary<string, JsonElement>
    {
        ["site_code"] = JsonSerializer.SerializeToElement("LAB"),
        ["client_subnet"] = JsonSerializer.SerializeToElement("192.168.16.0/24"),
        ["boundary_group_name"] = JsonSerializer.SerializeToElement("LABZ1 Client Network"),
        ["distribution_point_fqdn"] = JsonSerializer.SerializeToElement("LABZ1-CM01.test.gell.one"),
    };
    var request = SetupCmDiagnosticsWorkService.ValidateContentLocationRemediationRequest(valid);
    Assert(request.SiteCode == "LAB", "content remediation site code must round-trip");
    Assert(request.ClientSubnet == "192.168.16.0/24", "content remediation subnet must round-trip");
    Assert(
        request.BoundaryGroupName == "LABZ1 Client Network",
        "content remediation group must round-trip");
    Assert(
        request.DistributionPointFqdn == "LABZ1-CM01.test.gell.one",
        "content remediation DP must round-trip");
    Assert(
        SetupCmDiagnosticsWorkService.ExtractContentLocationDistributionPointHost(
            "[\"Display=\\\\LABZ1-CM01.test.gell.one\"]MSWNET")
            == "labz1-cm01.test.gell.one",
        "content remediation did not extract the exact DP host from ServerNALPath");
    Assert(
        SetupCmDiagnosticsWorkService.ExtractContentLocationDistributionPointHost(
            "[\"Display=\\\\prefixLABZ1-CM01.test.gell.one\"]MSWNET")
            != "labz1-cm01.test.gell.one",
        "content remediation accepted a prefixed DP host");
    Assert(
        SetupCmDiagnosticsWorkService.ExtractContentLocationDistributionPointHost(
            "[\"Display=\\\\LABZ1-CM01.test.gell.one.evil.test\"]MSWNET")
            != "labz1-cm01.test.gell.one",
        "content remediation accepted a suffixed DP host");
    Assert(
        SetupCmDiagnosticsWorkService.NormalizeContentLocationBoundaryValue("192.168.16.0/24")
            == "192.168.16.0",
        "content remediation did not normalize the CIDR request for strict boundary readback");
    var failure = SetupCmDiagnosticsWorkService.FormatContentLocationRemediationFailure(
        exitCode: 1,
        stdout: "partial script output",
        stderr: "module load failed");
    Assert(
        failure.Contains("exit code 1", StringComparison.Ordinal)
        && failure.Contains("module load failed", StringComparison.Ordinal)
        && failure.Contains("partial script output", StringComparison.Ordinal),
        "content remediation failure omitted bounded PowerShell evidence");

    AssertThrows<InvalidOperationException>(
        () => SetupCmDiagnosticsWorkService.ValidateContentLocationRemediationRequest(
            new Dictionary<string, JsonElement>(valid)
            {
                ["client_subnet"] = JsonSerializer.SerializeToElement("10.0.0.0/24"),
            }),
        "content remediation accepted a foreign subnet");
    AssertThrows<InvalidOperationException>(
        () => SetupCmDiagnosticsWorkService.ValidateContentLocationRemediationRequest(
            new Dictionary<string, JsonElement>(valid)
            {
                ["script"] = JsonSerializer.SerializeToElement("Get-ChildItem"),
            }),
        "content remediation accepted an arbitrary script field");
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

static void VerifyBuildHostVirtioRootsMatchOsDeployScript()
{
    var root = Directory.GetCurrentDirectory();
    var buildHostWorker = File.ReadAllText(
        Path.Combine(
            root,
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "BuildHostWorkService.cs"));
    var osDeployScript = File.ReadAllText(
        Path.Combine(
            root,
            "autopilot-proxmox",
            "tools",
            "osdeploy-build",
            "build-osdeploy.ps1"));
    var stagingScript = SourceBetween(
        buildHostWorker,
        "private async Task<string> StageVirtioDriversAsync",
        "private async Task<Dictionary<string, object?>> BuildWinPeAsync");
    var preflightScript = SourceBetween(
        buildHostWorker,
        "function Test-VirtioInput",
        "function Resolve-SourceMedia");

    foreach (var rootCandidate in new[]
    {
        @"E:\BuildRoot\inputs\virtio-win",
        @"E:\BuildRoot\inputs\virtio",
        @"E:\",
        @"F:\BuildRoot\inputs\virtio-win",
        @"F:\BuildRoot\inputs\virtio",
        @"F:\",
    })
    {
        Assert(
            osDeployScript.Contains(rootCandidate, StringComparison.Ordinal),
            $"OSDeploy script no longer accepts VirtIO root: {rootCandidate}");
        Assert(
            stagingScript.Contains(rootCandidate, StringComparison.Ordinal),
            $"build-host VirtIO staging is missing OSDeploy VirtIO root: {rootCandidate}");
        Assert(
            preflightScript.Contains(rootCandidate, StringComparison.Ordinal),
            $"build-host preflight is missing OSDeploy VirtIO root: {rootCandidate}");
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

static void VerifyOsDeployResolvesStagedSourceMedia()
{
    var root = Path.Combine(Path.GetTempPath(), $"osdeploy-media-{Guid.NewGuid():N}");
    var mediaDir = Path.Combine(root, "inputs", "media");
    Directory.CreateDirectory(mediaDir);
    try
    {
        Assert(
            BuildHostWorkService.ResolveStagedSourceMediaIso([mediaDir]) is null,
            "no staged ISO should resolve to null");

        var older = Path.Combine(mediaDir, "old-server.iso");
        var newer = Path.Combine(mediaDir, "en-us_windows_server_2022.iso");
        File.WriteAllText(older, "old");
        File.WriteAllText(newer, "new");
        File.SetLastWriteTimeUtc(older, DateTime.UtcNow.AddHours(-2));
        File.SetLastWriteTimeUtc(newer, DateTime.UtcNow);

        Assert(
            BuildHostWorkService.ResolveStagedSourceMediaIso([mediaDir]) == newer,
            "staged source media should resolve to the newest ISO");

        Assert(
            BuildHostWorkService
                .OsDeploySourceMediaDirectories(@"C:\BuildRoot\ProxmoxVEAutopilot")
                .Contains(@"C:\BuildRoot\ProxmoxVEAutopilot\inputs\media"),
            "source media search dirs must include inputs\\media under the work root");
    }
    finally
    {
        Directory.Delete(root, recursive: true);
    }
}

static void VerifySetupCmClientNetworkRepairContracts()
{
    Assert(
        SetupCmDiagnosticsWorkService.SupportedKinds.Contains(
            "setup_cm_client_network_repair"),
        "Setup-CM client network repair kind is not registered");
    Assert(
        SetupCmDiagnosticsWorkService.ClientNetworkRepairScriptResourceName
            == "AutopilotAgent.SetupCmClientNetworkRepair.ps1",
        "client network repair does not use the fixed packaged script");

    var repairScript = File.ReadAllText(
        Path.Combine(
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "SetupCmClientNetworkRepair.ps1"));
    foreach (var value in new[]
    {
        "BC-24-11-9C-43-E6",
        "192.168.16.103",
        "192.168.16.1",
        "192.168.16.12",
        "LABZ1-DC02.test.gell.one",
    })
    {
        Assert(
            repairScript.Contains(value, StringComparison.Ordinal),
            $"client network repair is missing fixed value: {value}");
    }
}

static void VerifySetupCmHealthClientTargetReconciliationContracts()
{
    Assert(
        SetupCmDiagnosticsWorkService.SupportedKinds.Contains(
            "setup_cm_health_client_target_reconciliation"),
        "Setup-CM health client target reconciliation kind is not registered");

    var reconciliationScript = Path.Combine(
        "autopilot-agent",
        "src",
        "AutopilotAgent",
        "SetupCmHealthClientTargetReconciliation.ps1");
    Assert(
        File.Exists(reconciliationScript),
        "Setup-CM health client target reconciliation script is missing");

    var source = File.ReadAllText(reconciliationScript);
    foreach (var value in new[]
    {
        @"C:\ProgramData\SetupCm\labz1.local.yaml",
        "testClient:",
        "LABZ1-CMCLIENT01",
        "RING0IVY24-01",
        "previous_client_name",
        "client_name",
        "TrimStart([char]0xFEFF)",
        "testClient block lines",
    })
    {
        Assert(
            source.Contains(value, StringComparison.Ordinal),
            $"health client target reconciliation is missing fixed value: {value}");
    }
}

static void VerifySetupCmConsoleDomainAdminsContracts()
{
    Assert(
        SetupCmDiagnosticsWorkService.SupportedKinds.Contains(
            "setup_cm_console_domain_admins"),
        "Setup-CM console Domain Admins kind is not registered");

    Assert(
        SetupCmDiagnosticsWorkService.ConsoleDomainAdminsScriptResourceName
            == "AutopilotAgent.SetupCmConsoleDomainAdmins.ps1",
        "Setup-CM console Domain Admins does not use the fixed packaged script");

    var script = Path.Combine(
        "autopilot-agent",
        "src",
        "AutopilotAgent",
        "SetupCmConsoleDomainAdmins.ps1");
    Assert(File.Exists(script), "Setup-CM console Domain Admins script is missing");

    var source = File.ReadAllText(script);
    foreach (var value in new[]
    {
        "Domain Admins",
        "Full Administrator",
        "Default",
        "Add-CMSecurityRoleToAdministrativeUser",
        "Add-CMSecurityScopeToAdministrativeUser",
        "SMS Admins",
        "MachineLaunchRestriction",
        "DefaultLaunchPermission",
        "[System.Security.AccessControl.AceFlags]::None",
        "sms_admins_membership",
        "machine_launch_remote_activation",
        "default_launch_remote_activation",
        "full_administrator",
        "default_scope",
    })
    {
        Assert(
            source.Contains(value, StringComparison.Ordinal),
            $"console Domain Admins script is missing fixed value: {value}");
    }
}

static void VerifySetupCmConsolePrincipalContracts()
{
    Assert(
        SetupCmDiagnosticsWorkService.SupportedKinds.Contains("setup_cm_console_principal"),
        "Setup-CM direct console principal repair kind is not registered");
    Assert(
        SetupCmDiagnosticsWorkService.ConsolePrincipalScriptResourceName
            == "AutopilotAgent.SetupCmConsolePrincipal.ps1",
        "Setup-CM direct console principal repair does not use the fixed packaged script");
    var request = SetupCmDiagnosticsWorkService.ValidateConsolePrincipalRequest(
        new Dictionary<string, JsonElement>
        {
            ["principal"] = JsonSerializer.SerializeToElement(@"TEST\adam"),
        });
    Assert(request.Principal == @"TEST\adam", "direct console principal must round-trip");
    AssertThrows<InvalidOperationException>(
        () => SetupCmDiagnosticsWorkService.ValidateConsolePrincipalRequest(
            new Dictionary<string, JsonElement>
            {
                ["principal"] = JsonSerializer.SerializeToElement("adam"),
            }),
        "direct console principal accepted an unqualified account");
    AssertThrows<InvalidOperationException>(
        () => SetupCmDiagnosticsWorkService.ValidateConsolePrincipalRequest(
            new Dictionary<string, JsonElement>
            {
                ["principal"] = JsonSerializer.SerializeToElement(@"TEST\adam"),
                ["extra"] = JsonSerializer.SerializeToElement("must-not-be-accepted"),
            }),
        "direct console principal accepted an arbitrary request field");
    var principalScript = File.ReadAllText(
        Path.Combine(
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "SetupCmConsolePrincipal.ps1"));
    foreach (var value in new[]
    {
        "Get-CMAdministrativeUser",
        "New-CMAdministrativeUser",
        "Full Administrator",
        "Default",
        "principal",
    })
    {
        Assert(
            principalScript.Contains(value, StringComparison.Ordinal),
            $"direct console principal repair is missing fixed value: {value}");
    }
}

static void VerifySetupCmConsoleConnectivityDiagnosticsContracts()
{
    Assert(
        SetupCmDiagnosticsWorkService.SupportedKinds.Contains(
            "setup_cm_console_connectivity_diagnostics"),
        "Setup-CM console connectivity diagnostic kind is not registered");
    Assert(
        SetupCmDiagnosticsWorkService.ConsoleConnectivityDiagnosticScriptResourceName
            == "AutopilotAgent.SetupCmConsoleConnectivityDiagnostics.ps1",
        "console connectivity diagnostics do not use the fixed packaged script");

    var diagnosticScript = File.ReadAllText(
        Path.Combine(
            "autopilot-agent",
            "src",
            "AutopilotAgent",
            "SetupCmConsoleConnectivityDiagnostics.ps1"));
    Assert(
        diagnosticScript.Contains("MachineLaunchRestriction", StringComparison.Ordinal),
        "console connectivity diagnostics do not inspect the DCOM machine launch restriction");
    Assert(
        diagnosticScript.Contains("Get-CMAdministrativeUser", StringComparison.Ordinal),
        "console connectivity diagnostics do not read Configuration Manager RBAC");
    Assert(
        diagnosticScript.Contains("DistributedCOM", StringComparison.Ordinal),
        "console connectivity diagnostics do not collect recent DCOM denial evidence");
    Assert(
        diagnosticScript.Contains("sms_admins_remote_activation", StringComparison.Ordinal),
        "console connectivity diagnostics do not verify the effective SMS Admins DCOM path");
    Assert(
        diagnosticScript.Contains("SMSAdminUI.log", StringComparison.Ordinal),
        "console connectivity diagnostics do not collect the Configuration Manager console log");
    Assert(
        diagnosticScript.Contains("SMSProv.log", StringComparison.Ordinal),
        "console connectivity diagnostics do not collect the SMS Provider log");
    Assert(
        diagnosticScript.Contains("WindowsIdentity", StringComparison.Ordinal),
        "console connectivity diagnostics do not inspect the running console process token");
    Assert(
        diagnosticScript.Contains("Microsoft.ConfigurationManagement", StringComparison.Ordinal),
        "console connectivity diagnostics do not target the Configuration Manager console process");
}

static void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

static string SourceBetween(string source, string startMarker, string endMarker)
{
    var start = source.IndexOf(startMarker, StringComparison.Ordinal);
    Assert(start >= 0, $"source is missing start marker: {startMarker}");
    var end = source.IndexOf(endMarker, start, StringComparison.Ordinal);
    Assert(end > start, $"source is missing end marker after {startMarker}: {endMarker}");
    return source[start..end];
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
