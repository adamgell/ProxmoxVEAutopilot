using Xunit;
using Autopilot.Launcher;

namespace Autopilot.Launcher.Tests;

public class WpeInitTests
{
    [Fact]
    public void ParseIpConfig_ValidIp_Found()
    {
        var output = """
        Windows IP Configuration

        Ethernet adapter Ethernet:
           IPv4 Address. . . . . . . . . . . : 192.168.64.20
           Subnet Mask . . . . . . . . . . . : 255.255.255.0
        """;
        var ip = WpeInit.ParseFirstNonApipaIp(output);
        Assert.Equal("192.168.64.20", ip);
    }

    [Fact]
    public void ParseIpConfig_ApipaOnly_ReturnsNull()
    {
        var output = """
        Ethernet adapter Ethernet:
           IPv4 Address. . . . . . . . . . . : 169.254.12.34
        """;
        Assert.Null(WpeInit.ParseFirstNonApipaIp(output));
    }

    [Fact]
    public void ParseIpConfig_NoIp_ReturnsNull()
    {
        var output = "Windows IP Configuration\n\n";
        Assert.Null(WpeInit.ParseFirstNonApipaIp(output));
    }

    [Fact]
    public void ParseIpConfig_Localhost_Ignored()
    {
        var output = """
        Ethernet adapter Loopback:
           IPv4 Address. . . . . . . . . . . : 127.0.0.1
        """;
        Assert.Null(WpeInit.ParseFirstNonApipaIp(output));
    }
}
