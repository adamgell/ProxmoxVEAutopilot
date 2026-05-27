import importlib.util
import json
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path


def _load_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "e2e"
        / "sdn_lab_stress_e2e.py"
    )
    spec = importlib.util.spec_from_file_location("sdn_lab_stress_e2e", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_redact_secrets_recursively_masks_sensitive_values():
    e2e = _load_module()

    payload = {
        "username": "E2E30\\joiner",
        "password": "Joiner-Plaintext-Secret",
        "nested": {
            "domain_join_password": "DomainJoin-Secret",
            "bearer_token": "token-value",
            "notes": "safe text",
        },
        "list": [{"dsrm_password": "Dsrm-Secret"}],
    }

    redacted = e2e.redact_secrets(payload)
    rendered = json.dumps(redacted)

    assert "Joiner-Plaintext-Secret" not in rendered
    assert "DomainJoin-Secret" not in rendered
    assert "token-value" not in rendered
    assert "Dsrm-Secret" not in rendered
    assert redacted["username"] == "E2E30\\joiner"
    assert redacted["nested"]["notes"] == "safe text"


def test_redact_command_text_masks_encoded_powershell_and_known_secrets():
    e2e = _load_module()

    text = "qm guest exec 114 -- powershell.exe -EncodedCommand QQBCAEMA PlainSecret"

    redacted = e2e.redact_command_text(text, ["PlainSecret"])

    assert "QQBCAEMA" not in redacted
    assert "PlainSecret" not in redacted
    assert "-EncodedCommand [REDACTED]" in redacted


def test_redact_known_secret_values_masks_strings_without_secret_keys():
    e2e = _load_module()

    payload = {
        "message": "scheduled task output contained PlainSecret!",
        "rows": [{"note": "still PlainSecret! here"}],
    }

    redacted = e2e.redact_known_secret_values(payload, ["PlainSecret!"])

    rendered = json.dumps(redacted)
    assert "PlainSecret!" not in rendered
    assert redacted["message"] == "scheduled task output contained [REDACTED]"


def test_parse_qga_json_output_extracts_guest_payload():
    e2e = _load_module()

    raw = json.dumps({
        "exitcode": 0,
        "exited": 1,
        "out-data": '{"computer":"E2E30-WK-01","kerberos_ok":true}\\r\\n',
    })

    parsed = e2e.parse_qga_json_output(raw)

    assert parsed == {"computer": "E2E30-WK-01", "kerberos_ok": True}


def test_safe_record_name_removes_path_separators_from_evidence_names():
    e2e = _load_module()

    assert (
        e2e.safe_record_name("teardown_error_delete_subnet_cxwlab1_10.77.20.0/24")
        == "teardown_error_delete_subnet_cxwlab1_10.77.20.0_24"
    )


def test_validate_workstation_proof_requires_domain_dc_srv_secure_channel_and_user_auth():
    e2e = _load_module()
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )
    proof = {
        "computer": "E2E30-WK-01",
        "domain": "e2e30.lab",
        "part_of_domain": True,
        "dns_servers": ["10.77.30.100"],
        "dc_port_53": True,
        "dc_port_88": True,
        "dc_port_389": True,
        "dc_port_445": True,
        "dsgetdc_found": True,
        "dsgetdc_dc": "E2E30-DC01.e2e30.lab",
        "dsgetdc_address": "10.77.30.100",
        "srv_lookup_ok": True,
        "srv_records": [{"NameTarget": "e2e30-dc01.e2e30.lab", "Port": 389}],
        "secure_channel_ok": True,
        "trusted_dc": "E2E30-DC01.e2e30.lab",
        "kerberos_ok": True,
        "kdc_called": "E2E30-DC01.e2e30.lab",
        "user_auth": {
            "ok": True,
            "whoami": "e2e30\\e2eproof",
            "ldap_bind_ok": True,
            "cifs_kerberos_ok": True,
            "share_write_read_ok": True,
            "bad_password_rejected": True,
        },
    }

    assert e2e.validate_workstation_proof(proof, spec) == []

    proof["user_auth"]["bad_password_rejected"] = False
    failures = e2e.validate_workstation_proof(proof, spec)

    assert "bad_password_not_rejected" in failures


def test_validate_workstation_proof_requires_domain_user_ldap_and_cifs_kerberos():
    e2e = _load_module()
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )
    proof = {
        "computer": "E2E30-WK-01",
        "domain": "e2e30.lab",
        "part_of_domain": True,
        "dns_servers": ["10.77.30.100"],
        "dc_port_53": True,
        "dc_port_88": True,
        "dc_port_389": True,
        "dc_port_445": True,
        "dsgetdc_found": True,
        "dsgetdc_dc": "E2E30-DC01.e2e30.lab",
        "dsgetdc_address": "10.77.30.100",
        "srv_lookup_ok": True,
        "srv_records": [{"NameTarget": "e2e30-dc01.e2e30.lab", "Port": 389}],
        "secure_channel_ok": True,
        "trusted_dc": "E2E30-DC01.e2e30.lab",
        "kerberos_ok": True,
        "kdc_called": "E2E30-DC01.e2e30.lab",
        "user_auth": {
            "ok": True,
            "whoami": "e2e30\\e2eproof",
            "ldap_bind_ok": True,
            "cifs_kerberos_ok": True,
            "share_write_read_ok": True,
            "bad_password_rejected": True,
        },
    }

    proof["user_auth"]["whoami"] = "wrong\\user"
    assert "user_identity_wrong" in e2e.validate_workstation_proof(proof, spec)
    proof["user_auth"]["whoami"] = "e2e30\\e2eproof"

    proof["user_auth"]["ldap_bind_ok"] = False
    assert "user_ldap_bind_failed" in e2e.validate_workstation_proof(proof, spec)
    proof["user_auth"]["ldap_bind_ok"] = True

    proof["user_auth"]["cifs_kerberos_ok"] = False
    assert "user_cifs_kerberos_failed" in e2e.validate_workstation_proof(proof, spec)


def test_workstation_user_auth_proof_waits_for_scheduled_task_to_run():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )

    script = harness.workstation_proof_script(spec, "proof-password")

    assert "$pendingTaskResults = @(0x41300, 0x41301, 0x41302, 0x41303, 0x41306)" in script
    assert "$info.LastTaskResult" in script
    assert "-not (Test-Path $outPath) -and $task.State -ne 'Ready'" not in script


def test_workstation_user_auth_proof_uses_limited_triggered_task_with_diagnostics():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )

    script = harness.workstation_proof_script(spec, "proof-password")

    assert "New-ScheduledTaskTrigger -Once" in script
    assert "New-ScheduledTaskSettingsSet" in script
    assert "schtasks.exe /Run" in script
    assert "-RunLevel Highest" not in script
    assert "`$out.ldap_bind_ok" in script
    assert "`$out.cifs_kerberos_ok" in script
    assert "$auth.task" in script


