"""run_late_command / run_firstboot_script step compilers."""
from __future__ import annotations

from web.ubuntu_compiler import compile_step


def test_run_late_command_appends_to_runcmd() -> None:
    out = compile_step(
        "run_late_command",
        params={"command": "echo hello > /tmp/greet"},
        credentials={},
    )
    # No curtin wrapping — plain sh -c so the shell redirect works on the
    # booted cloud image. The step-type name is historical.
    assert out.runcmd == ["sh -c 'echo hello > /tmp/greet'"]
    assert out.firstboot_runcmd == []


def test_run_firstboot_script_appends_to_firstboot_runcmd() -> None:
    out = compile_step(
        "run_firstboot_script",
        params={"command": "hostnamectl set-hostname $(hostname)"},
        credentials={},
    )
    assert out.firstboot_runcmd == ["hostnamectl set-hostname $(hostname)"]
    assert out.runcmd == []


def test_run_firstboot_script_multiline_command() -> None:
    out = compile_step(
        "run_firstboot_script",
        params={"command": "set -e\ntouch /var/log/firstboot\necho done"},
        credentials={},
    )
    # Multi-line commands are wrapped in sh -c "..." to keep runcmd semantics
    # identical regardless of how many lines the user wrote.
    assert len(out.firstboot_runcmd) == 1
    assert "set -e" in out.firstboot_runcmd[0]
    assert "touch /var/log/firstboot" in out.firstboot_runcmd[0]
