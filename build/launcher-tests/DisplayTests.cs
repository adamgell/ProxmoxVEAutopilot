using Autopilot.Launcher;
using Xunit;

namespace Autopilot.Launcher.Tests;

public class DisplayTests
{
    [Fact]
    public void FormatDuration_UnderMinute_ShowsSeconds()
    {
        Assert.Equal("7s", Display.FormatDuration(TimeSpan.FromSeconds(7)));
    }

    [Fact]
    public void FormatDuration_OverMinute_ShowsMinSec()
    {
        Assert.Equal("2:37", Display.FormatDuration(TimeSpan.FromSeconds(157)));
    }

    [Fact]
    public void FormatBytes_Gigabytes()
    {
        Assert.Equal("6.5 GB", Display.FormatBytes(6949212989));
    }

    [Fact]
    public void FormatBytes_Megabytes()
    {
        Assert.Equal("963 B", Display.FormatBytes(963));
    }

    [Fact]
    public void ProgressBar_HalfFull()
    {
        var bar = Display.ProgressBar(50, 20);
        Assert.Equal("██████████░░░░░░░░░░", bar);
    }

    [Fact]
    public void ProgressBar_Full()
    {
        var bar = Display.ProgressBar(100, 10);
        Assert.Equal("██████████", bar);
    }

    [Fact]
    public void ProgressBar_Empty()
    {
        var bar = Display.ProgressBar(0, 10);
        Assert.Equal("░░░░░░░░░░", bar);
    }

    [Fact]
    public void StepIcon_AllStates()
    {
        Assert.Equal("[ ]", Display.StepIcon(StepState.Pending));
        Assert.Equal("[▸]", Display.StepIcon(StepState.Active));
        Assert.Equal("[✓]", Display.StepIcon(StepState.Done));
        Assert.Equal("[✗]", Display.StepIcon(StepState.Error));
    }
}