def test_workstation_user_auth_proof_grants_batch_logon_right_before_scheduling():
    """Domain workstations don't grant SeBatchLogonRight to ordinary domain
    users by default. Without the grant, Task Scheduler refuses to start
    the proof task with ERROR_LOGON_NOT_GRANTED (0x80070569, observed as
    Task Scheduler Operational event 101 'Additional Data: Error Value:
    2147943785' and last_result=267011 / SCHED_S_TASK_HAS_NOT_RUN). The
    proof script must grant SeBatchLogonRight to the proof user before
    calling Register-ScheduledTask, otherwise every workstation proof
    fails with user_auth_failed / user_share_write_read_failed.

    Observed in sdn-lab-stress-20260527T123846Z E2E30-WK-01_domain_auth_proof.json.
    """
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )

    script = harness.workstation_proof_script(spec, "proof-password")

    # Marker comment / function presence proves the grant step exists.
    assert "Grant SeBatchLogonRight" in script, (
        "proof script must grant batch logon right to the proof user before "
        "registering the scheduled task"
    )

    # secedit-based grant for minimum-privilege approach (don't add user
    # to powerful local groups like Backup Operators).
    assert "secedit.exe /export" in script
    assert "secedit.exe /configure" in script
    assert "SeBatchLogonRight" in script

    # /areas USER_RIGHTS restricts the import so we don't accidentally
    # rewrite unrelated policy when re-applying the modified .inf.
    assert "/areas USER_RIGHTS" in script

    # secedit /configure expects the .inf in UTF-16 LE (Unicode); writing
    # it as ASCII or UTF-8 silently breaks the import.
    assert "UnicodeEncoding" in script

    # The grant must precede Register-ScheduledTask in the script body so
    # the user already has the right when the task is registered.
    grant_idx = script.index("Grant SeBatchLogonRight")
    register_idx = script.index("Register-ScheduledTask -TaskName $taskName")
    assert grant_idx < register_idx, (
        "SeBatchLogonRight grant must run BEFORE Register-ScheduledTask"
    )


def test_cloudosd_domain_join_config_carries_credential_sequence_and_dc_ip():
    e2e = _load_module()
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )

    config = e2e.build_cloudosd_domain_join_config(
        spec,
        e2e.CloudOsdJoinSequence(sequence_id=42, credential_id=99),
    )

    assert config == {
        "enabled": True,
        "source_sequence_id": 42,
        "credential_id": 99,
        "domain_fqdn": "e2e30.lab",
        "credential_domain": "E2E30",
        "domain_controller_ipv4": "10.77.30.100",
        "ou_path": "",
        "acceptable_domain_names": ["e2e30.lab", "E2E30"],
    }


def test_cloudosd_domain_join_plan_requires_full_os_join_and_verify_steps():
    e2e = _load_module()
    run = {
        "domain_join": {
            "enabled": True,
            "credential_id": 99,
            "domain_controller_ipv4": "10.77.30.100",
        },
    }

    assert e2e.validate_cloudosd_domain_join_plan(
        run,
        [
            "cloudosd_preflight",
            "wait_agent_heartbeat",
            "join_domain_role",
            "verify_ad_domain_join",
        ],
    ) == []

    failures = e2e.validate_cloudosd_domain_join_plan(
        {"domain_join": {"enabled": False}},
        ["cloudosd_preflight", "wait_agent_heartbeat"],
    )

    assert failures == [
        "domain_join_not_enabled",
        "domain_join_missing_credential",
        "domain_join_missing_dc_ip",
        "missing_join_domain_role",
        "missing_verify_ad_domain_join",
    ]


def test_validate_cloudosd_artifact_manifest_requires_current_firstboot_hash():
    e2e = _load_module()

    expected = {"PVEAutopilot-FirstBoot.ps1": "abc123"}

    assert e2e.validate_cloudosd_artifact_manifest(
        {"component_sha256": {"PVEAutopilot-FirstBoot.ps1": "abc123"}},
        expected,
    ) == []
    assert e2e.validate_cloudosd_artifact_manifest({}, expected) == [
        "artifact_manifest_missing_component_sha256",
        "artifact_manifest_missing_PVEAutopilot-FirstBoot.ps1_sha256",
    ]
    assert e2e.validate_cloudosd_artifact_manifest(
        {"component_sha256": {"PVEAutopilot-FirstBoot.ps1": "old"}},
        expected,
    ) == ["artifact_manifest_stale_PVEAutopilot-FirstBoot.ps1_sha256"]


def test_negative_join_signal_accepts_cursor_kind_or_plain_error_message():
    e2e = _load_module()

    assert e2e.negative_join_signal(
        [{"cursor_kind": "join_domain_role"}],
        [{"event_type": "step_failed", "message": ""}],
    ) is True
    assert e2e.negative_join_signal(
        [{"cursor_kind": "verify_ad_domain_join"}],
        [{"event_type": "step_failed", "message": "failed to join domain with bad credentials"}],
    ) is True
    assert e2e.negative_join_signal(
        [{"cursor_kind": "verify_ad_domain_join"}],
        [{"event_type": "step_failed", "message": "unrelated failure"}],
    ) is False


def test_negative_disk_boot_restart_candidate_requires_pe_complete_without_heartbeat():
    e2e = _load_module()

    assert e2e.negative_disk_boot_restart_candidate(
        {
            "state": "pe_registered",
            "vmid": 123,
            "osdcloud_finished_at": "2026-05-25T19:05:09Z",
            "first_heartbeat_at": None,
        }
    ) is True
    assert e2e.negative_disk_boot_restart_candidate(
        {
            "state": "pe_registered",
            "vmid": 123,
            "osdcloud_finished_at": "2026-05-25T19:05:09Z",
            "first_heartbeat_at": "2026-05-25T19:10:09Z",
        }
    ) is False
    assert e2e.negative_disk_boot_restart_candidate(
        {"state": "created", "vmid": 123, "osdcloud_finished_at": None}
    ) is False


