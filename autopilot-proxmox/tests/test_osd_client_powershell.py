from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("pwsh") is None,
    reason="PowerShell is required for OSD client Pester tests",
)


def test_osd_client_content_materializer_pester():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-Command",
            (
                "$c = New-PesterConfiguration; "
                "$c.Run.Path = 'tests/pester/OsdClient.Content.Tests.ps1'; "
                "$c.Run.Exit = $true; "
                "$c.TestResult.Enabled = $false; "
                "Invoke-Pester -Configuration $c"
            ),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
