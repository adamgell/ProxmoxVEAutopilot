from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_osdeploy_cockpit_template_has_cloudosd_parity_surfaces():
    template = (ROOT / "web/templates/osdeploy.html").read_text(encoding="utf-8")

    assert "OSDeploy Run History" in template
    assert "OSDeploy Cache" in template
    assert "Artifacts" in template
    assert "Preflight" in template
    assert "Review &amp; Launch" in template
    assert "data-osdeploy-archive" in template
    assert "data-osdeploy-unarchive" in template
    assert "data-osdeploy-bulk-archive=\"archive-stale-failed\"" in template
    assert "data-osdeploy-cache-action=\"refresh\"" in template
    assert "data-osdeploy-cache-warm" in template
    assert "data-osdeploy-cache-verify" in template
    assert "data-osdeploy-cache-delete" in template
    assert "data-osdeploy-cache-download" in template
    assert "/api/osdeploy/v1/cache/{{ entry.id }}/download/{{ entry.file_name }}" in template
    assert "/api/osdeploy/v1/cache/catalog/refresh" in template
    assert "/api/osdeploy/v1/artifacts/build/preflight" in template
    assert 'value="{{ osdeploy_build_defaults.remote }}"' in template
    assert 'value="{{ osdeploy_build_defaults.remote_root }}"' in template
    assert 'name="build_host_agent_id"' in template
    assert 'id="activateOsdeployBuildHostBtn"' in template
    assert 'id="repairOsdeployBuildHostBtn"' in template
    assert "/api/osdeploy/v1/build-host/agents/{agent_id}/activate" in template
    assert "/api/osdeploy/v1/build-host/agents/{agent_id}/repair" in template
    assert 'name="source_media_path"' in template
    assert 'name="image_name"' in template
    assert 'name="image_index"' in template
    assert 'name="os_version"' in template
    assert 'name="os_edition"' in template
    assert 'name="os_language"' in template
    assert 'href="/settings"' in template
    assert "OSDeploy Build Host" in template
    assert "data-osdeploy-build-key-status" in template
    assert "osdeploy-build-host-key.sh" in template
    assert "osdeploy_build_defaults.ssh_public_key" in template
    assert "artifact.publish_job_url" in template
    assert "artifact.publish_log_url" in template


def test_osdeploy_cockpit_wires_operator_actions_to_osdeploy_api():
    template = (ROOT / "web/templates/osdeploy.html").read_text(encoding="utf-8")

    assert "function initOSDeploy" in template
    assert "osdeployBuildForm" in template
    assert "form.dataset.preflightEndpoint" in template
    assert "form.dataset.endpoint" in template
    assert "activateOsdeployBuildHostBtn" in template
    assert "repairOsdeployBuildHostBtn" in template
    assert "confirm_build_host: true" in template
    assert "upgrade_agent: true" in template
    assert "/api/osdeploy/v1/cache/catalog/refresh" in template
    assert "/api/osdeploy/v1/cache/${id}/warm" in template
    assert "/api/osdeploy/v1/cache/${id}/verify" in template
    assert "/api/osdeploy/v1/cache/${id}/delete" in template
    assert "/api/osdeploy/v1/artifacts/${artifactId}/publish" in template
    assert "/api/osdeploy/v1/runs/${encodeURIComponent(runId)}/${action}" in template
    assert "/api/osdeploy/v1/runs/${action}" in template
    assert "osdeployActionStatus" in template
    assert "osdeployBuildStatus" in template