def test_maybe_restart_stopped_negative_disk_boot_records_and_starts_once():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.negative_disk_boot_restarts = set()
    records = {}
    starts = []

    harness.record = lambda name, payload: records.setdefault(name, payload)
    harness.pve_cmd = lambda command, **kwargs: "status: stopped"

    def ssh_result(host, command, **kwargs):
        starts.append((host, command, kwargs))
        return SimpleNamespace(returncode=0, stdout="UPID:pve2:123", stderr="")

    harness.ssh_result = ssh_result
    harness.pve = "root@pve"
    run = {"run_id": "bad-run", "provision_job_id": "job-bad"}
    rows = [
        {
            "run_id": "bad-run",
            "state": "pe_registered",
            "vmid": 123,
            "osdcloud_finished_at": "2026-05-25T19:05:09Z",
            "first_heartbeat_at": None,
        }
    ]

    harness.maybe_restart_stopped_negative_disk_boot(run, rows)
    harness.maybe_restart_stopped_negative_disk_boot(run, rows)

    assert starts == [("root@pve", "qm start 123", {"timeout": 60, "check": False})]
    assert records["negative_disk_boot_restart_observed"]["vmid"] == 123
    assert records["negative_disk_boot_restart_result"]["exit_code"] == 0
    assert "bad-run" in harness.negative_disk_boot_restarts


def test_maybe_restart_stopped_negative_disk_boot_accepts_running_after_start_timeout():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.negative_disk_boot_restarts = set()
    records = {}
    starts = []
    statuses = iter(["status: stopped", "status: running"])

    harness.record = lambda name, payload: records.setdefault(name, payload)
    harness.pve_cmd = lambda command, **kwargs: next(statuses)

    def ssh_result(host, command, **kwargs):
        starts.append((host, command, kwargs))
        return SimpleNamespace(returncode=255, stdout="", stderr="timeout waiting on systemd")

    harness.ssh_result = ssh_result
    harness.pve = "root@pve"
    run = {"run_id": "bad-run", "provision_job_id": "job-bad"}
    rows = [
        {
            "run_id": "bad-run",
            "state": "pe_registered",
            "vmid": 123,
            "osdcloud_finished_at": "2026-05-25T19:05:09Z",
            "first_heartbeat_at": None,
        }
    ]

    harness.maybe_restart_stopped_negative_disk_boot(run, rows)

    assert starts == [("root@pve", "qm start 123", {"timeout": 60, "check": False})]
    assert records["negative_disk_boot_restart_result"]["exit_code"] == 255
    assert records["negative_disk_boot_restart_result"]["post_status"] == "status: running"
    assert "bad-run" in harness.negative_disk_boot_restarts


def test_maybe_restart_stopped_negative_disk_boot_records_warning_when_restart_times_out_stopped():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.negative_disk_boot_restarts = set()
    records = {}
    starts = []
    statuses = iter(["status: stopped", "status: stopped", "status: stopped", "status: stopped"])

    harness.record = lambda name, payload: records.setdefault(name, payload)
    harness.pve_cmd = lambda command, **kwargs: next(statuses)

    def ssh_result(host, command, **kwargs):
        starts.append((host, command, kwargs))
        return SimpleNamespace(returncode=255, stdout="", stderr="timeout waiting on systemd\n")

    harness.ssh_result = ssh_result
    harness.pve = "root@pve"
    run = {"run_id": "bad-run", "provision_job_id": "job-bad"}
    rows = [
        {
            "run_id": "bad-run",
            "state": "pe_registered",
            "vmid": 123,
            "osdcloud_finished_at": "2026-05-25T19:05:09Z",
            "first_heartbeat_at": None,
        }
    ]

    harness.maybe_restart_stopped_negative_disk_boot(run, rows)

    assert starts == [
        ("root@pve", "qm start 123", {"timeout": 60, "check": False}),
        ("root@pve", "qm start 123", {"timeout": 60, "check": False}),
        ("root@pve", "qm start 123", {"timeout": 60, "check": False}),
    ]
    assert records["negative_disk_boot_restart_result"]["exit_code"] == 255
    assert records["negative_disk_boot_restart_result"]["post_status"] == "status: stopped"
    assert len(records["negative_disk_boot_restart_result"]["attempts"]) == 3
    assert records["negative_disk_boot_restart_warning"]["vmid"] == 123
    assert "bad-run" in harness.negative_disk_boot_restarts


def test_create_cloudosd_run_uses_pg_domain_join_config_not_sequence_id_only():
    e2e = _load_module()
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )
    sequence = e2e.CloudOsdJoinSequence(sequence_id=42, credential_id=99)
    harness = object.__new__(e2e.StressHarness)
    harness.args = SimpleNamespace(cloudosd_artifact="cloud-artifact")
    harness.node = "pve2"
    harness.cloudosd_os_version = "Windows 11 24H2"
    harness.lab_contexts = {spec.domain: {"bubble_id": "bubble-id"}}
    harness.created_cloudosd_runs = []
    captured = {}
    records = {}
    remembered = []
    harness.log = lambda message: None
    harness.record = lambda name, payload: records.setdefault(name, payload)
    harness.remember_vmid = lambda vmid: remembered.append(vmid)

    def controller_python(script, **kwargs):
        captured["script"] = script
        return {
            "run": {
                "run_id": "run-1",
                "vmid": 118,
                "provision_job_id": "job-1",
                "domain_join": e2e.build_cloudosd_domain_join_config(spec, sequence),
            },
            "plan_steps": [
                {"kind": "join_domain_role"},
                {"kind": "verify_ad_domain_join"},
            ],
            "provision_job": {"ok": True, "job_id": "job-1"},
        }

    harness.controller_python = controller_python

    run = harness.create_cloudosd_run(spec, "E2E30-WK-01", sequence)

    assert run["run_id"] == "run-1"
    assert run["provision_job_id"] == "job-1"
    assert harness.created_cloudosd_runs == ["run-1"]
    assert remembered == [118]
    assert "cloudosd_pg.create_run" in captured["script"]
    assert "'enabled': True" in captured["script"]
    assert "domain_join=domain_join" in captured["script"]
    assert "os_version='Windows 11 24H2'" in captured["script"]
    assert "cloudosd_provision_extra_vars" in captured["script"]
    assert "job_manager.start('provision_cloudosd'" in captured["script"]
    assert "cloudosd_endpoints.provision_run" not in captured["script"]
    assert '"enabled": true' not in captured["script"]
    assert "RunCreateBody" not in captured["script"]
    assert records["E2E30-WK-01_cloudosd_domain_join_plan"]["failures"] == []


