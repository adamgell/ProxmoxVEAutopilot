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
        services.AddHttpClient<AgentApiClient>();
        services.AddHostedService<Worker>();
    });

await builder.Build().RunAsync();