def test_osdeploy_builder_is_operational_server_base_launcher():
    template = (ROOT / "web/templates/osdeploy.html").read_text(encoding="utf-8")

    assert 'id="osdeployRunForm"' in template
    assert 'class="osdeploy-grid osdeploy-builder-form"' in template
    assert 'class="osdeploy-field osdeploy-field-artifact"' in template
    assert ".osdeploy-builder-form > label { min-width:0;" in template
    assert ".osdeploy-builder-form select" in template
    assert "text-overflow:ellipsis" in template
    assert 'id="osdeploy_artifact_id"' in template
    assert 'id="osdeploy_vm_name"' in template
    assert 'id="osdeploy_server_role"' in template
    assert 'id="osdeployBlockingChecks"' in template
    assert 'id="osdeployWarningChecks"' in template
    assert 'id="osdeployPreflightStatus"' in template
    assert 'id="osdeployLaunchError"' in template
    assert 'id="reviewOsdeployArtifact"' in template
    assert 'id="reviewOsdeployComputerName"' in template
    assert 'id="reviewOsdeployBlocking"' in template
    assert "const osdeployState" in template
    assert "osdeployState.artifacts" in template
    assert "osdeployState.options" in template
    assert "roleCatalog:" in template
    assert 'data-role-options-panel="file_server"' in template
    assert 'data-role-options-panel="isolated_domain_controller"' in template
    assert 'data-role-options-panel="mecm_prereq"' in template
    assert 'data-role-options-panel="lab_in_a_box"' in template
    assert "roleOptionsPayload" in template
    assert "role_options" in template
    assert '"/api/osdeploy/v1/bundles"' in template
    assert '"/api/osdeploy/v1/preflight"' in template
    assert '"/api/osdeploy/v1/runs"' in template
    assert "/api/osdeploy/v1/runs/${run.run_id}/provision" in template
    assert "launchOsdeploy" in template


def test_osdeploy_run_detail_template_has_v2_plan_and_readiness():
    template = (ROOT / "web/templates/osdeploy_run_detail.html").read_text(encoding="utf-8")

    assert "OSDeploy v2 Plan" in template
    assert "Server Readiness" in template
    assert "data-osdeploy-v2-steps" in template
    assert "data-osdeploy-field=\"readiness_state\"" in template
    assert "data-osdeploy-field=\"role_status\"" in template
    assert "data-osdeploy-field=\"heartbeat\"" in template
    assert "data-osdeploy-field=\"job_link\"" in template


def test_provision_page_exposes_osdeploy_and_legacy_winpe_labels():
    template = (ROOT / "web/templates/provision.html").read_text(encoding="utf-8")

    assert '<option value="osdeploy">OSDeploy v2 (Windows Server / advanced installs)</option>' in template
    assert '<option value="winpe">Legacy WinPE (fallback image apply)</option>' in template
    assert "OSDeploy Server base deployment" in template
    assert 'data-boot-section="osdeploy"' in template


def test_base_nav_exposes_osdeploy_cockpit():
    template = (ROOT / "web/templates/base.html").read_text(encoding="utf-8")

    assert 'href="/osdeploy">OSDeploy Server' in template
    assert 'aria-label="OSDeploy pages"' not in template
    assert 'href="/osdeploy/builder"' not in template
    assert 'href="/osdeploy/cache"' not in template
    assert 'href="/osdeploy/artifacts"' not in template


def test_osdeploy_page_exposes_local_tabs():
    template = (ROOT / "web/templates/osdeploy.html").read_text(encoding="utf-8")

    assert 'aria-label="OSDeploy pages"' in template
    assert 'href="/osdeploy/builder"' in template
    assert 'href="/osdeploy/cache"' in template
    assert 'href="/osdeploy/artifacts"' in template


def test_v2_sequence_library_surfaces_osdeploy_role_templates():
    from web import app as web_app

    names = {template["name"]: template for template in web_app._v2_flow_templates()}
    for name in [
        "OSDeploy Windows Server Base",
        "OSDeploy File Server",
        "OSDeploy Isolated Domain Controller",
        "OSDeploy MECM Prereq Baseline",
        "OSDeploy Lab in a Box",
    ]:
        assert names[name]["target_os"] == "windows"
        assert any(node["kind"] == "osdeploy_preflight" for node in names[name]["nodes"])
        assert not any(node["kind"] == "cloudosd_preflight" for node in names[name]["nodes"])
        assert any(node["kind"] == "wait_agent_heartbeat" for node in names[name]["nodes"])
    assert any(node["kind"] == "configure_file_server_role" for node in names["OSDeploy File Server"]["nodes"])
    assert any(node["kind"] == "configure_isolated_domain_controller_role" for node in names["OSDeploy Isolated Domain Controller"]["nodes"])
    assert any(node["kind"] == "configure_mecm_prereq_role" for node in names["OSDeploy MECM Prereq Baseline"]["nodes"])
    assert not any(node["kind"] == "run_script" for name in names.values() for node in name["nodes"] if str(name["name"]).startswith("OSDeploy"))