def test_build_teardown_actions_stops_and_destroys_vms_before_sdn_delete_and_apply():
    e2e = _load_module()
    resources = e2e.TeardownResources(
        vmids=[115, 114],
        cloudosd_run_ids=["cloud-run"],
        osdeploy_run_ids=["osd-run"],
        bubble_ids=["bubble-id"],
        sdn_objects=[
            e2e.SdnObject(kind="subnet", vnet="e2ev30", name="10.77.30.0/24"),
            e2e.SdnObject(kind="vnet", name="e2ev30"),
            e2e.SdnObject(kind="zone", name="e2ez30"),
        ],
    )

    actions = e2e.build_teardown_actions(resources)
    action_names = [action.name for action in actions]

    assert action_names[:4] == [
        "qm_stop_115",
        "qm_destroy_115",
        "qm_stop_114",
        "qm_destroy_114",
    ]
    assert action_names.index("delete_subnet_e2ev30_10.77.30.0/24") < action_names.index("delete_vnet_e2ev30")
    assert action_names.index("delete_vnet_e2ev30") < action_names.index("delete_zone_e2ez30")
    assert action_names[-1] == "apply_sdn"


def test_existing_cxw_teardown_requires_baseline_capture():
    e2e = _load_module()

    assert e2e.should_include_existing_cxw(False, False) is False
    assert e2e.should_include_existing_cxw(True, False) is False
    assert e2e.should_include_existing_cxw(True, True) is True


def test_vm_name_teardown_scope_protects_unrelated_vms():
    e2e = _load_module()

    assert e2e.vm_name_in_teardown_scope("E2E30-WK-01", include_cxw=False) is True
    assert e2e.vm_name_in_teardown_scope("E2E40-DC01", include_cxw=False) is True
    assert e2e.vm_name_in_teardown_scope("CXW-W2-01", include_cxw=False) is False
    assert e2e.vm_name_in_teardown_scope("CXW-W2-01", include_cxw=True) is True
    assert e2e.vm_name_in_teardown_scope("DNS4", include_cxw=True) is False


def test_planned_vmid_for_name_pins_e2e_lab_assets():
    e2e = _load_module()

    assert e2e.planned_vmid_for_name("E2E30-DC01") == 114
    assert e2e.planned_vmid_for_name("E2E30-WK-03") == 121
    assert e2e.planned_vmid_for_name("E2E40-WK-04") == 128
    assert e2e.planned_vmid_for_name("DNS4") is None


def test_domain_join_admin_username_uses_upn_for_new_forest_join():
    e2e = _load_module()
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )

    assert e2e.domain_join_admin_username(spec) == "Administrator@e2e30.lab"


def test_sdn_create_commands_use_gateway_dns_for_dc_bootstrap():
    e2e = _load_module()
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )

    commands = e2e.build_sdn_create_commands(spec)
    subnet_command = commands[2]

    assert "--type subnet" in subnet_command
    assert "--dhcp-dns-server 10.77.30.1" in subnet_command
    assert "start-address=10.77.30.100,end-address=10.77.30.199" in subnet_command


def test_sdn_set_dns_commands_try_proxmox_id_before_raw_cidr():
    e2e = _load_module()
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )

    commands = e2e.build_sdn_set_dns_commands(spec, spec.dc_ip)

    assert commands == [
        "pvesh set /cluster/sdn/vnets/e2ev30/subnets/e2ez30-10.77.30.0-24 --dhcp-dns-server 10.77.30.100",
        "pvesh set /cluster/sdn/vnets/e2ev30/subnets/10.77.30.0/24 --dhcp-dns-server 10.77.30.100",
    ]


def test_subnet_delete_candidates_try_proxmox_id_before_raw_cidr():
    e2e = _load_module()
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )
    obj = e2e.SdnObject(kind="subnet", vnet="e2ev30", name="10.77.30.0/24")

    assert e2e.subnet_delete_candidates(obj, [spec]) == [
        "e2ez30-10.77.30.0-24",
        "10.77.30.0/24",
    ]


def test_sdn_lock_token_accepts_string_or_mapping_response():
    e2e = _load_module()

    assert e2e.sdn_lock_token_from_response("019e-lock") == "019e-lock"
    assert e2e.sdn_lock_token_from_response({"lock-token": "dash-token"}) == "dash-token"
    assert e2e.sdn_lock_token_from_response({"lock_token": "underscore-token"}) == "underscore-token"


def test_osdeploy_request_fields_follow_artifact_metadata():
    e2e = _load_module()

    fields = e2e.osdeploy_request_fields_from_artifact({
        "os_version": "Windows Server 2025",
        "os_edition": "Datacenter",
        "os_language": "en-us",
    })

    assert fields == {
        "os_version": "Windows Server 2025",
        "os_edition": "Datacenter",
        "os_language": "en-us",
    }


def test_select_ready_cloudosd_feature_image_requires_exact_ready_cache_match():
    e2e = _load_module()

    rows = [
        {
            "entry_type": "feature_image",
            "status": "missing",
            "windows_version": "Windows 11 25H2",
            "architecture": "amd64",
            "language": "en-us",
            "activation": "Volume",
            "edition": "Enterprise",
            "file_name": "25h2.esd",
        },
        {
            "entry_type": "feature_image",
            "status": "ready",
            "windows_version": "Windows 11 24H2",
            "architecture": "amd64",
            "language": "en-us",
            "activation": "Volume",
            "edition": "Enterprise",
            "file_name": "24h2.esd",
        },
    ]

    selected = e2e.select_ready_cloudosd_feature_image(rows, os_version="Windows 11 24H2")

    assert selected["file_name"] == "24h2.esd"
    assert e2e.select_ready_cloudosd_feature_image(rows, os_version="Windows 11 25H2") is None


def test_validate_cloudosd_cache_file_stat_requires_existing_expected_size_file():
    e2e = _load_module()
    entry = {"expected_size_bytes": 4612287490}

    assert e2e.validate_cloudosd_cache_file_stat(entry, {"exists": True, "size_bytes": 4612287490}) == []
    assert e2e.validate_cloudosd_cache_file_stat(entry, {"exists": False}) == [
        "cache_file_missing",
        "cache_file_size_missing",
    ]
    assert e2e.validate_cloudosd_cache_file_stat(entry, {"exists": True, "size_bytes": 42}) == [
        "cache_file_size_mismatch",
    ]


def test_e2e_credential_names_are_unique_per_run_stamp():
    e2e = _load_module()

    assert (
        e2e.e2e_credential_name("E2E30", "forest-admin", "20260525T011900Z")
        == "e2e-e2e30-forest-admin-20260525T011900Z"
    )


def test_cxw_baseline_records_qga_errors_without_blocking_teardown():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.args = SimpleNamespace(include_existing_cxw=True)
    harness.pve = "root@pve"
    harness.cxw_vmids = [114]
    harness.cxw_present_vmids = []
    harness.cxw_baseline_captured = False
    records = {}
    harness.record = lambda name, payload: records.setdefault(name, payload)
    harness.log = lambda message: None
    harness.ssh_result = lambda *args, **kwargs: SimpleNamespace(
        returncode=0,
        stdout="status: running",
        stderr="",
    )

    def fail_qga(*args, **kwargs):
        raise e2e.E2EError("QGA unavailable")

    harness.wait_for_qga = fail_qga

    harness.run_baseline_cxw()

    assert harness.cxw_baseline_captured is True
    assert records["cxw_baseline"]["114"]["qga_error"] == "QGA unavailable"
    assert harness.cxw_present_vmids == [114]


