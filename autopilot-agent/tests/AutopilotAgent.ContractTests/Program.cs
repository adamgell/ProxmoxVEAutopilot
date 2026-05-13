using System.Net;
using System.Text.Json;
using AutopilotAgent;

await AgentApiClientRegistersCloudOsdRunAsFullOsV2Agent();
VerifyDomainJoinMatcher();
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
