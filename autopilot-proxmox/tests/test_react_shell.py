import json
import os
import subprocess
import sys
from pathlib import Path

from web import auth


def test_react_shell_auth_boundary_is_narrow():
    assert auth.is_exempt_path("/static/react/assets/app.js")
    assert not auth.is_exempt_path("/react-shell")
    assert not auth.is_exempt_path("/app")
    assert not auth.is_exempt_path("/openapi.json")


def test_react_shell_route_renders_authenticated_bootstrap(web_client):
    response = web_client.get("/react-shell")

    assert response.status_code == 200
    assert 'id="react-root"' in response.text
    assert 'data-react-shell="protected"' in response.text
    assert "Proxmox VE Autopilot" in response.text


def test_openapi_export_script_uses_local_app_import(tmp_path):
    output = tmp_path / "openapi.json"
    env = os.environ.copy()
    env["AUTOPILOT_AUTH_BYPASS"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/export_openapi_schema.py",
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    schema = json.loads(output.read_text(encoding="utf-8"))
    assert schema["info"]["title"] == "Proxmox VE Autopilot"
    assert "/api/version" in schema["paths"]