def test_cxw_baseline_skips_missing_legacy_vmids_without_qga_wait():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.args = SimpleNamespace(include_existing_cxw=True)
    harness.pve = "root@pve"
    harness.cxw_vmids = [114]
    harness.cxw_present_vmids = []
    harness.cxw_baseline_captured = False
    records = {}
    harness.record = lambda name, payload: records.setdefault(name, payload)
    harness.log = lambda message: None
    harness.ssh_result = lambda *args, **kwargs: SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="Configuration file does not exist",
    )

    def fail_wait(*args, **kwargs):
        raise AssertionError("missing CXW VM should not wait for QGA")

    harness.wait_for_qga = fail_wait

    harness.run_baseline_cxw()

    assert harness.cxw_baseline_captured is True
    assert records["cxw_baseline"]["114"]["present"] is False
    assert harness.cxw_present_vmids == []


def test_wait_until_raises_timeout_with_last_observation():
    e2e = _load_module()
    observations = iter(["first", "second", "last"])
    ticks = iter([0.0, 0.4, 0.8, 1.2])

    def predicate():
        return False, next(observations)

    def monotonic():
        return next(ticks)

    try:
        e2e.wait_until(
            predicate,
            timeout_seconds=1.0,
            interval_seconds=0.1,
            sleep=lambda _: None,
            monotonic=monotonic,
        )
    except e2e.E2ETimeout as exc:
        assert exc.last_observation == "last"
        assert "timed out" in str(exc)
    else:
        raise AssertionError("wait_until should have raised E2ETimeout")


def test_skill_status_timeout_payload_is_nonfatal_and_sanitized():
    e2e = _load_module()
    exc = subprocess.TimeoutExpired(
        ["./skill.sh", "status"],
        45,
        output="partial stdout",
        stderr="partial stderr",
    )

    payload = e2e.skill_status_timeout_payload(exc)

    assert payload["exit_code"] is None
    assert payload["timed_out"] is True
    assert payload["mcp_docs_available"] is False
    assert "partial stdout" in payload["stdout"]


def test_wait_for_qga_uses_pve9_guest_cmd_ping():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.pve = "root@pve"
    calls = []
    harness.log = lambda message: None

    def ssh_result(host, command, **kwargs):
        assert host == "root@pve"
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    harness.ssh_result = ssh_result

    harness.wait_for_qga(114, timeout_seconds=1)

    assert calls == ["qm guest cmd 114 ping"]


def test_qga_ping_rejects_not_running_text_even_with_zero_exit_code():
    e2e = _load_module()
    result = SimpleNamespace(
        returncode=0,
        stdout="",
        stderr="QEMU guest agent is not running",
    )

    assert e2e.qga_ping_succeeded(result) is False


def test_wait_for_qga_rejects_unresolved_vmid_without_shell_call():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.log = lambda message: None

    def pve_cmd(*args, **kwargs):
        raise AssertionError("missing VMID must not invoke qm")

    harness.pve_cmd = pve_cmd

    try:
        harness.wait_for_qga(0, timeout_seconds=1)
    except e2e.E2EError as exc:
        assert "resolved VMID" in str(exc)
    else:
        raise AssertionError("wait_for_qga should fail fast for VMID 0")


def test_qm_guest_exec_json_accepts_inline_exit_output():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    calls = []

    def pve_cmd(command, **kwargs):
        calls.append(command)
        return json.dumps({
            "exitcode": 0,
            "exited": 1,
            "out-data": '{"ok":true}',
        })

    harness.pve_cmd = pve_cmd

    assert harness.qm_guest_exec_json(118, "Write-Output ok") == {"ok": True}
    assert calls[0].startswith("qm guest exec 118 -- powershell.exe")


def test_qm_guest_exec_json_polls_exec_status_when_qga_returns_pid():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    calls = []

    def pve_cmd(command, **kwargs):
        calls.append(command)
        if command.startswith("qm guest exec-status"):
            return json.dumps({
                "exitcode": 0,
                "exited": 1,
                "out-data": '{"proof":"ok"}',
            })
        return json.dumps({"pid": 5760})

    harness.pve_cmd = pve_cmd

    assert harness.qm_guest_exec_json(118, "Write-Output ok", timeout=1) == {"proof": "ok"}
    assert calls[1] == "qm guest exec-status 118 5760"


def test_qm_guest_exec_json_retries_transient_exec_status_timeout():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    calls = []

    def pve_cmd(command, **kwargs):
        calls.append(command)
        if command.startswith("qm guest exec-status"):
            status_calls = sum(1 for call in calls if call.startswith("qm guest exec-status"))
            if status_calls == 1:
                raise e2e.E2EError("VM 118 qga command 'guest-exec-status' failed - got timeout")
            return json.dumps({
                "exitcode": 0,
                "exited": 1,
                "out-data": '{"proof":"ok"}',
            })
        return json.dumps({"pid": 5760})

    harness.pve_cmd = pve_cmd

    def fake_wait_until(predicate, **kwargs):
        ok, observation = predicate()
        assert not ok
        assert "guest-exec-status" in str(observation)
        ok, observation = predicate()
        assert ok
        return observation

    original_wait_until = e2e.wait_until
    e2e.wait_until = fake_wait_until
    try:
        assert harness.qm_guest_exec_json(118, "Write-Output ok", timeout=1) == {"proof": "ok"}
    finally:
        e2e.wait_until = original_wait_until

    assert calls.count("qm guest exec-status 118 5760") == 2


def test_trigger_awaiting_reboots_uses_qga_once_and_records_evidence():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.pve = "root@pve"
    harness.triggered_reboots = set()
    calls = []
    records = {}
    harness.log = lambda message: None
    harness.record = lambda name, payload: records.setdefault(name, payload)

    def ssh_result(host, command, **kwargs):
        assert host == "root@pve"
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='{"pid":1}', stderr="")

    harness.ssh_result = ssh_result
    status = {
        "osdeploy": [
            {
                "run_id": "run-dc",
                "vmid": 114,
                "ts_state": "awaiting_reboot",
                "cursor_step_id": "step-dc",
                "cursor_kind": "configure_isolated_domain_controller_role",
                "cursor_state": "awaiting_reboot",
            }
        ],
        "cloudosd": [],
    }

    harness.trigger_awaiting_reboots(status)
    harness.trigger_awaiting_reboots(status)

    assert calls == ["qm guest exec 114 -- shutdown.exe /r /t 5 /f"]
    assert "run-dc:step-dc" in harness.triggered_reboots
    assert records["required_reboot_114_step-dc"]["guest_exec"]["exit_code"] == 0


