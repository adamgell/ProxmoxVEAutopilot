from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "new_entra_app_registration.ps1"


def test_new_entra_app_registration_script_declares_safe_outputs_and_redirects():
    text = SCRIPT.read_text()

    assert "param(" in text
    assert "$RedirectUri" in text
    assert "https://autopilot.gell.one/auth/callback" in text
    assert "$OutputPath" in text
    assert "vault_entra_app_id:" in text
    assert "vault_entra_tenant_id:" in text
    assert "vault_entra_app_secret:" in text
    assert "Secret text is written only to the output vault file" in text


def test_new_entra_app_registration_script_creates_app_secret_and_graph_permissions():
    text = SCRIPT.read_text()

    assert "Connect-MgGraph" in text
    assert "Application.ReadWrite.All" in text
    assert "AppRoleAssignment.ReadWrite.All" in text
    assert "00000003-0000-0000-c000-000000000000" in text
    assert "DeviceManagementServiceConfig.ReadWrite.All" in text
    assert "DeviceManagementManagedDevices.ReadWrite.All" in text
    assert "Device.ReadWrite.All" in text
    assert "Organization.Read.All" in text
    assert "openid" in text
    assert "profile" in text
    assert "email" in text
    assert "/applications" in text
    assert "/addPassword" in text
    assert "/appRoleAssignments" in text
    assert "requiredResourceAccess" in text
    assert "enableIdTokenIssuance = $true" in text
