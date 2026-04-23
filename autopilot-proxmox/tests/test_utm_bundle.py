"""Tests for web.utm_bundle — UTM .utm bundle generator and runtime control.

Spec: docs/superpowers/specs/2026-04-23-utm-native-lifecycle-foundation-design.md
"""
import json
import subprocess
import sys


def test_cli_build_echoes_spec_on_stdout(tmp_path):
    """The `build` CLI reads a spec JSON from stdin and echoes the UUID it
    received on stdout as JSON. This proves the Ansible↔Python handoff shape
    before any bundle-writing logic exists.
    """
    spec = {"name": "test", "uuid": "00000000-0000-0000-0000-000000000000"}
    result = subprocess.run(
        [sys.executable, "-m", "web.utm_bundle", "build",
         "--spec", "-", "--out", str(tmp_path / "test.utm")],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(result.stdout)
    assert out["uuid"] == spec["uuid"]
