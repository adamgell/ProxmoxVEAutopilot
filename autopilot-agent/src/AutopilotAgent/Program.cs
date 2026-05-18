using AutopilotAgent;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

var builder = Host.CreateDefaultBuilder(args)
    .UseWindowsService(options =>
    {
        options.ServiceName = "AutopilotAgent";
    })
    .ConfigureServices(services =>
    {
        services.AddSingleton<AgentFileLog>();
        services.AddSingleton<TelemetryCollector>();
        services.AddSingleton<HashCaptureService>();
        services.AddSingleton<OsDeployRoleWorkService>();
        services.AddSingleton<OsdV2WorkService>();
        services.AddSingleton<BuildHostWorkService>();
        services.AddHttpClient<AgentApiClient>(client =>
        {
            client.Timeout = TimeSpan.FromHours(12);
        });
        services.AddHostedService<Worker>();
    });

await builder.Build().RunAsync();