def test_trigger_awaiting_reboots_records_reset_timeout_without_raising():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.pve = "root@pve"
    harness.triggered_reboots = set()
    records = {}
    harness.log = lambda message: None
    harness.record = lambda name, payload: records.setdefault(name, payload)

    def ssh_result(host, command, **kwargs):
        if command.startswith("qm guest exec"):
            return SimpleNamespace(returncode=255, stdout="", stderr="QGA not ready")
        raise subprocess.TimeoutExpired(["ssh", host, command], kwargs.get("timeout", 30))

    harness.ssh_result = ssh_result

    harness.trigger_awaiting_reboots({
        "osdeploy": [
            {
                "run_id": "run-dc",
                "vmid": 114,
                "ts_state": "awaiting_reboot",
                "cursor_step_id": "step-dc",
                "cursor_kind": "configure_isolated_domain_controller_role",
                "cursor_state": "awaiting_reboot",
            }
        ],
        "cloudosd": [],
    })

    record = records["required_reboot_114_step-dc"]
    assert record["guest_exec"]["exit_code"] == 255
    assert record["qm_reset"]["timed_out"] is True


def test_wait_for_runs_complete_tracks_late_assigned_dc_vmid():
    e2e = _load_module()
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )
    harness = object.__new__(e2e.StressHarness)
    harness.created_osdeploy_runs = ["run-dc"]
    harness.created_cloudosd_runs = []
    harness.created_vmids = []
    harness.labs = [spec]
    harness.lab_contexts = {spec.domain: {"dc_run_id": "run-dc", "dc_vmid": 0}}
    harness.log = lambda message: None
    harness.record = lambda name, payload: None
    harness.fetch_run_status = lambda run_ids, *, job_ids=None: {
        "osdeploy": [{"run_id": "run-dc", "vmid": 114, "state": "complete", "ts_state": "done"}],
        "cloudosd": [],
        "events": [],
        "jobs": [],
    }
    harness.recover_stale_running_join_steps = lambda run_ids: None

    status = harness.wait_for_runs_complete(["run-dc"], timeout_seconds=1, label="domain_controllers")

    assert status["osdeploy"][0]["vmid"] == 114
    assert harness.created_vmids == [114]
    assert harness.lab_contexts[spec.domain]["dc_vmid"] == 114


def test_wait_for_runs_complete_fails_fast_when_required_job_failed():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.created_osdeploy_runs = []
    harness.created_cloudosd_runs = ["good-run"]
    harness.created_vmids = []
    harness.labs = []
    harness.lab_contexts = {}
    harness.log = lambda message: None
    harness.record = lambda name, payload: None
    harness.sync_run_status = lambda status: status
    harness.trigger_awaiting_reboots = lambda status: None
    harness.maybe_release_negative_job_slot = lambda run, status: None
    harness.recover_stale_running_join_steps = lambda run_ids: None

    def fetch_run_status(run_ids, *, job_ids=None):
        assert run_ids == ["good-run"]
        assert job_ids == ["job-good"]
        return {
            "osdeploy": [],
            "cloudosd": [
                {
                    "run_id": "good-run",
                    "state": "created",
                    "ts_state": "cloudosd_created",
                    "vmid": None,
                }
            ],
            "events": [],
            "jobs": [
                {
                    "id": "job-good",
                    "job_type": "provision_cloudosd",
                    "status": "failed",
                    "exit_code": 2,
                    "vm_name": "E2E40-WK-03",
                }
            ],
        }

    harness.fetch_run_status = fetch_run_status

    try:
        harness.wait_for_runs_complete(
            ["good-run"],
            timeout_seconds=1,
            label="cloudosd_workstations",
            required_jobs={"good-run": "job-good"},
        )
    except e2e.E2EError as exc:
        assert "cloudosd_workstations provisioning job failed" in str(exc)
        assert "E2E40-WK-03" in str(exc)
    else:
        raise AssertionError("wait_for_runs_complete did not fail on a failed provision job")


def test_wait_for_runs_complete_releases_negative_job_slot_after_join_failure():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.created_osdeploy_runs = []
    harness.created_cloudosd_runs = ["good-run", "bad-run"]
    harness.created_vmids = []
    harness.labs = []
    harness.lab_contexts = {}
    harness.released_negative_jobs = set()
    harness.secrets = ["PlaintextSecret!"]
    records = {}
    kills = []
    terminal_waits = []
    harness.log = lambda message: None
    harness.record = lambda name, payload: records.setdefault(name, payload)
    harness.sync_run_status = lambda status: status
    harness.trigger_awaiting_reboots = lambda status: None
    harness.request_job_kill = lambda job_id, **kwargs: kills.append((job_id, kwargs)) or {"job_id": job_id}
    harness.wait_for_job_terminal = lambda job_id: terminal_waits.append(job_id) or {"id": job_id, "status": "failed"}
    harness.recover_stale_running_join_steps = lambda run_ids: None

    def fetch_run_status(run_ids, *, job_ids=None):
        assert run_ids == ["good-run", "bad-run"]
        assert job_ids == ["job-bad"]
        return {
            "osdeploy": [],
            "cloudosd": [
                {"run_id": "good-run", "state": "complete", "ts_state": "done", "vmid": 118},
                {
                    "run_id": "bad-run",
                    "state": "full_os_waiting_domain_join",
                    "ts_state": "full_os_waiting_domain_join",
                    "vmid": 123,
                },
            ],
            "events": [
                {
                    "run_id": "bad-run",
                    "event_type": "join_domain_role_failed",
                    "severity": "warning",
                    "message": "The user name or password is incorrect.",
                }
            ],
            "jobs": [{"id": "job-bad", "status": "running", "exit_code": None}],
        }

    harness.fetch_run_status = fetch_run_status

    status = harness.wait_for_runs_complete(
        ["good-run"],
        timeout_seconds=1,
        label="cloudosd_workstations",
        negative_run={"run_id": "bad-run", "provision_job_id": "job-bad"},
    )

    assert status["cloudosd"][0]["run_id"] == "good-run"
    assert kills == [("job-bad", {"reason": "negative domain-join failure captured"})]
    assert terminal_waits == ["job-bad"]
    assert "job-bad" in harness.released_negative_jobs
    assert records["negative_slot_release_observed"]["state"] == "full_os_waiting_domain_join"
    assert records["negative_slot_release_observed"]["leaked"] is False
    assert records["negative_slot_release_result"]["job"]["status"] == "failed"


def test_recover_stale_running_join_steps_escapes_controller_code_literals():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    captured = {}
    records = {}
    def controller_python(code, **kwargs):
        captured["code"] = code
        return {"requeued": []}

    harness.controller_python = controller_python
    harness.record = lambda name, payload: records.setdefault(name, payload)

    harness.recover_stale_running_join_steps(["run-1"])

    assert "s.kind IN ('capture_autopilot_hash', 'join_domain_role')" in captured["code"]
    assert "s.kind != 'join_domain_role' OR COALESCE(h.domain_joined, false) = false" in captured["code"]
    assert "json.dumps({'vmid': row.get('vmid'), 'kind': row.get('kind')})" in captured["code"]
    assert records == {}


def test_controller_python_retries_transient_deadlock_errors():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.controller = "root@controller"
    harness.secrets = []
    records = {}
    logs = []
    calls = []
    sleeps = []
    harness.record = lambda name, payload: records.setdefault(name, payload)
    harness.log = lambda message: logs.append(message)

    def controller_cmd(command, *, input_text=None, timeout=300, check=True):
        calls.append(command)
        if len(calls) == 1:
            raise e2e.E2EError("psycopg.errors.DeadlockDetected: deadlock detected")
        return '{"ok": true}\n'

    original_sleep = e2e.time.sleep
    harness.controller_cmd = controller_cmd
    e2e.time.sleep = lambda seconds: sleeps.append(seconds)
    try:
        result = harness.controller_python("print('ok')")
    finally:
        e2e.time.sleep = original_sleep

    assert result == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [2]
    assert any(name.startswith("controller_python_retry_1_") for name in records)
    assert logs == ["controller Python hit transient DB lock/deadlock; retrying attempt 2/4"]


def test_collect_failure_evidence_captures_guest_logs_before_teardown():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)
    harness.created_osdeploy_runs = []
    harness.created_cloudosd_runs = ["run-failed", "run-ok"]
    harness.created_vmids = []
    harness.secrets = ["PlainSecret!"]
    records = {}
    qga_calls = []
    harness.record = lambda name, payload: records.setdefault(
        name,
        e2e.redact_known_secret_values(e2e.redact_secrets(payload), harness.secrets),
    )
    harness.sync_run_status = lambda status: status

    def fetch_run_status(run_ids):
        assert run_ids == ["run-failed", "run-ok"]
        return {
            "osdeploy": [],
            "cloudosd": [
                {
                    "run_id": "run-failed",
                    "state": "failed",
                    "ts_state": "failed",
                    "vmid": 128,
                    "expected_computer_name": "E2E40-WK-04",
                },
                {
                    "run_id": "run-ok",
                    "state": "complete",
                    "ts_state": "done",
                    "vmid": 126,
                    "expected_computer_name": "E2E40-WK-02",
                },
            ],
            "events": [{"run_id": "run-failed", "message": "AutopilotAgent postinstall failed"}],
        }

    def qm_guest_exec_json(vmid, script, **kwargs):
        qga_calls.append((vmid, script))
        return {
            "computer": "E2E40-WK-04",
            "logs": {"postinstall": ["line with PlainSecret!"]},
        }

    harness.fetch_run_status = fetch_run_status
    harness.qm_guest_exec_json = qm_guest_exec_json

    harness.collect_failure_evidence("cloudosd failed")

    assert qga_calls and qga_calls[0][0] == 128
    assert "AutopilotAgent" in qga_calls[0][1]
    assert records["failure_run_status"]["cloudosd"][0]["run_id"] == "run-failed"
    rendered = json.dumps(records["failure_guest_E2E40-WK-04_128"])
    assert "PlainSecret!" not in rendered
    assert "[REDACTED]" in rendered


def test_failure_guest_diagnostic_script_uses_braced_path_reference():
    e2e = _load_module()
    harness = object.__new__(e2e.StressHarness)

    script = harness.failure_guest_diagnostic_script()

    assert "failed to read ${Path}:" in script
    assert "failed to read $Path:" not in script


def test_delete_sdn_object_raises_when_subnet_is_not_empty():
    e2e = _load_module()
    spec = e2e.LabSpec(
        name="E2E Lab 30",
        zone="e2ez30",
        vnet="e2ev30",
        cidr="10.77.30.0/24",
        gateway="10.77.30.1",
        domain="e2e30.lab",
        netbios="E2E30",
        dc_name="E2E30-DC01",
        dc_ip="10.77.30.100",
        workstation_prefix="E2E30-WK",
    )
    harness = object.__new__(e2e.StressHarness)
    harness.pve = "root@pve"
    harness.labs = [spec]
    records = {}
    harness.record = lambda name, payload: records.setdefault(name, payload)
    harness.ssh_result = lambda *args, **kwargs: SimpleNamespace(
        returncode=255,
        stdout="",
        stderr="delete sdn subnet object failed: cannot delete subnet '10.77.30.0/24', not empty",
    )

    try:
        harness.delete_sdn_object(e2e.SdnObject(kind="subnet", vnet="e2ev30", name="10.77.30.0/24"))
    except e2e.E2EError as exc:
        assert "not empty" in str(exc)
    else:
        raise AssertionError("not-empty subnet delete should raise")
    assert records["delete_subnet_warning_e2ev30"]["subnet"] == "10.77.30.0/24"


def test_main_handles_keyboard_interrupt_without_traceback(monkeypatch, tmp_path):
    e2e = _load_module()
    calls = {}

    class FakeHarness:
        def __init__(self, args):
            self.evidence_dir = tmp_path

        def run(self):
            raise KeyboardInterrupt()

        def record(self, name, payload):
            calls[name] = payload

        def log(self, message):
            calls["log"] = message

    monkeypatch.setattr(e2e, "StressHarness", FakeHarness)

    rc = e2e.main([
        "--osdeploy-artifact",
        "osd",
        "--cloudosd-artifact",
        "cloud",
        "--evidence-dir",
        str(tmp_path),
    ])

    assert rc == 130
    assert calls["failure"]["type"] == "KeyboardInterrupt"
    assert calls["log"] == "FAILED: interrupted"


def test_parse_args_teardown_only_allows_omitted_artifact_ids(tmp_path):
    e2e = _load_module()

    args = e2e.parse_args([
        "--teardown-only",
        "--evidence-dir",
        str(tmp_path),
    ])

    assert args.teardown_only is True
    assert args.osdeploy_artifact is None
    assert args.cloudosd_artifact is None


def test_parse_args_still_requires_artifact_ids_for_normal_run(tmp_path, capsys):
    e2e = _load_module()

    try:
        e2e.parse_args(["--evidence-dir", str(tmp_path)])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("normal run without artifact IDs should exit non-zero")
    err = capsys.readouterr().err
    assert "--osdeploy-artifact" in err
    assert "--cloudosd-artifact" in err


def test_teardown_only_seeds_state_and_invokes_teardown(tmp_path):
    e2e = _load_module()

    harness = object.__new__(e2e.StressHarness)
    harness.args = SimpleNamespace(include_existing_cxw=False)
    harness.evidence_dir = tmp_path
    harness.created_vmids = []
    harness.created_cloudosd_runs = []
    harness.created_osdeploy_runs = []
    harness.created_bubbles = []
    harness.created_sdn = []
    harness.cxw_baseline_captured = False
    harness.labs = [
        e2e.LabSpec(
            name="E2E Stress Lab 30",
            zone="e2ez30",
            vnet="e2ev30",
            cidr="10.77.30.0/24",
            gateway="10.77.30.1",
            domain="e2e30.lab",
            netbios="E2E30",
            dc_name="E2E30-DC01",
            dc_ip="10.77.30.100",
            workstation_prefix="E2E30-WK",
        ),
        e2e.LabSpec(
            name="E2E Stress Lab 40",
            zone="e2ez40",
            vnet="e2ev40",
            cidr="10.77.40.0/24",
            gateway="10.77.40.1",
            domain="e2e40.lab",
            netbios="E2E40",
            dc_name="E2E40-DC01",
            dc_ip="10.77.40.100",
            workstation_prefix="E2E40-WK",
        ),
    ]

    calls: dict[str, object] = {}
    records: dict[str, object] = {}

    harness.record = lambda name, payload: records.setdefault(name, payload)
    harness.log = lambda message: calls.setdefault("log_first", message)
    harness.clear_active_e2e_jobs = lambda *, reason: calls.setdefault("clear_reason", reason)
    harness.pve_teardown_scope_vms = lambda *, include_cxw: [
        {"vmid": 114, "name": "E2E30-DC01"},
        {"vmid": 118, "name": "E2E30-WK-01"},
        {"vmid": 125, "name": "E2E40-WK-01"},
    ]
    harness.discover_orphan_runs_and_bubbles = lambda *, include_cxw: {
        "cloudosd": ["cloud-run-1", "cloud-run-2"],
        "osdeploy": ["osd-run-1"],
        "bubbles": ["bubble-30", "bubble-40"],
    }
    harness.teardown = lambda: calls.setdefault("teardown_called", True)

    def fail(*args, **kwargs):
        raise AssertionError("teardown_only must not invoke run-body methods")

    harness.run_baseline_cxw = fail
    harness.create_sdn_objects = fail
    harness.create_lab_record = fail
    harness.create_dc_run = fail
    harness.wait_for_runs_complete = fail

    harness.teardown_only()

    assert calls["teardown_called"] is True
    assert calls["clear_reason"] == "teardown_only"
    assert harness.created_vmids == [114, 118, 125]
    assert harness.created_cloudosd_runs == ["cloud-run-1", "cloud-run-2"]
    assert harness.created_osdeploy_runs == ["osd-run-1"]
    assert harness.created_bubbles == ["bubble-30", "bubble-40"]
    assert e2e.SdnObject(kind="subnet", vnet="e2ev30", name="10.77.30.0/24") in harness.created_sdn
    assert e2e.SdnObject(kind="vnet", name="e2ev30") in harness.created_sdn
    assert e2e.SdnObject(kind="zone", name="e2ez30") in harness.created_sdn
    assert e2e.SdnObject(kind="subnet", vnet="e2ev40", name="10.77.40.0/24") in harness.created_sdn
    assert e2e.SdnObject(kind="vnet", name="e2ev40") in harness.created_sdn
    assert e2e.SdnObject(kind="zone", name="e2ez40") in harness.created_sdn
    assert not any(obj.name == "cxwlab1" or obj.name == "cxwz1" for obj in harness.created_sdn)
    assert harness.cxw_baseline_captured is False
    assert "teardown_only_vms_discovered" in records
    assert "teardown_only_db_discovered" in records


def test_teardown_only_with_include_cxw_adds_cxw_sdn_and_sets_baseline_flag(tmp_path):
    e2e = _load_module()

    harness = object.__new__(e2e.StressHarness)
    harness.args = SimpleNamespace(include_existing_cxw=True)
    harness.evidence_dir = tmp_path
    harness.created_vmids = []
    harness.created_cloudosd_runs = []
    harness.created_osdeploy_runs = []
    harness.created_bubbles = []
    harness.created_sdn = []
    harness.cxw_baseline_captured = False
    harness.labs = [
        e2e.LabSpec(
            name="E2E Stress Lab 30",
            zone="e2ez30",
            vnet="e2ev30",
            cidr="10.77.30.0/24",
            gateway="10.77.30.1",
            domain="e2e30.lab",
            netbios="E2E30",
            dc_name="E2E30-DC01",
            dc_ip="10.77.30.100",
            workstation_prefix="E2E30-WK",
        ),
    ]

    harness.record = lambda name, payload: None
    harness.log = lambda message: None
    harness.clear_active_e2e_jobs = lambda *, reason: None
    harness.pve_teardown_scope_vms = lambda *, include_cxw: []
    harness.discover_orphan_runs_and_bubbles = lambda *, include_cxw: {
        "cloudosd": [],
        "osdeploy": [],
        "bubbles": [],
    }
    harness.teardown = lambda: None

    harness.teardown_only()

    assert harness.cxw_baseline_captured is True
    assert e2e.SdnObject(kind="vnet", name="cxwlab1") in harness.created_sdn
    assert e2e.SdnObject(kind="zone", name="cxwz1") in harness.created_sdn
    assert e2e.SdnObject(kind="subnet", vnet="cxwlab1", name="10.77.20.0/24") in harness.created_sdn


def test_main_dispatches_to_teardown_only_when_flag_set(monkeypatch, tmp_path):
    e2e = _load_module()
    dispatched: dict[str, bool] = {}

    class FakeHarness:
        def __init__(self, args):
            self.evidence_dir = tmp_path

        def run(self):
            dispatched["run"] = True

        def teardown_only(self):
            dispatched["teardown_only"] = True

        def record(self, name, payload):
            pass

        def log(self, message):
            pass

    monkeypatch.setattr(e2e, "StressHarness", FakeHarness)

    rc = e2e.main([
        "--teardown-only",
        "--evidence-dir",
        str(tmp_path),
    ])

    assert rc == 0
    assert dispatched == {"teardown_only": True}
