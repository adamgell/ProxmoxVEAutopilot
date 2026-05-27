#!/usr/bin/env python3
"""Destructive maximum-stress SDN isolated lab E2E harness.

This script is intentionally operator-facing rather than a product API. It
drives existing ProxmoxVEAutopilot runtime surfaces, records sanitized evidence,
and tears down every E2E asset it creates when requested.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import shlex
import string
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SECRET_KEY_RE = re.compile(
    r"(password|secret|token|key|credential|bearer|dsrm)",
    re.IGNORECASE,
)
CONTROLLER_APP_DIR = "/opt/ProxmoxVEAutopilot/autopilot-proxmox"
PUBLIC_EGRESS_URL = "https://www.msftconnecttest.com/connecttest.txt"
DEFAULT_CLOUDOSD_OS_VERSION = "Windows 11 24H2"
DEFAULT_CLOUDOSD_OS_ACTIVATION = "Volume"
DEFAULT_CLOUDOSD_OS_EDITION = "Enterprise"
DEFAULT_CLOUDOSD_OS_LANGUAGE = "en-us"
PLANNED_VMIDS = {
    "E2E30-DC01": 114,
    "E2E40-DC01": 115,
    "E2E30-WK-01": 118,
    "E2E30-WK-02": 119,
    "E2E30-WK-03": 121,
    "E2E30-WK-04": 122,
    "E2E30-BAD-01": 123,
    "E2E40-WK-01": 125,
    "E2E40-WK-02": 126,
    "E2E40-WK-03": 127,
    "E2E40-WK-04": 128,
}
CLOUDOSD_ARTIFACT_COMPONENTS = (
    "PVEAutopilot-FirstBoot.ps1",
)


class E2EError(RuntimeError):
    """Base harness failure."""


class E2ETimeout(E2EError):
    """Raised when a polling condition is not satisfied before timeout."""

    def __init__(self, message: str, *, last_observation: Any = None) -> None:
        super().__init__(message)
        self.last_observation = last_observation


@dataclass(frozen=True)
class LabSpec:
    name: str
    zone: str
    vnet: str
    cidr: str
    gateway: str
    domain: str
    netbios: str
    dc_name: str
    dc_ip: str
    workstation_prefix: str

    @property
    def dhcp_start(self) -> str:
        return self.cidr.rsplit(".", 1)[0] + ".100"

    @property
    def dhcp_end(self) -> str:
        return self.cidr.rsplit(".", 1)[0] + ".199"

    @property
    def dc_fqdn(self) -> str:
        return f"{self.dc_name}.{self.domain}"

    @property
    def bootstrap_dns(self) -> str:
        # The DC must resolve the controller from WinPE before AD DNS exists.
        return self.gateway

    @property
    def subnet_id(self) -> str:
        return f"{self.zone}-{self.cidr.replace('.', '.').replace('/', '-')}"

    @property
    def proof_user(self) -> str:
        return "e2eproof"

    @property
    def proof_user_upn(self) -> str:
        return f"{self.proof_user}@{self.domain}"

    @property
    def proof_user_sam(self) -> str:
        return f"{self.netbios}\\{self.proof_user}"


@dataclass(frozen=True)
class SdnObject:
    kind: str
    name: str
    vnet: str = ""


@dataclass(frozen=True)
class TeardownAction:
    name: str
    kind: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TeardownResources:
    vmids: list[int] = field(default_factory=list)
    cloudosd_run_ids: list[str] = field(default_factory=list)
    osdeploy_run_ids: list[str] = field(default_factory=list)
    bubble_ids: list[str] = field(default_factory=list)
    sdn_objects: list[SdnObject] = field(default_factory=list)


@dataclass(frozen=True)
class CloudOsdJoinSequence:
    sequence_id: int
    credential_id: int


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def safe_record_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "record"


def redact_secrets(value: Any) -> Any:
    """Return a recursive copy with secret-like keys masked."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    return value


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_cloudosd_artifact_component_hashes(tool_root: Path | None = None) -> dict[str, str]:
    root = tool_root or (Path(__file__).resolve().parents[2] / "tools" / "cloudosd-build")
    return {
        name: sha256_file(root / name)
        for name in CLOUDOSD_ARTIFACT_COMPONENTS
    }


def validate_cloudosd_artifact_manifest(manifest: dict[str, Any], expected_hashes: dict[str, str]) -> list[str]:
    failures: list[str] = []
    components = manifest.get("component_sha256")
    if not isinstance(components, dict):
        failures.append("artifact_manifest_missing_component_sha256")
        components = {}
    for name, expected in expected_hashes.items():
        actual = str(components.get(name) or "").lower()
        if not actual:
            failures.append(f"artifact_manifest_missing_{name}_sha256")
        elif actual != expected.lower():
            failures.append(f"artifact_manifest_stale_{name}_sha256")
    return failures


def redact_known_secret_values(value: Any, secrets: Iterable[str]) -> Any:
    """Return a recursive copy with known secret values masked inside strings."""
    needles = [secret for secret in secrets if secret]
    if isinstance(value, dict):
        return {str(key): redact_known_secret_values(item, needles) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_known_secret_values(item, needles) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_known_secret_values(item, needles) for item in value)
    if isinstance(value, str):
        out = value
        for secret in needles:
            out = out.replace(secret, "[REDACTED]")
        return out
    return value


def parse_qga_json_output(raw: str) -> dict[str, Any]:
    """Parse `qm guest exec` JSON and decode a JSON object from out-data."""
    try:
        qga = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise E2EError(f"QGA command did not return JSON: {exc}") from exc
    if int(qga.get("exitcode", 1)) != 0:
        raise E2EError(f"QGA command exited {qga.get('exitcode')}: {qga.get('err-data') or qga}")
    text = (
        str(qga.get("out-data") or "")
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .strip()
    )
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise E2EError(f"QGA out-data did not contain JSON: {text[:500]}") from exc


def failed_required_job_rows(status: dict[str, Any], required_job_by_run: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Return required run ids whose backing provision jobs have failed."""
    job_to_run = {job_id: run_id for run_id, job_id in required_job_by_run.items() if job_id}
    failed: dict[str, dict[str, Any]] = {}
    for row in status.get("jobs") or []:
        job_id = str(row.get("id") or "")
        run_id = job_to_run.get(job_id)
        if not run_id:
            continue
        job_status = str(row.get("status") or "")
        exit_code = row.get("exit_code")
        if job_status == "failed" or (job_status == "complete" and exit_code not in (None, 0)):
            failed[run_id] = dict(row)
    return failed


def _norm_name(value: Any) -> str:
    return str(value or "").strip().rstrip(".").lower()


def validate_workstation_proof(proof: dict[str, Any], spec: LabSpec) -> list[str]:
    """Return validation failure ids for a workstation domain-auth proof."""
    failures: list[str] = []
    dc_fqdn = _norm_name(spec.dc_fqdn)
    if _norm_name(proof.get("domain")) != _norm_name(spec.domain):
        failures.append("domain_mismatch")
    if proof.get("part_of_domain") is not True:
        failures.append("not_part_of_domain")
    if spec.dc_ip not in [str(item) for item in proof.get("dns_servers", [])]:
        failures.append("dns_server_not_dc")
    for key in ("dc_port_53", "dc_port_88", "dc_port_389"):
        if proof.get(key) is not True:
            failures.append(f"{key}_closed")
    if proof.get("dsgetdc_found") is not True:
        failures.append("dsgetdc_not_found")
    if _norm_name(proof.get("dsgetdc_dc")) != dc_fqdn:
        failures.append("dsgetdc_wrong_dc")
    if str(proof.get("dsgetdc_address") or "").strip() != spec.dc_ip:
        failures.append("dsgetdc_wrong_ip")
    srv_records = proof.get("srv_records") or []
    srv_targets = {_norm_name(record.get("NameTarget")) for record in srv_records if isinstance(record, dict)}
    if proof.get("srv_lookup_ok") is not True or dc_fqdn not in srv_targets:
        failures.append("srv_lookup_wrong_dc")
    if proof.get("secure_channel_ok") is not True:
        failures.append("secure_channel_failed")
    if _norm_name(proof.get("trusted_dc")) != dc_fqdn:
        failures.append("secure_channel_wrong_dc")
    if proof.get("kerberos_ok") is not True:
        failures.append("machine_kerberos_failed")
    if _norm_name(proof.get("kdc_called")) != dc_fqdn:
        failures.append("kerberos_wrong_kdc")
    user_auth = proof.get("user_auth") or {}
    if user_auth.get("ok") is not True:
        failures.append("user_auth_failed")
    if _norm_name(user_auth.get("whoami")) != _norm_name(spec.proof_user_sam):
        failures.append("user_identity_wrong")
    if user_auth.get("ldap_bind_ok") is not True:
        failures.append("user_ldap_bind_failed")
    if user_auth.get("cifs_kerberos_ok") is not True:
        failures.append("user_cifs_kerberos_failed")
    if user_auth.get("share_write_read_ok") is not True:
        failures.append("user_share_write_read_failed")
    if user_auth.get("bad_password_rejected") is not True:
        failures.append("bad_password_not_rejected")
    return failures


def build_cloudosd_domain_join_config(
    spec: LabSpec,
    sequence: CloudOsdJoinSequence,
) -> dict[str, Any]:
    return {
        "enabled": True,
        "source_sequence_id": sequence.sequence_id,
        "credential_id": sequence.credential_id,
        "domain_fqdn": spec.domain,
        "credential_domain": spec.netbios,
        "domain_controller_ipv4": spec.dc_ip,
        "ou_path": "",
        "acceptable_domain_names": [spec.domain, spec.netbios],
    }


def validate_cloudosd_domain_join_plan(
    run: dict[str, Any],
    plan_kinds: Iterable[str],
) -> list[str]:
    failures: list[str] = []
    domain_join = run.get("domain_join") or {}
    kinds = [str(kind) for kind in plan_kinds]
    if domain_join.get("enabled") is not True:
        failures.append("domain_join_not_enabled")
    if not domain_join.get("credential_id"):
        failures.append("domain_join_missing_credential")
    if not domain_join.get("domain_controller_ipv4"):
        failures.append("domain_join_missing_dc_ip")
    if "stage_ad_domain_join_unattend" in kinds:
        failures.append("unexpected_pe_unattend_join")
    if "join_domain_role" not in kinds:
        failures.append("missing_join_domain_role")
    if "verify_ad_domain_join" not in kinds:
        failures.append("missing_verify_ad_domain_join")
    if "join_domain_role" in kinds and "verify_ad_domain_join" in kinds:
        if kinds.index("join_domain_role") > kinds.index("verify_ad_domain_join"):
            failures.append("join_after_verify")
    return failures


def negative_join_signal(rows: Iterable[dict[str, Any]], events: Iterable[dict[str, Any]]) -> bool:
    row_items = list(rows)
    event_items = list(events)
    if any(str(row.get("cursor_kind") or "") == "join_domain_role" for row in row_items):
        return True
    text = json.dumps(redact_secrets(event_items), default=str).lower()
    return "join_domain_role" in text or "domain_join" in text or "join domain" in text


def negative_disk_boot_restart_candidate(row: dict[str, Any]) -> bool:
    state = str(row.get("ts_state") or row.get("state") or "")
    try:
        vmid = int(row.get("vmid") or 0)
    except (TypeError, ValueError):
        vmid = 0
    return (
        state == "pe_registered"
        and vmid > 0
        and bool(row.get("osdcloud_finished_at"))
        and not row.get("first_heartbeat_at")
    )


def build_teardown_actions(resources: TeardownResources) -> list[TeardownAction]:
    actions: list[TeardownAction] = []
    for vmid in resources.vmids:
        actions.append(TeardownAction(name=f"qm_stop_{vmid}", kind="qm_stop", args={"vmid": vmid}))
        actions.append(TeardownAction(name=f"qm_destroy_{vmid}", kind="qm_destroy", args={"vmid": vmid}))
    for run_id in resources.cloudosd_run_ids:
        actions.append(TeardownAction(name=f"archive_cloudosd_{run_id}", kind="archive_cloudosd", args={"run_id": run_id}))
    for run_id in resources.osdeploy_run_ids:
        actions.append(TeardownAction(name=f"archive_osdeploy_{run_id}", kind="archive_osdeploy", args={"run_id": run_id}))
    for bubble_id in resources.bubble_ids:
        actions.append(TeardownAction(name=f"delete_bubble_{bubble_id}", kind="delete_bubble", args={"bubble_id": bubble_id}))

    order = {"subnet": 0, "vnet": 1, "zone": 2}
    for obj in sorted(resources.sdn_objects, key=lambda item: order.get(item.kind, 99)):
        if obj.kind == "subnet":
            actions.append(
                TeardownAction(
                    name=f"delete_subnet_{obj.vnet}_{obj.name}",
                    kind="delete_subnet",
                    args={"vnet": obj.vnet, "subnet": obj.name},
                )
            )
        elif obj.kind == "vnet":
            actions.append(TeardownAction(name=f"delete_vnet_{obj.name}", kind="delete_vnet", args={"vnet": obj.name}))
        elif obj.kind == "zone":
            actions.append(TeardownAction(name=f"delete_zone_{obj.name}", kind="delete_zone", args={"zone": obj.name}))
    if resources.sdn_objects:
        actions.append(TeardownAction(name="apply_sdn", kind="apply_sdn"))
    return actions


def should_include_existing_cxw(include_existing_cxw: bool, baseline_captured: bool) -> bool:
    """Existing CXW assets may only be destroyed after baseline evidence exists."""
    return include_existing_cxw and baseline_captured


def vm_name_in_teardown_scope(name: str, *, include_cxw: bool) -> bool:
    value = str(name or "")
    return value.startswith(("E2E30-", "E2E40-")) or (include_cxw and value.startswith("CXW-"))


def planned_vmid_for_name(name: str) -> int | None:
    return PLANNED_VMIDS.get(str(name or ""))


def build_sdn_create_commands(spec: LabSpec) -> list[str]:
    return [
        "pvesh create /cluster/sdn/zones "
        f"--zone {shlex.quote(spec.zone)} --type simple --dhcp dnsmasq --ipam pve",
        "pvesh create /cluster/sdn/vnets "
        f"--vnet {shlex.quote(spec.vnet)} --zone {shlex.quote(spec.zone)} --alias {shlex.quote(spec.name)}",
        f"pvesh create /cluster/sdn/vnets/{shlex.quote(spec.vnet)}/subnets "
        f"--subnet {shlex.quote(spec.cidr)} "
        "--type subnet "
        f"--gateway {shlex.quote(spec.gateway)} "
        "--snat 1 "
        f"--dhcp-dns-server {shlex.quote(spec.bootstrap_dns)} "
        f"--dhcp-range {shlex.quote('start-address=' + spec.dhcp_start + ',end-address=' + spec.dhcp_end)}",
    ]


def build_sdn_set_dns_commands(spec: LabSpec, dns_server: str) -> list[str]:
    return [
        f"pvesh set /cluster/sdn/vnets/{shlex.quote(spec.vnet)}/subnets/{shlex.quote(candidate)} "
        f"--dhcp-dns-server {shlex.quote(dns_server)}"
        for candidate in subnet_delete_candidates(SdnObject(kind="subnet", vnet=spec.vnet, name=spec.cidr), [spec])
    ]


def subnet_delete_candidates(obj: SdnObject, labs: Iterable[LabSpec]) -> list[str]:
    candidates: list[str] = []
    if "/" in obj.name:
        zone = next((lab.zone for lab in labs if lab.vnet == obj.vnet), "")
        if zone:
            candidates.append(f"{zone}-{obj.name.replace('/', '-')}")
    candidates.append(obj.name)
    return list(dict.fromkeys(candidates))


def sdn_lock_token_from_response(lock_response: Any) -> str:
    if isinstance(lock_response, str):
        return lock_response.strip()
    if isinstance(lock_response, dict):
        return str(
            lock_response.get("lock-token")
            or lock_response.get("lock_token")
            or lock_response.get("token")
            or ""
        ).strip()
    return ""


def sdn_delete_error_is_missing(error: str) -> bool:
    text = error.lower()
    return any(
        marker in text
        for marker in (
            "does not exist",
            "doesn't exist",
            "not found",
            "no such",
        )
    )


def is_transient_controller_error(error: str) -> bool:
    text = error.lower()
    return any(
        marker in text
        for marker in (
            "deadlock detected",
            "deadlockdetected",
            "serializationfailure",
            "could not serialize access",
            "locknotavailable",
        )
    )


def is_transient_qga_exec_status_error(error: str) -> bool:
    text = error.lower()
    return "guest-exec-status" in text and any(
        marker in text
        for marker in (
            "got timeout",
            "timed out",
            "timeout",
        )
    )


def qga_ping_succeeded(result: subprocess.CompletedProcess[str]) -> bool:
    text = (result.stdout or result.stderr or "").strip().lower()
    if "guest agent is not running" in text or "qemu guest agent is not running" in text:
        return False
    if "not running" in text and "guest agent" in text:
        return False
    return result.returncode == 0 or "success" in text or text == "{}"


def osdeploy_request_fields_from_artifact(artifact: dict[str, Any] | None) -> dict[str, str]:
    artifact = artifact or {}
    return {
        "os_version": str(artifact.get("os_version") or "Windows Server 2025"),
        "os_edition": str(artifact.get("os_edition") or "Datacenter"),
        "os_language": str(artifact.get("os_language") or "en-us"),
    }


def select_ready_cloudosd_feature_image(
    rows: Iterable[dict[str, Any]],
    *,
    os_version: str,
    architecture: str = "amd64",
    language: str = DEFAULT_CLOUDOSD_OS_LANGUAGE,
    activation: str = DEFAULT_CLOUDOSD_OS_ACTIVATION,
    edition: str = DEFAULT_CLOUDOSD_OS_EDITION,
) -> dict[str, Any] | None:
    """Pick the exact ready cached feature image the stress run will request."""
    for row in rows:
        if str(row.get("entry_type") or "") != "feature_image":
            continue
        if str(row.get("status") or "") != "ready":
            continue
        if str(row.get("windows_version") or "") != os_version:
            continue
        if str(row.get("architecture") or "") != architecture:
            continue
        if str(row.get("language") or "") != language:
            continue
        if str(row.get("activation") or "") != activation:
            continue
        if str(row.get("edition") or "") != edition:
            continue
        return dict(row)
    return None


def validate_cloudosd_cache_file_stat(entry: dict[str, Any], stat: dict[str, Any]) -> list[str]:
    """Return preflight failures for the cached feature-image backing file."""
    failures: list[str] = []
    if stat.get("exists") is not True:
        failures.append("cache_file_missing")
    expected_size = int(entry.get("expected_size_bytes") or entry.get("size_bytes") or 0)
    observed_size = int(stat.get("size_bytes") or 0)
    if expected_size and observed_size and observed_size != expected_size:
        failures.append("cache_file_size_mismatch")
    elif expected_size and not observed_size:
        failures.append("cache_file_size_missing")
    return failures


def e2e_credential_name(netbios: str, purpose: str, stamp: str) -> str:
    return f"e2e-{netbios.lower()}-{purpose}-{stamp}"


def domain_join_admin_username(spec: LabSpec) -> str:
    return f"Administrator@{spec.domain}"


def wait_until(
    predicate: Callable[[], tuple[bool, Any]],
    *,
    timeout_seconds: float,
    interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    start = monotonic()
    last_observation: Any = None
    while True:
        ok, observation = predicate()
        last_observation = observation
        if ok:
            return observation
        if monotonic() - start >= timeout_seconds:
            raise E2ETimeout("condition timed out", last_observation=last_observation)
        sleep(interval_seconds)


def scan_secret_leaks(root: Path, secrets: Iterable[str]) -> list[str]:
    leaks: list[str] = []
    needles = [secret for secret in secrets if secret]
    if not needles:
        return leaks
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for secret in needles:
            if secret in text:
                leaks.append(str(path))
                break
    return leaks


def _process_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def redact_command_text(text: str, secrets: Iterable[str] = ()) -> str:
    out = str(text or "")
    out = re.sub(r"(-EncodedCommand\s+)[A-Za-z0-9+/=]+", r"\1[REDACTED]", out)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out


def skill_status_result_payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "exit_code": result.returncode,
        "stdout": _process_output_text(result.stdout),
        "stderr": _process_output_text(result.stderr),
        "timed_out": False,
        "mcp_docs_available": result.returncode == 0,
    }


def skill_status_timeout_payload(exc: subprocess.TimeoutExpired) -> dict[str, Any]:
    return {
        "exit_code": None,
        "stdout": _process_output_text(exc.output),
        "stderr": _process_output_text(exc.stderr),
        "timed_out": True,
        "timeout_seconds": exc.timeout,
        "mcp_docs_available": False,
    }


def command_timeout_payload(exc: subprocess.TimeoutExpired) -> dict[str, Any]:
    return {
        "exit_code": None,
        "stdout": _process_output_text(exc.output),
        "stderr": _process_output_text(exc.stderr),
        "timed_out": True,
        "timeout_seconds": exc.timeout,
    }


def _ps_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _pwsh_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def _random_password(prefix: str) -> str:
    alphabet = string.ascii_letters + string.digits
    return f"{prefix}!" + "".join(random.SystemRandom().choice(alphabet) for _ in range(18)) + "7a!"


class StressHarness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.controller = args.controller
        self.pve = args.pve
        self.node = args.node
        self.evidence_dir = Path(args.evidence_dir).resolve()
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.secrets: list[str] = []
        self.created_vmids: list[int] = []
        self.created_cloudosd_runs: list[str] = []
        self.created_osdeploy_runs: list[str] = []
        self.created_bubbles: list[str] = []
        self.created_sdn: list[SdnObject] = []
        self.released_negative_jobs: set[str] = set()
        self.negative_disk_boot_restarts: set[str] = set()
        self.osdeploy_artifact: dict[str, Any] = {}
        self.cloudosd_artifact: dict[str, Any] = {}
        self.cloudosd_os_version = args.cloudosd_os_version
        self.cloudosd_feature_image_cache: dict[str, Any] = {}
        self.cxw_vmids = [114, 115, 118, 119, 121]
        self.cxw_present_vmids: list[int] = []
        self.cxw_baseline_captured = False
        self.triggered_reboots: set[str] = set()
        self.labs = [
            LabSpec(
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
            LabSpec(
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
        self.lab_contexts: dict[str, dict[str, Any]] = {}

    def remember_vmid(self, value: Any) -> int:
        try:
            vmid = int(value or 0)
        except (TypeError, ValueError):
            return 0
        if vmid > 0 and vmid not in self.created_vmids:
            self.created_vmids.append(vmid)
        return vmid

    def sync_run_status(self, status: dict[str, Any]) -> dict[str, Any]:
        rows = list(status.get("osdeploy") or []) + list(status.get("cloudosd") or [])
        tracked_runs = set(self.created_osdeploy_runs) | set(self.created_cloudosd_runs)
        for row in rows:
            run_id = str(row.get("run_id") or "")
            if run_id not in tracked_runs:
                continue
            vmid = self.remember_vmid(row.get("vmid"))
            if not vmid:
                continue
            for spec in self.labs:
                ctx = self.lab_contexts.get(spec.domain) or {}
                if str(ctx.get("dc_run_id") or "") == run_id:
                    ctx["dc_vmid"] = vmid
        return status

    def refresh_tracked_vmids_from_db(self) -> dict[str, Any]:
        run_ids = list(dict.fromkeys(self.created_osdeploy_runs + self.created_cloudosd_runs))
        if not run_ids:
            return {}
        status = self.fetch_run_status(run_ids)
        self.sync_run_status(status)
        self.record("tracked_vmids_refreshed", status)
        return status

    def attach_vmids_to_run_items(self, items: list[dict[str, Any]], status: dict[str, Any]) -> None:
        rows = list(status.get("osdeploy") or []) + list(status.get("cloudosd") or [])
        vmid_by_run = {
            str(row.get("run_id") or ""): int(row.get("vmid") or 0)
            for row in rows
            if int(row.get("vmid") or 0) > 0
        }
        for item in items:
            run = item.get("run") or {}
            vmid = vmid_by_run.get(str(run.get("run_id") or ""))
            if vmid:
                run["vmid"] = vmid

    def record(self, name: str, payload: Any) -> None:
        path = self.evidence_dir / f"{safe_record_name(name)}.json"
        sanitized = redact_known_secret_values(redact_secrets(payload), self.secrets)
        path.write_text(
            json.dumps(sanitized, indent=2, sort_keys=True, default=_json_default),
            encoding="utf-8",
        )

    def log(self, message: str) -> None:
        line = f"[{_utc_stamp()}] {message}"
        print(line, flush=True)
        with (self.evidence_dir / "events.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def run_cmd(
        self,
        cmd: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 300,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            cmd,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            rendered_cmd = redact_command_text(" ".join(cmd), self.secrets)
            stdout = redact_command_text(result.stdout[-2000:], self.secrets)
            stderr = redact_command_text(result.stderr[-2000:], self.secrets)
            raise E2EError(
                f"command failed ({result.returncode}): {rendered_cmd}\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        return result

    def ssh(self, host: str, command: str, *, input_text: str | None = None, timeout: int = 300, check: bool = True) -> str:
        result = self.ssh_result(host, command, input_text=input_text, timeout=timeout, check=check)
        return result.stdout

    def ssh_result(
        self,
        host: str,
        command: str,
        *,
        input_text: str | None = None,
        timeout: int = 300,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_cmd(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
            input_text=input_text,
            timeout=timeout,
            check=check,
        )

    def pve_cmd(self, command: str, *, timeout: int = 300, check: bool = True) -> str:
        return self.ssh(self.pve, command, timeout=timeout, check=check)

    def controller_cmd(self, command: str, *, input_text: str | None = None, timeout: int = 300, check: bool = True) -> str:
        return self.ssh(self.controller, command, input_text=input_text, timeout=timeout, check=check)

    def controller_python(self, code: str, *, timeout: int = 300) -> Any:
        command = f"cd {shlex.quote(CONTROLLER_APP_DIR)} && docker compose exec -T autopilot python -"
        last_exc: E2EError | None = None
        for attempt in range(1, 5):
            try:
                stdout = self.controller_cmd(command, input_text=code, timeout=timeout)
                break
            except E2EError as exc:
                if not is_transient_controller_error(str(exc)) or attempt == 4:
                    raise
                last_exc = exc
                self.record(
                    f"controller_python_retry_{attempt}_{_utc_stamp()}",
                    {"attempt": attempt, "error": str(exc)[-2000:]},
                )
                self.log(f"controller Python hit transient DB lock/deadlock; retrying attempt {attempt + 1}/4")
                time.sleep(min(10, attempt * 2))
        else:
            raise last_exc or E2EError("controller python failed without an exception")
        text = stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text.splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise E2EError(f"controller python did not end with JSON: {text[-1000:]}") from exc

    def pvesh_json(self, command: str, *, timeout: int = 300, check: bool = True) -> Any:
        stdout = self.pve_cmd(f"{command} --output-format json", timeout=timeout, check=check)
        text = stdout.strip()
        if not text:
            return None
        return json.loads(text)

    def qm_guest_exec_json(self, vmid: int, script: str, *, timeout: int = 300) -> dict[str, Any]:
        encoded = _pwsh_encoded(script)
        raw = self.pve_cmd(
            f"qm guest exec {int(vmid)} -- powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}",
            timeout=timeout,
        )
        try:
            initial = json.loads(raw)
        except json.JSONDecodeError:
            initial = {}
        if isinstance(initial, dict) and initial.get("pid") and "exitcode" not in initial:
            pid = int(initial["pid"])

            def predicate() -> tuple[bool, Any]:
                try:
                    status_raw = self.pve_cmd(f"qm guest exec-status {int(vmid)} {pid}", timeout=30)
                except E2EError as exc:
                    if is_transient_qga_exec_status_error(str(exc)):
                        return False, str(exc)
                    raise
                try:
                    status = json.loads(status_raw)
                except json.JSONDecodeError:
                    return False, status_raw
                return bool(status.get("exited")) or "exitcode" in status, status_raw

            raw = wait_until(predicate, timeout_seconds=timeout, interval_seconds=5)
        return parse_qga_json_output(raw)

    def clear_active_e2e_jobs(self, *, reason: str) -> dict[str, Any]:
        payload = self.controller_python(
            """
import json
from web import db_pg
where = '''
(
  job_type in ('provision_osdeploy', 'provision_cloudosd')
  and status in ('pending', 'running')
  and (
    args_json->>'vm_name' like 'E2E30-%'
    or args_json->>'vm_name' like 'E2E40-%'
    or args_json->>'vm_name' like 'CXW-%'
  )
)
'''
with db_pg.connect() as conn:
    before = [dict(row) for row in conn.execute(
        f"select id, job_type, status, args_json->>'vm_name' as vm_name from jobs where {where} order by created_at"
    ).fetchall()]
    pending = [dict(row) for row in conn.execute(
        f\"\"\"
        update jobs
        set status = 'failed',
            exit_code = -9,
            ended_at = now(),
            kill_requested = false
        where {where} and status = 'pending'
        returning id, job_type, status, args_json->>'vm_name' as vm_name
        \"\"\"
    ).fetchall()]
    running = [dict(row) for row in conn.execute(
        f\"\"\"
        update jobs
        set kill_requested = true
        where {where} and status = 'running'
        returning id, job_type, status, args_json->>'vm_name' as vm_name
        \"\"\"
    ).fetchall()]
    conn.commit()
print(json.dumps({'before': before, 'pending_failed': pending, 'running_kill_requested': running}, default=str))
""",
            timeout=120,
        )
        running = payload.get("running_kill_requested") or []
        if running:
            self.controller_cmd(
                "for c in $(docker ps --format '{{.Names}}' | grep autopilot-builder); do "
                "docker exec \"$c\" sh -lc 'pkill -TERM -f \"provision_proxmox_.*E2E\" || true'; "
                "done",
                timeout=60,
                check=False,
            )
            time.sleep(5)
            after = self.controller_python(
                """
import json
from web import db_pg
with db_pg.connect() as conn:
    forced = [dict(row) for row in conn.execute(
        \"\"\"
        update jobs
        set status = 'failed',
            exit_code = -9,
            ended_at = now(),
            kill_requested = false
        where job_type in ('provision_osdeploy', 'provision_cloudosd')
          and status = 'running'
          and (
            args_json->>'vm_name' like 'E2E30-%'
            or args_json->>'vm_name' like 'E2E40-%'
            or args_json->>'vm_name' like 'CXW-%'
          )
        returning id, job_type, status, args_json->>'vm_name' as vm_name
        \"\"\"
    ).fetchall()]
    conn.commit()
print(json.dumps({'running_forced_failed': forced}, default=str))
""",
                timeout=120,
            )
            payload["after_kill"] = after
        self.record(f"{reason}_e2e_job_cleanup", payload)
        return payload

    def preflight(self) -> None:
        self.log("running skill.sh status")
        try:
            skill = self.run_cmd(["./skill.sh", "status"], timeout=45, check=False)
            skill_payload = skill_status_result_payload(skill)
        except subprocess.TimeoutExpired as exc:
            skill_payload = skill_status_timeout_payload(exc)
            self.log("skill.sh status timed out; continuing from local repo and live runtime evidence")
        self.record("skill_status", skill_payload)

        self.clear_active_e2e_jobs(reason="preflight")

        self.log("checking live stack, artifacts, queue caps, and PVE capacity")
        data = self.controller_python(
            f"""
import json
from web import db_pg
with db_pg.connect() as conn:
    rows = {{}}
    rows['osdeploy_artifact'] = conn.execute("select id, build_sha, proxmox_volid, image_name, os_version, os_edition, os_language from osdeploy_artifacts where id = %s", ({self.args.osdeploy_artifact!r},)).fetchone()
    rows['cloudosd_artifact'] = conn.execute("select id, build_sha, proxmox_volid, manifest_path, built_at from cloudosd_artifacts where id = %s", ({self.args.cloudosd_artifact!r},)).fetchone()
    rows['limits'] = conn.execute("select job_type, max_concurrent from job_type_limits where job_type in ('provision_cloudosd','provision_osdeploy')").fetchall()
    rows['cloudosd_feature_image_cache'] = conn.execute(
        '''
        select id, entry_type, status, windows_version, architecture, language,
               activation, edition, file_name, local_path, size_bytes,
               expected_size_bytes, sha256, expected_sha256, verified_at, updated_at
        from cloudosd_cache_entries
        where entry_type = 'feature_image'
          and architecture = 'amd64'
          and language = %s
          and activation = %s
          and edition = %s
        order by case when status = 'ready' then 0 else 1 end,
                 verified_at desc nulls last,
                 updated_at desc
        ''',
        ({DEFAULT_CLOUDOSD_OS_LANGUAGE!r}, {DEFAULT_CLOUDOSD_OS_ACTIVATION!r}, {DEFAULT_CLOUDOSD_OS_EDITION!r}),
    ).fetchall()
print(json.dumps({{k: (dict(v) if hasattr(v, 'keys') else [dict(x) for x in v]) for k,v in rows.items()}}, default=str))
"""
        )
        status = self.pvesh_json(f"pvesh get /nodes/{shlex.quote(self.node)}/status")
        storage = self.pvesh_json(f"pvesh get /nodes/{shlex.quote(self.node)}/storage")
        vms = self.pvesh_json(f"pvesh get /nodes/{shlex.quote(self.node)}/qemu")
        self.record("preflight_live_state", {"db": data, "status": status, "storage": storage, "vms": vms})

        if not data.get("osdeploy_artifact") or not data["osdeploy_artifact"].get("proxmox_volid"):
            raise E2EError("OSDeploy artifact missing or not published to Proxmox")
        if not data.get("cloudosd_artifact") or not data["cloudosd_artifact"].get("proxmox_volid"):
            raise E2EError("CloudOSD artifact missing or not published to Proxmox")
        self.osdeploy_artifact = data["osdeploy_artifact"]
        self.cloudosd_artifact = data["cloudosd_artifact"]
        artifact_manifest = self.controller_python(
            f"""
import json
from pathlib import Path
path = Path({str(self.cloudosd_artifact.get('manifest_path') or '')!r})
payload = {{'path': str(path), 'exists': path.is_file(), 'manifest': {{}}}}
if path.is_file():
    payload['manifest'] = json.loads(path.read_text())
print(json.dumps(payload, default=str))
""",
            timeout=120,
        )
        expected_component_hashes = expected_cloudosd_artifact_component_hashes()
        artifact_freshness_failures = validate_cloudosd_artifact_manifest(
            artifact_manifest.get("manifest") if isinstance(artifact_manifest, dict) else {},
            expected_component_hashes,
        )
        self.record(
            "cloudosd_artifact_manifest_verified",
            {
                "artifact": self.cloudosd_artifact,
                "manifest": artifact_manifest,
                "expected_component_hashes": expected_component_hashes,
                "failures": artifact_freshness_failures,
            },
        )
        if artifact_freshness_failures:
            raise E2EError(
                "CloudOSD artifact manifest does not match this harness source; "
                f"refusing stale artifact {self.args.cloudosd_artifact}: {artifact_freshness_failures}"
            )
        ready_cache = select_ready_cloudosd_feature_image(
            data.get("cloudosd_feature_image_cache") or [],
            os_version=self.cloudosd_os_version,
        )
        if not ready_cache:
            available = [
                {
                    "windows_version": row.get("windows_version"),
                    "status": row.get("status"),
                    "file_name": row.get("file_name"),
                }
                for row in data.get("cloudosd_feature_image_cache") or []
            ]
            raise E2EError(
                f"CloudOSD feature image cache is not ready for {self.cloudosd_os_version}; "
                f"refusing live stress fan-out against Microsoft ESD downloads. available={available}"
            )
        cache_stat = self.controller_python(
            f"""
import json
from pathlib import Path
path = Path({str(ready_cache.get('local_path') or '')!r})
payload = {{'path': str(path), 'exists': path.is_file()}}
if path.is_file():
    payload['size_bytes'] = path.stat().st_size
print(json.dumps(payload))
""",
            timeout=120,
        )
        cache_failures = validate_cloudosd_cache_file_stat(ready_cache, cache_stat or {})
        self.record(
            "cloudosd_feature_image_cache_verified",
            {"entry": ready_cache, "file": cache_stat, "failures": cache_failures},
        )
        if cache_failures:
            raise E2EError(
                f"CloudOSD feature image cache backing file is not usable for {self.cloudosd_os_version}: "
                f"{cache_failures}; file={cache_stat}"
            )
        self.cloudosd_feature_image_cache = ready_cache
        limits = {row["job_type"]: int(row["max_concurrent"]) for row in data.get("limits", [])}
        if limits.get("provision_cloudosd") != 4 or limits.get("provision_osdeploy") != 3:
            raise E2EError(f"unexpected provision limits: {limits}")
        available = int((status.get("memory") or {}).get("available") or 0)
        if available < 90 * 1024**3:
            raise E2EError(f"pve2 available memory below 90 GiB: {available}")
        nvmepool = next((item for item in storage if item.get("storage") == "nvmepool"), None)
        if not nvmepool or int(nvmepool.get("avail") or 0) < 900 * 1024**3:
            raise E2EError(f"nvmepool free space below 900 GiB: {nvmepool}")

        protected = []
        allowed_names = {"CXW-DC01", "CXW-W2-01", "CXW-W2-02", "CXW-W2-03R", "CXW-W2-04"}
        for item in vms:
            name = str(item.get("name") or "")
            vmid = int(item.get("vmid") or 0)
            if vmid in self.cxw_vmids and name not in allowed_names:
                protected.append({"vmid": vmid, "name": name})
        if protected:
            raise E2EError(f"refusing to touch non-E2E VMIDs in CXW range: {protected}")

    def create_sdn_objects(self, spec: LabSpec) -> None:
        self.log(f"creating SDN objects for {spec.vnet}")
        for command in build_sdn_create_commands(spec):
            self.pve_cmd(command)
        self.created_sdn.extend([
            SdnObject(kind="subnet", vnet=spec.vnet, name=spec.cidr),
            SdnObject(kind="vnet", name=spec.vnet),
            SdnObject(kind="zone", name=spec.zone),
        ])

    def set_lab_dhcp_dns(self, spec: LabSpec, dns_server: str) -> None:
        self.log(f"setting {spec.vnet} DHCP DNS server to {dns_server}")
        last_error = ""
        for command in build_sdn_set_dns_commands(spec, dns_server):
            result = self.ssh_result(self.pve, command, check=False)
            if result.returncode == 0:
                self.record(
                    f"{spec.netbios}_dhcp_dns_updated",
                    {"vnet": spec.vnet, "dns_server": dns_server, "command": command},
                )
                return
            last_error = (result.stderr or result.stdout or "").strip()
        raise E2EError(f"failed to set DHCP DNS for {spec.vnet} to {dns_server}: {last_error}")

    def switch_labs_to_dc_dns(self) -> None:
        for spec in self.labs:
            self.set_lab_dhcp_dns(spec, spec.dc_ip)
        self.apply_sdn()

    def apply_sdn(self) -> None:
        self.log("applying SDN pending changes")
        lock = self.pvesh_json("pvesh create /cluster/sdn/lock --allow-pending 1", check=True)
        token = sdn_lock_token_from_response(lock)
        if not token:
            raise E2EError(f"SDN lock token missing: {lock}")
        applied = False
        try:
            self.pve_cmd(f"pvesh set /cluster/sdn --lock-token {shlex.quote(str(token))} --release-lock 1", timeout=600)
            applied = True
        finally:
            if not applied:
                self.pve_cmd(
                    f"pvesh delete /cluster/sdn/lock --lock-token {shlex.quote(str(token))} --force 1",
                    check=False,
                )

    def delete_sdn_object(self, obj: SdnObject) -> None:
        if obj.kind == "subnet":
            last_error = ""
            for name in subnet_delete_candidates(obj, self.labs):
                result = self.ssh_result(
                    self.pve,
                    f"pvesh delete /cluster/sdn/vnets/{shlex.quote(obj.vnet)}/subnets/{shlex.quote(name)}",
                    check=False,
                )
                if result.returncode == 0:
                    return
                last_error = (result.stderr or result.stdout or "").strip()
            self.record(f"delete_subnet_warning_{obj.vnet}", {"subnet": obj.name, "last_error": last_error})
            if not sdn_delete_error_is_missing(last_error):
                raise E2EError(f"failed to delete subnet {obj.vnet}/{obj.name}: {last_error}")
        elif obj.kind == "vnet":
            result = self.ssh_result(self.pve, f"pvesh delete /cluster/sdn/vnets/{shlex.quote(obj.name)}", check=False)
            if result.returncode != 0:
                error = (result.stderr or result.stdout or "").strip()
                self.record(f"delete_vnet_warning_{obj.name}", {"vnet": obj.name, "last_error": error})
                if not sdn_delete_error_is_missing(error):
                    raise E2EError(f"failed to delete VNet {obj.name}: {error}")
        elif obj.kind == "zone":
            result = self.ssh_result(self.pve, f"pvesh delete /cluster/sdn/zones/{shlex.quote(obj.name)}", check=False)
            if result.returncode != 0:
                error = (result.stderr or result.stdout or "").strip()
                self.record(f"delete_zone_warning_{obj.name}", {"zone": obj.name, "last_error": error})
                if not sdn_delete_error_is_missing(error):
                    raise E2EError(f"failed to delete SDN zone {obj.name}: {error}")

    def create_lab_record(self, spec: LabSpec) -> str:
        self.log(f"creating lab bubble record for {spec.name}")
        payload = self.controller_python(
            f"""
import json
from web import db_pg, lab_bubbles_pg, sdn_labs_pg
with db_pg.connect() as conn:
    lab_bubbles_pg.init(conn)
    sdn_labs_pg.init(conn)
    existing = conn.execute("select id from lab_bubbles where name = %s", ({spec.name!r},)).fetchone()
    if existing:
        lab_bubbles_pg.delete_bubble(conn, str(existing['id']))
    bubble = lab_bubbles_pg.create_bubble(
        conn,
        name={spec.name!r},
        domain_name={spec.domain!r},
        netbios_name={spec.netbios!r},
        cidr={spec.cidr!r},
        gateway_ip={spec.gateway!r},
        planned_bridge={spec.vnet!r},
        isolation_status='planned',
        dhcp_scope={spec.cidr!r},
        dhcp_pool_start={spec.dhcp_start!r},
        dhcp_pool_end={spec.dhcp_end!r},
    )
    binding = sdn_labs_pg.upsert_binding(
        conn,
        bubble_id=bubble['id'],
        zone={spec.zone!r},
        vnet={spec.vnet!r},
        subnet={spec.cidr!r},
        egress_policy='open',
        snat_enabled=True,
        firewall_profile='isolated_open_egress',
        actor='sdn-lab-stress-e2e',
    )
print(json.dumps({{'bubble': bubble, 'binding': binding}}, default=str))
"""
        )
        bubble_id = str(payload["bubble"]["id"])
        self.created_bubbles.append(bubble_id)
        return bubble_id

    def create_dc_run(self, spec: LabSpec, bubble_id: str) -> dict[str, Any]:
        forest_password = _random_password(f"{spec.netbios}Admin")
        dsrm_password = _random_password(f"{spec.netbios}Dsrm")
        os_fields = osdeploy_request_fields_from_artifact(self.osdeploy_artifact)
        stamp = _utc_stamp()
        forest_cred_name = e2e_credential_name(spec.netbios, "forest-admin", stamp)
        dsrm_cred_name = e2e_credential_name(spec.netbios, "dsrm", stamp)
        self.secrets.extend([forest_password, dsrm_password])
        self.log(f"creating OSDeploy DC run for {spec.dc_name}")
        payload = self.controller_python(
            f"""
import json
from web import app as web_app, db_pg, sequences_pg
from web import osdeploy_endpoints, osdeploy_pg
cipher = web_app._cipher()
with db_pg.connect() as conn:
    sequences_pg.init(conn)
    forest_cred = sequences_pg.create_credential(
        conn,
        cipher,
        name={forest_cred_name!r},
        type='domain_join',
        payload={{'username': 'Administrator', 'password': {forest_password!r}}},
    )
    dsrm_cred = sequences_pg.create_credential(
        conn,
        cipher,
        name={dsrm_cred_name!r},
        type='local_admin',
        payload={{'username': 'Administrator', 'password': {dsrm_password!r}}},
    )
    conn.commit()
body = osdeploy_endpoints.RunCreateBody(
    artifact_id={self.args.osdeploy_artifact!r},
    vm_name={spec.dc_name!r},
    node={self.node!r},
    iso_storage='isos',
    storage='nvmepool',
    network_bridge={spec.vnet!r},
    vmid={planned_vmid_for_name(spec.dc_name)!r},
    architecture='amd64',
    server_role='isolated_domain_controller',
    os_version={os_fields['os_version']!r},
    os_edition={os_fields['os_edition']!r},
    os_language={os_fields['os_language']!r},
    vm_cores=4,
    vm_memory_mb=12288,
    vm_disk_size_gb=120,
    secure_boot=False,
    outbound_policy={{'mode': 'open'}},
    role_options={{
        'forest_fqdn': {spec.domain!r},
        'netbios_name': {spec.netbios!r},
        'forest_admin_credential_id': forest_cred,
        'dsrm_credential_id': dsrm_cred,
    }},
    bubble_id={bubble_id!r},
    asset_role='domain_controller',
)
created = osdeploy_endpoints.create_run(body)
with db_pg.connect() as conn:
    artifact = osdeploy_pg.get_artifact(conn, created['run']['artifact_id'])
extra_vars = osdeploy_endpoints.osdeploy_provision_extra_vars(run=created['run'], artifact=artifact, request=None)
cmd = [
    'ansible-playbook',
    str(osdeploy_endpoints._APP_ROOT / 'playbooks' / 'provision_proxmox_osdeploy.yml'),
]
for key, value in extra_vars.items():
    cmd.extend(['-e', f"{{key}}={{value}}"])
job = web_app.job_manager.start('provision_osdeploy', cmd, args=extra_vars)
provision_job = {{'ok': True, 'job_id': job['id']}}
created['run']['provision_job_id'] = job['id']
print(json.dumps({{'run': created['run'], 'provision_job': provision_job, 'forest_password': {forest_password!r}}}, default=str))
""",
            timeout=120,
        )
        run = payload["run"]
        self.created_osdeploy_runs.append(run["run_id"])
        self.remember_vmid(run.get("vmid"))
        self.lab_contexts[spec.domain] = {
            "bubble_id": bubble_id,
            "forest_password": forest_password,
            "dc_run_id": run["run_id"],
            "dc_vmid": int(run.get("vmid") or 0),
            "dc_provision_job_id": str(run.get("provision_job_id") or ""),
        }
        return run

    def create_cloudosd_sequence(self, spec: LabSpec, *, bad_password: bool = False) -> CloudOsdJoinSequence:
        password = self.lab_contexts[spec.domain]["forest_password"]
        if bad_password:
            password = password + "-wrong"
        self.secrets.append(password)
        suffix = "bad" if bad_password else "join"
        self.log(f"creating CloudOSD {suffix} sequence for {spec.domain}")
        payload = self.controller_python(
            f"""
import json
from web import app as web_app, db_pg, sequences_pg
cipher = web_app._cipher()
with db_pg.connect() as conn:
    sequences_pg.init(conn)
    cred_id = sequences_pg.create_credential(
        conn,
        cipher,
        name={('e2e-' + spec.netbios.lower() + '-' + suffix + '-' + _utc_stamp())!r},
        type='domain_join',
        payload={{
            'domain_fqdn': {spec.domain!r},
            'username': {domain_join_admin_username(spec)!r},
            'password': {password!r},
            'ou_hint': '',
        }},
    )
    sequence_id = sequences_pg.create_sequence(
        conn,
        name={('E2E ' + spec.netbios + ' CloudOSD ' + suffix + ' ' + _utc_stamp())!r},
        description='E2E CloudOSD full-OS domain join sequence',
        produces_autopilot_hash=True,
        steps=[{{
            'step_type': 'join_ad_domain',
            'params': {{
                'credential_id': cred_id,
                'ou_path': '',
                'domain_controller_ipv4': {spec.dc_ip!r},
            }},
            'enabled': True,
        }}],
    )
    conn.commit()
print(json.dumps({{'sequence_id': sequence_id, 'credential_id': cred_id}}))
"""
        )
        return CloudOsdJoinSequence(
            sequence_id=int(payload["sequence_id"]),
            credential_id=int(payload["credential_id"]),
        )

    def create_cloudosd_run(
        self,
        spec: LabSpec,
        name: str,
        sequence: CloudOsdJoinSequence,
        *,
        asset_role: str = "workstation",
    ) -> dict[str, Any]:
        self.log(f"creating CloudOSD run {name}")
        bubble_id = self.lab_contexts[spec.domain]["bubble_id"]
        domain_join = build_cloudosd_domain_join_config(spec, sequence)
        payload = self.controller_python(
            f"""
import json
from web import app as web_app, cloudosd_endpoints, cloudosd_pg, db_pg, lab_bubbles_pg
domain_join = {domain_join!r}
with db_pg.connect() as conn:
    run = cloudosd_pg.create_run(
        conn,
        artifact_id={self.args.cloudosd_artifact!r},
        vm_name={name!r},
        node={self.node!r},
        iso_storage='isos',
        storage='nvmepool',
        network_bridge={spec.vnet!r},
        requested_vmid={planned_vmid_for_name(name)!r},
        architecture='amd64',
        os_version={self.cloudosd_os_version!r},
        os_activation={DEFAULT_CLOUDOSD_OS_ACTIVATION!r},
        os_edition={DEFAULT_CLOUDOSD_OS_EDITION!r},
        os_language={DEFAULT_CLOUDOSD_OS_LANGUAGE!r},
        vm_cores=4,
        vm_memory_mb=8192,
        vm_disk_size_gb=80,
        vm_group_tag={spec.netbios!r},
        source_surface='sdn_lab_stress_e2e',
        source_sequence_id={sequence.sequence_id},
        tpm_enabled=True,
        secure_boot=True,
        outbound_policy={{'mode': 'open'}},
        domain_join=domain_join,
    )
    plan_steps = [dict(row) for row in conn.execute(
        "select ordinal, kind, phase, state from ts_run_plan_steps where run_id = %s order by ordinal",
        (run['run_id'],),
    ).fetchall()]
    lab_bubbles_pg.add_asset(
        conn,
        {bubble_id!r},
        asset_type='vm',
        asset_role={asset_role!r},
        vmid=run.get('vmid') or run.get('requested_vmid'),
        run_id=run['run_id'],
        membership_state='provisioning',
        actor='cloudosd',
    )
    artifact = cloudosd_pg.get_artifact(conn, run['artifact_id'])
extra_vars = cloudosd_endpoints.cloudosd_provision_extra_vars(run=run, artifact=artifact, request=None)
cmd = [
    'ansible-playbook',
    str(cloudosd_endpoints._APP_ROOT / 'playbooks' / 'provision_proxmox_cloudosd.yml'),
]
for key, value in extra_vars.items():
    cmd.extend(['-e', f"{{key}}={{value}}"])
job = web_app.job_manager.start('provision_cloudosd', cmd, args=extra_vars)
provision_job = {{'ok': True, 'job_id': job['id']}}
run['provision_job_id'] = job['id']
print(json.dumps({{'run': run, 'plan_steps': plan_steps, 'provision_job': provision_job}}, default=str))
""",
            timeout=120,
        )
        run = payload["run"]
        plan_steps = payload.get("plan_steps") or []
        plan_failures = validate_cloudosd_domain_join_plan(
            run,
            [step.get("kind") for step in plan_steps if isinstance(step, dict)],
        )
        self.record(f"{name}_cloudosd_domain_join_plan", {"run": run, "plan_steps": plan_steps, "failures": plan_failures})
        if plan_failures:
            raise E2EError(f"{name} CloudOSD domain-join plan invalid: {plan_failures}")
        self.created_cloudosd_runs.append(run["run_id"])
        self.remember_vmid(run.get("vmid"))
        return run

    def fetch_run_status(self, run_ids: list[str], *, job_ids: list[str] | None = None) -> dict[str, Any]:
        return self.controller_python(
            f"""
import json
from web import db_pg
run_ids = {run_ids!r}
job_ids = {list(dict.fromkeys(job_ids or []))!r}
out = {{}}
with db_pg.connect() as conn:
    out['osdeploy'] = [dict(row) for row in conn.execute(
        "select o.run_id, o.vm_name, o.vmid, o.state, t.state as ts_state, t.phase, t.cursor_step_id, s.kind as cursor_kind, s.state as cursor_state, t.finished_at from osdeploy_runs o join ts_provisioning_runs t on t.id=o.run_id left join ts_run_plan_steps s on s.id=t.cursor_step_id where o.run_id = any(%s)",
        (run_ids,),
    ).fetchall()]
    out['cloudosd'] = [dict(row) for row in conn.execute(
        "select c.run_id, c.expected_computer_name, c.vmid, c.state, c.osdcloud_finished_at, c.first_heartbeat_at, t.state as ts_state, t.phase, t.cursor_step_id, s.kind as cursor_kind, s.state as cursor_state, t.finished_at from cloudosd_runs c join ts_provisioning_runs t on t.id=c.run_id left join ts_run_plan_steps s on s.id=t.cursor_step_id where c.run_id = any(%s)",
        (run_ids,),
    ).fetchall()]
    out['events'] = [dict(row) for row in conn.execute(
        "select run_id, event_type, severity, message, created_at from ts_run_step_events where run_id = any(%s) order by created_at desc limit 80",
        (run_ids,),
    ).fetchall()]
    out['jobs'] = [dict(row) for row in conn.execute(
        "select id, job_type, status, exit_code, ended_at, args_json->>'vm_name' as vm_name from jobs where id = any(%s) order by created_at",
        (job_ids,),
    ).fetchall()] if job_ids else []
print(json.dumps(out, default=str))
""",
            timeout=120,
        )

    def trigger_awaiting_reboots(self, status: dict[str, Any]) -> None:
        for row in list(status.get("osdeploy") or []) + list(status.get("cloudosd") or []):
            state = str(row.get("ts_state") or row.get("state") or "")
            cursor_state = str(row.get("cursor_state") or "")
            if state != "awaiting_reboot" and cursor_state != "awaiting_reboot":
                continue
            run_id = str(row.get("run_id") or "")
            step_id = str(row.get("cursor_step_id") or "")
            vmid = int(row.get("vmid") or 0)
            if not run_id or not step_id or vmid <= 0:
                continue
            key = f"{run_id}:{step_id}"
            if key in self.triggered_reboots:
                continue
            self.triggered_reboots.add(key)
            self.log(f"triggering required reboot for VMID {vmid} after {row.get('cursor_kind') or 'step'}")
            guest_payload: dict[str, Any]
            fallback_payload: dict[str, Any] | None = None
            try:
                guest = self.ssh_result(
                    self.pve,
                    f"qm guest exec {vmid} -- shutdown.exe /r /t 5 /f",
                    timeout=30,
                    check=False,
                )
                guest_payload = {
                    "exit_code": guest.returncode,
                    "stdout": guest.stdout,
                    "stderr": guest.stderr,
                    "timed_out": False,
                }
            except subprocess.TimeoutExpired as exc:
                guest_payload = command_timeout_payload(exc)
            if guest_payload.get("exit_code") not in (0, None) or guest_payload.get("timed_out"):
                try:
                    fallback = self.ssh_result(self.pve, f"qm reset {vmid}", timeout=30, check=False)
                    fallback_payload = {
                        "exit_code": fallback.returncode,
                        "stdout": fallback.stdout,
                        "stderr": fallback.stderr,
                        "timed_out": False,
                    }
                except subprocess.TimeoutExpired as exc:
                    fallback_payload = command_timeout_payload(exc)
            self.record(
                f"required_reboot_{vmid}_{safe_record_name(step_id)}",
                {
                    "run_id": run_id,
                    "step_id": step_id,
                    "step_kind": row.get("cursor_kind"),
                    "vmid": vmid,
                    "guest_exec": guest_payload,
                    "qm_reset": fallback_payload,
                },
            )

    def request_job_kill(self, job_id: str, *, reason: str) -> dict[str, Any]:
        payload = self.controller_python(
            f"""
import json
from web import jobs_pg
jobs_pg.request_kill({job_id!r})
print(json.dumps({{'job_id': {job_id!r}, 'kill_requested': True}}))
""",
            timeout=60,
        )
        self.record(
            f"job_kill_requested_{safe_record_name(job_id)}",
            {"job_id": job_id, "reason": reason, "result": payload},
        )
        return payload or {"job_id": job_id, "kill_requested": True}

    def wait_for_job_terminal(self, job_id: str, *, timeout_seconds: int = 300) -> dict[str, Any]:
        def predicate() -> tuple[bool, Any]:
            row = self.controller_python(
                f"""
import json
from web import db_pg
with db_pg.connect() as conn:
    row = conn.execute(
        "select id, status, exit_code, ended_at from jobs where id = %s",
        ({job_id!r},),
    ).fetchone()
print(json.dumps(dict(row) if row else {{}}, default=str))
""",
                timeout=60,
            )
            status = str((row or {}).get("status") or "")
            return bool(row) and status not in {"pending", "running"}, row

        return wait_until(predicate, timeout_seconds=timeout_seconds, interval_seconds=5)

    def maybe_restart_stopped_negative_disk_boot(self, run: dict[str, Any] | None, rows: list[dict[str, Any]]) -> None:
        if not run or not rows:
            return
        run_id = str(run.get("run_id") or "")
        key = run_id or str(run.get("provision_job_id") or "")
        if not hasattr(self, "negative_disk_boot_restarts"):
            self.negative_disk_boot_restarts = set()
        if not key or key in self.negative_disk_boot_restarts:
            return
        row = rows[0]
        if not negative_disk_boot_restart_candidate(row):
            return
        vmid = int(row.get("vmid") or 0)
        status = self.pve_cmd(f"qm status {vmid}", timeout=30, check=False).strip()
        if "status: stopped" not in status.lower():
            return
        self.negative_disk_boot_restarts.add(key)
        self.record(
            "negative_disk_boot_restart_observed",
            {
                "run_id": run_id,
                "job_id": run.get("provision_job_id"),
                "vmid": vmid,
                "status": status,
                "row": row,
            },
        )
        attempts: list[dict[str, Any]] = []
        payload: dict[str, Any] = {
            "run_id": run_id,
            "job_id": run.get("provision_job_id"),
            "vmid": vmid,
            "attempts": attempts,
        }
        for attempt in range(1, 4):
            result = self.ssh_result(self.pve, f"qm start {vmid}", timeout=60, check=False)
            post_status = self.pve_cmd(f"qm status {vmid}", timeout=30, check=False).strip()
            attempt_payload = {
                "attempt": attempt,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "post_status": post_status,
            }
            attempts.append(attempt_payload)
            payload.update(
                {
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "post_status": post_status,
                }
            )
            if result.returncode == 0 or "status: running" in post_status.lower():
                self.record("negative_disk_boot_restart_result", payload)
                return
        self.record("negative_disk_boot_restart_result", payload)
        self.record(
            "negative_disk_boot_restart_warning",
            {
                **payload,
                "message": "negative-path VM restart failed; preserving evidence and continuing",
            },
        )

    def maybe_release_negative_job_slot(self, run: dict[str, Any] | None, status: dict[str, Any]) -> None:
        if not run:
            return
        run_id = str(run.get("run_id") or "")
        job_id = str(run.get("provision_job_id") or "")
        key = job_id or run_id
        if not key or key in self.released_negative_jobs:
            return

        rows = [row for row in status.get("cloudosd", []) if str(row.get("run_id") or "") == run_id]
        self.maybe_restart_stopped_negative_disk_boot(run, rows)
        state = str(rows[0].get("ts_state") or rows[0].get("state") or "") if rows else ""
        events = [event for event in status.get("events", []) if str(event.get("run_id") or "") == run_id]
        has_join_signal = negative_join_signal(rows, events)
        if not (state in {"failed", "full_os_waiting_domain_join", "done"} and has_join_signal):
            return

        rendered_status = json.dumps(redact_secrets({"rows": rows, "events": events}), default=str)
        leaked = [secret for secret in self.secrets if secret and secret in rendered_status]
        self.record(
            "negative_slot_release_observed",
            {"run_id": run_id, "job_id": job_id, "state": state, "leaked": bool(leaked), "events": events},
        )
        if leaked:
            raise E2EError("negative path leaked a secret in run evidence")
        if state == "done":
            raise E2EError("negative-path workstation unexpectedly completed")
        self.released_negative_jobs.add(key)
        if state == "failed":
            return
        if not job_id:
            raise E2EError("negative-path job id was not captured; cannot release CloudOSD queue slot")
        self.request_job_kill(job_id, reason="negative domain-join failure captured")
        terminal = self.wait_for_job_terminal(job_id)
        self.record("negative_slot_release_result", {"run_id": run_id, "job_id": job_id, "job": terminal})

    def recover_stale_running_join_steps(self, run_ids: list[str]) -> None:
        if not run_ids:
            return
        payload = self.controller_python(
            f"""
import json
from web import db_pg
run_ids = {run_ids!r}
with db_pg.connect() as conn:
    candidates = [dict(row) for row in conn.execute(
        '''
        WITH latest_heartbeat AS (
            SELECT DISTINCT ON (current_run_id)
                   current_run_id, vmid, current_step_id, domain_joined, received_at
            FROM agent_heartbeats
            WHERE current_run_id::text = ANY(%s)
            ORDER BY current_run_id, received_at DESC
        )
        SELECT c.expected_computer_name, c.vmid, c.run_id::text AS run_id,
               s.id::text AS step_id, s.kind, s.state AS step_state,
               s.claimed_at, h.current_step_id, h.received_at
        FROM cloudosd_runs c
        JOIN ts_provisioning_runs t ON t.id = c.run_id
        JOIN ts_run_plan_steps s ON s.id = t.cursor_step_id
        JOIN latest_heartbeat h ON h.current_run_id = c.run_id
        WHERE c.run_id::text = ANY(%s)
          AND c.archived_at IS NULL
          AND s.kind IN ('capture_autopilot_hash', 'join_domain_role')
          AND s.state = 'running'
          AND s.claimed_at < now() - interval '5 minutes'
          AND h.received_at > s.claimed_at
          AND COALESCE(h.current_step_id, '') = ''
          AND (s.kind != 'join_domain_role' OR COALESCE(h.domain_joined, false) = false)
        ORDER BY c.expected_computer_name
        ''',
        (run_ids, run_ids),
    ).fetchall()]
    for row in candidates:
        conn.execute(
            "UPDATE ts_run_plan_steps SET state='pending', claimed_by=NULL, claimed_at=NULL, last_error=NULL WHERE id=%s",
            (row['step_id'],),
        )
        conn.execute(
            "UPDATE ts_provisioning_runs SET state='running_full_os', phase='full_os', last_error=NULL, finished_at=NULL WHERE id=%s",
            (row['run_id'],),
        )
        conn.execute(
            '''
            INSERT INTO ts_run_step_events (
                run_id, step_id, event_type, severity, agent_id, phase,
                message, data_json, created_at
            )
            VALUES (%s, %s, 'operator_requeued_stale_running_step', 'warning',
                    'sdn-lab-stress-e2e', 'full_os',
                    'Agent heartbeat had no current_step_id while a full-OS step was running',
                    %s::jsonb, now())
            ''',
            (row['run_id'], row['step_id'], json.dumps({{'vmid': row.get('vmid'), 'kind': row.get('kind')}})),
        )
    conn.commit()
print(json.dumps({{'requeued': candidates}}, default=str))
""",
            timeout=120,
        )
        if payload and payload.get("requeued"):
            self.record(f"stale_join_recovery_{_utc_stamp()}", payload)

    def wait_for_runs_complete(
        self,
        run_ids: list[str],
        *,
        timeout_seconds: int,
        label: str,
        negative_run: dict[str, Any] | None = None,
        required_jobs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.log(f"waiting for {label} completion")
        required_job_by_run = dict(required_jobs or {})

        def predicate() -> tuple[bool, Any]:
            poll_run_ids = list(dict.fromkeys(run_ids + ([negative_run["run_id"]] if negative_run else [])))
            poll_job_ids = list(required_job_by_run.values())
            if negative_run and negative_run.get("provision_job_id"):
                poll_job_ids.append(str(negative_run["provision_job_id"]))
            status = self.fetch_run_status(poll_run_ids, job_ids=list(dict.fromkeys(poll_job_ids)))
            self.sync_run_status(status)
            self.trigger_awaiting_reboots(status)
            self.maybe_release_negative_job_slot(negative_run, status)
            self.recover_stale_running_join_steps(run_ids)
            rows = status.get("osdeploy", []) + status.get("cloudosd", [])
            state_by_id = {str(row["run_id"]): str(row.get("ts_state") or row.get("state")) for row in rows}
            required = set(run_ids)
            failed = {run_id: state for run_id, state in state_by_id.items() if run_id in required and state == "failed"}
            if failed:
                raise E2EError(f"{label} failed: {failed}; latest={status.get('events')[:10]}")
            failed_jobs = failed_required_job_rows(status, required_job_by_run)
            if failed_jobs:
                raise E2EError(f"{label} provisioning job failed: {failed_jobs}; latest={status.get('events')[:10]}")
            missing = [run_id for run_id in run_ids if state_by_id.get(run_id) not in {"done", "complete"}]
            self.record(f"poll_{label}_{_utc_stamp()}", status)
            return not missing, status

        return wait_until(predicate, timeout_seconds=timeout_seconds, interval_seconds=60)

    def wait_for_qga(self, vmid: int, *, timeout_seconds: int = 1800) -> None:
        if int(vmid or 0) <= 0:
            raise E2EError("cannot wait for QGA without a resolved VMID")
        self.log(f"waiting for QGA on VMID {vmid}")

        def predicate() -> tuple[bool, Any]:
            result = self.ssh_result(self.pve, f"qm guest cmd {int(vmid)} ping", timeout=30, check=False)
            observation = {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            return qga_ping_succeeded(result), observation

        wait_until(predicate, timeout_seconds=timeout_seconds, interval_seconds=20)

    def failure_guest_diagnostic_script(self) -> str:
        return r"""
$ProgressPreference='SilentlyContinue'
$ErrorActionPreference='Continue'
function Read-Tail {
  param([string]$Path, [int]$Lines = 120)
  if (-not (Test-Path -LiteralPath $Path)) { return @() }
  try {
    return @(Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction Stop)
  } catch {
    return @("failed to read ${Path}: $($_.Exception.Message)")
  }
}
$root = 'C:\ProgramData\ProxmoxVEAutopilot'
$result = [ordered]@{}
$result.computer = $env:COMPUTERNAME
$result.now = (Get-Date).ToString('o')
$result.services = @()
foreach ($name in @('AutopilotAgent','QEMU-GA')) {
  try {
    $svc = Get-Service -Name $name -ErrorAction Stop
    $cim = Get-CimInstance Win32_Service -Filter "Name='$name'" -ErrorAction SilentlyContinue
    $result.services += [ordered]@{
      name = $name
      status = [string]$svc.Status
      start_type = [string]$svc.StartType
      state = if ($cim) { [string]$cim.State } else { '' }
      start_mode = if ($cim) { [string]$cim.StartMode } else { '' }
      exit_code = if ($cim) { [int]$cim.ExitCode } else { $null }
      path_name = if ($cim) { [string]$cim.PathName } else { '' }
    }
  } catch {
    $result.services += [ordered]@{ name = $name; error = $_.Exception.Message }
  }
}
$result.files = @()
if (Test-Path -LiteralPath $root) {
  $result.files = @(Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 100 FullName,Length,LastWriteTime)
}
$result.logs = [ordered]@{
  firstboot = Read-Tail 'C:\ProgramData\ProxmoxVEAutopilot\CloudOSD\firstboot.log' 160
  postinstall = Read-Tail 'C:\ProgramData\ProxmoxVEAutopilot\AutopilotAgent\install\postinstall.log' 160
  agent_msi = Read-Tail 'C:\ProgramData\ProxmoxVEAutopilot\CloudOSD\AutopilotAgent-msi.log' 120
  qga = Read-Tail 'C:\ProgramData\qemu-ga\qemu-ga.log' 120
  setupcomplete = Read-Tail 'C:\Windows\Setup\Scripts\SetupComplete.log' 120
}
try {
  $result.events = @(Get-WinEvent -LogName Application -MaxEvents 80 -ErrorAction Stop |
    Where-Object { $_.ProviderName -match 'MsiInstaller|Application Error|\.NET Runtime|Service Control Manager' } |
    Select-Object -First 40 TimeCreated,ProviderName,Id,LevelDisplayName,Message)
} catch {
  $result.events_error = $_.Exception.Message
}
$result | ConvertTo-Json -Depth 10 -Compress
"""

    def collect_failure_evidence(self, reason: str) -> None:
        run_ids = list(dict.fromkeys(self.created_osdeploy_runs + self.created_cloudosd_runs))
        if not run_ids:
            return
        status = self.fetch_run_status(run_ids)
        self.sync_run_status(status)
        self.record("failure_run_status", {"reason": reason, **status})
        rows = list(status.get("osdeploy") or []) + list(status.get("cloudosd") or [])
        for row in rows:
            state = str(row.get("ts_state") or row.get("state") or "")
            if state not in {"failed", "error"} and str(row.get("state") or "") != "failed":
                continue
            vmid = int(row.get("vmid") or 0)
            if vmid <= 0:
                continue
            name = str(row.get("expected_computer_name") or row.get("vm_name") or row.get("run_id") or vmid)
            try:
                payload = self.qm_guest_exec_json(vmid, self.failure_guest_diagnostic_script(), timeout=240)
                self.record(f"failure_guest_{name}_{vmid}", payload)
            except Exception as exc:
                self.record(f"failure_guest_{name}_{vmid}_warning", {"error": str(exc), "row": row})

    def dc_setup_script(self, spec: LabSpec, user_password: str) -> str:
        return f"""
$ErrorActionPreference = 'Stop'
$domain = {_ps_string(spec.domain)}
$netbios = {_ps_string(spec.netbios)}
$user = {_ps_string(spec.proof_user)}
$password = {_ps_string(user_password)}
$sharePath = 'C:\\E2EAuthProof'
Import-Module ActiveDirectory
$secure = ConvertTo-SecureString $password -AsPlainText -Force
$existing = Get-ADUser -Filter "SamAccountName -eq '$user'" -ErrorAction SilentlyContinue
if (-not $existing) {{
  New-ADUser -Name 'E2E Proof User' -SamAccountName $user -UserPrincipalName "$user@$domain" -AccountPassword $secure -Enabled $true -PasswordNeverExpires $true
}} else {{
  Set-ADAccountPassword -Identity $existing -Reset -NewPassword $secure
  Enable-ADAccount -Identity $existing
}}
New-Item -ItemType Directory -Force -Path $sharePath | Out-Null
$acl = Get-Acl $sharePath
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule("$netbios\\Domain Users", 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.SetAccessRule($rule)
Set-Acl -Path $sharePath -AclObject $acl
$share = Get-SmbShare -Name E2EAuthProof -ErrorAction SilentlyContinue
if (-not $share) {{
  New-SmbShare -Name E2EAuthProof -Path $sharePath -ChangeAccess "$netbios\\Domain Users" -FullAccess "$netbios\\Domain Admins" | Out-Null
}}
$domainObj = Get-ADDomain -Identity $domain
$srv = Resolve-DnsName -Type SRV "_ldap._tcp.dc._msdcs.$domain" -ErrorAction Stop
[ordered]@{{
  ok = $true
  domain = $domainObj.DNSRoot
  sysvol = (Test-Path "$env:SystemRoot\\SYSVOL\\sysvol")
  netlogon = [bool](Get-SmbShare -Name NETLOGON -ErrorAction SilentlyContinue)
  proof_share = [bool](Get-SmbShare -Name E2EAuthProof -ErrorAction SilentlyContinue)
  srv_records = @($srv | Where-Object {{$_.Type -eq 'SRV'}} | Select-Object NameTarget,Port)
}} | ConvertTo-Json -Depth 6 -Compress
"""

    def workstation_proof_script(self, spec: LabSpec, user_password: str) -> str:
        bad_password = user_password + "-bad"
        return f"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Continue'
$domain = {_ps_string(spec.domain)}
$netbios = {_ps_string(spec.netbios)}
$dcIp = {_ps_string(spec.dc_ip)}
$dcFqdn = {_ps_string(spec.dc_fqdn)}
$proofUser = {_ps_string(spec.proof_user_sam)}
$proofPassword = {_ps_string(user_password)}
$badPassword = {_ps_string(bad_password)}
$result = [ordered]@{{}}
$result.computer = $env:COMPUTERNAME
$cs = Get-CimInstance Win32_ComputerSystem
$result.domain = [string]$cs.Domain
$result.part_of_domain = [bool]$cs.PartOfDomain
$result.ipv4 = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {{ $_.IPAddress -like '{spec.cidr.rsplit('.', 1)[0]}.*' }} | Select-Object -ExpandProperty IPAddress)
$result.dns_servers = @(Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object {{$_.ServerAddresses}} | ForEach-Object {{$_.ServerAddresses}} | Select-Object -Unique)
$result.dc_port_53 = (Test-NetConnection -ComputerName $dcIp -Port 53 -InformationLevel Quiet)
$result.dc_port_88 = (Test-NetConnection -ComputerName $dcIp -Port 88 -InformationLevel Quiet)
$result.dc_port_389 = (Test-NetConnection -ComputerName $dcIp -Port 389 -InformationLevel Quiet)
$result.dc_port_445 = (Test-NetConnection -ComputerName $dcIp -Port 445 -InformationLevel Quiet)
$ds = & nltest.exe /dsgetdc:$domain 2>&1
$dsText = ($ds -join "`n")
$result.dsgetdc_found = ($LASTEXITCODE -eq 0 -and $dsText -match 'DC:')
$result.dsgetdc_dc = if ($dsText -match 'DC:\\s+\\\\\\\\([^\\r\\n]+)') {{ $Matches[1] }} else {{ '' }}
$result.dsgetdc_address = if ($dsText -match 'Address:\\s+\\\\\\\\([^\\r\\n]+)') {{ $Matches[1] }} else {{ '' }}
try {{
  $srv = Resolve-DnsName -Type SRV "_ldap._tcp.dc._msdcs.$domain" -ErrorAction Stop | Where-Object {{$_.Type -eq 'SRV'}} | Select-Object -First 5 Name,NameTarget,Port,Type
  $result.srv_lookup_ok = $true
  $result.srv_records = @($srv)
}} catch {{
  $result.srv_lookup_ok = $false
  $result.srv_error = $_.Exception.Message
}}
$sc = & nltest.exe /sc_verify:$domain 2>&1
$scText = ($sc -join "`n")
$result.secure_channel_ok = ($LASTEXITCODE -eq 0 -and $scText -match 'NERR_Success|The command completed successfully')
$result.trusted_dc = if ($scText -match 'Trusted DC Name\\s+\\\\\\\\([^\\r\\n ]+)') {{ $Matches[1] }} else {{ '' }}
$klist = & klist.exe get "krbtgt/$domain" 2>&1
$kText = ($klist -join "`n")
$result.kerberos_ok = ($LASTEXITCODE -eq 0 -or $kText -match 'ticket to krbtgt/.+retrieved successfully')
$result.kerberos_client = if ($kText -match 'Client:\\s+([^\\r\\n]+)') {{ $Matches[1].Trim() }} else {{ '' }}
$result.kdc_called = if ($kText -match 'Kdc Called:\\s+([^\\r\\n]+)') {{ $Matches[1].Trim() }} else {{ '' }}
$auth = [ordered]@{{ ok = $false; share_write_read_ok = $false; bad_password_rejected = $false; whoami = '' }}
try {{
  $work = 'C:\\ProgramData\\ProxmoxVEAutopilot\\E2EProof'
  New-Item -ItemType Directory -Force -Path $work | Out-Null
  # Grant SeBatchLogonRight to the proof user before Register-ScheduledTask.
  # Domain-joined workstations do not give ordinary domain users the right
  # to be logged on as a batch job, which makes Task Scheduler refuse to
  # start the task with ERROR_LOGON_NOT_GRANTED (0x80070569 / 2147943785,
  # Task Scheduler Operational event 101). Use secedit /areas USER_RIGHTS
  # so we only touch user-rights policy, not the rest of the local SECPOL.
  try {{
    $proofSid = (New-Object System.Security.Principal.NTAccount($proofUser)).Translate([System.Security.Principal.SecurityIdentifier]).Value
    $auth.proof_user_sid = $proofSid
    $secInf = Join-Path $work 'batch-logon.inf'
    $secDb  = Join-Path $work 'batch-logon.sdb'
    & secedit.exe /export /cfg $secInf /areas USER_RIGHTS /quiet | Out-Null
    $inf = Get-Content -LiteralPath $secInf -Raw
    if ($inf -match '(?m)^SeBatchLogonRight\\s*=\\s*([^\\r\\n]*)') {{
      $existing = $Matches[1].Trim()
      if ($existing -notmatch [regex]::Escape($proofSid)) {{
        $newLine = "SeBatchLogonRight = " + $existing.TrimEnd(',') + ",*" + $proofSid
        $inf = [regex]::Replace($inf, '(?m)^SeBatchLogonRight\\s*=\\s*[^\\r\\n]*', $newLine)
      }}
    }} else {{
      $inf = $inf -replace '(?m)^\\[Privilege Rights\\]\\s*$', "[Privilege Rights]`r`nSeBatchLogonRight = *$proofSid"
    }}
    # secedit /configure needs UTF-16 LE w/ BOM (Unicode).
    [System.IO.File]::WriteAllText($secInf, $inf, [System.Text.UnicodeEncoding]::new($false, $true))
    & secedit.exe /configure /db $secDb /cfg $secInf /areas USER_RIGHTS /quiet | Out-Null
    $auth.batch_logon_grant_ok = ($LASTEXITCODE -eq 0)
    Remove-Item -LiteralPath $secInf, $secDb -Force -ErrorAction SilentlyContinue
  }} catch {{
    $auth.batch_logon_grant_ok = $false
    $auth.batch_logon_grant_error = $_.Exception.Message
  }}
  $scriptPath = Join-Path $work 'user-auth-proof.ps1'
  $outPath = Join-Path $work 'user-auth-proof.json'
  $share = "\\\\$dcFqdn\\E2EAuthProof"
  $userScript = @"
`$ErrorActionPreference='Stop'
`$out=[ordered]@{{}}
`$out.whoami = (& whoami.exe)
`$out.ldap_bind_ok = `$false
`$out.cifs_kerberos_ok = `$false
try {{
  `$root = New-Object DirectoryServices.DirectoryEntry('LDAP://$dcFqdn/RootDSE')
  `$out.ldap_dns_host_name = [string]`$root.dnsHostName
  `$out.ldap_default_naming_context = [string]`$root.defaultNamingContext
  `$out.ldap_bind_ok = [bool]`$out.ldap_default_naming_context
}} catch {{
  `$out.ldap_error = `$_.Exception.Message
}}
`$target = '$share\\' + `$env:COMPUTERNAME + '.txt'
'domain-user-proof:' + `$env:COMPUTERNAME | Set-Content -Path `$target -Encoding UTF8
`$read = Get-Content -Path `$target -ErrorAction Stop
`$out.share_write_read_ok = (`$read -like 'domain-user-proof:*')
`$klist = & klist.exe get 'cifs/$dcFqdn' 2>&1
`$klistText = (`$klist -join "`n")
`$out.cifs_kerberos_ok = (`$LASTEXITCODE -eq 0 -or `$klistText -match 'ticket to cifs/.+retrieved successfully')
`$out.cifs_kerberos_client = if (`$klistText -match 'Client:\\s+([^\\r\\n]+)') {{ `$Matches[1].Trim() }} else {{ '' }}
`$out.cifs_kdc_called = if (`$klistText -match 'Kdc Called:\\s+([^\\r\\n]+)') {{ `$Matches[1].Trim() }} else {{ '' }}
`$out | ConvertTo-Json -Compress | Set-Content -Path '$outPath' -Encoding UTF8
"@
  Set-Content -Path $scriptPath -Value $userScript -Encoding UTF8
  $taskName = 'PVEAutopilot-E2EProof'
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(15)
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -User $proofUser -Password $proofPassword -RunLevel Limited -Force | Out-Null
  $startAttempts = @()
  try {{
    Start-ScheduledTask -TaskName $taskName
    $startAttempts += [ordered]@{{ method = 'Start-ScheduledTask'; ok = $true }}
  }} catch {{
    $startAttempts += [ordered]@{{ method = 'Start-ScheduledTask'; ok = $false; error = $_.Exception.Message }}
  }}
  $schtasksOut = & schtasks.exe /Run /TN $taskName 2>&1
  $startAttempts += [ordered]@{{ method = 'schtasks.exe /Run'; ok = ($LASTEXITCODE -eq 0); exit_code = $LASTEXITCODE; output = ($schtasksOut -join "`n") }}
  $deadline = (Get-Date).AddMinutes(5)
  $pendingTaskResults = @(0x41300, 0x41301, 0x41302, 0x41303, 0x41306)
  $lastTaskResult = 0x41303
  $taskSnapshot = [ordered]@{{ start_attempts = $startAttempts }}
  do {{
    Start-Sleep -Seconds 5
    $task = Get-ScheduledTask -TaskName $taskName
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    $lastTaskResult = [int]$info.LastTaskResult
    $taskSnapshot.state = [string]$task.State
    $taskSnapshot.last_result = $lastTaskResult
    $taskSnapshot.last_run_time = [string]$info.LastRunTime
    $taskSnapshot.next_run_time = [string]$info.NextRunTime
    $stillPending = ($task.State -in @('Queued', 'Running')) -or ($pendingTaskResults -contains $lastTaskResult)
  }} while ((Get-Date) -lt $deadline -and -not (Test-Path $outPath) -and $stillPending)
  $auth.task = $taskSnapshot
  if (Test-Path $outPath) {{
    $taskPayload = Get-Content $outPath -Raw | ConvertFrom-Json
    $auth.ok = [bool]($taskPayload.share_write_read_ok -and $taskPayload.ldap_bind_ok -and $taskPayload.cifs_kerberos_ok)
    $auth.share_write_read_ok = [bool]$taskPayload.share_write_read_ok
    $auth.ldap_bind_ok = [bool]$taskPayload.ldap_bind_ok
    $auth.ldap_dns_host_name = [string]$taskPayload.ldap_dns_host_name
    $auth.ldap_default_naming_context = [string]$taskPayload.ldap_default_naming_context
    if ($taskPayload.ldap_error) {{ $auth.ldap_error = [string]$taskPayload.ldap_error }}
    $auth.cifs_kerberos_ok = [bool]$taskPayload.cifs_kerberos_ok
    $auth.cifs_kerberos_client = [string]$taskPayload.cifs_kerberos_client
    $auth.cifs_kdc_called = [string]$taskPayload.cifs_kdc_called
    $auth.whoami = [string]$taskPayload.whoami
  }} else {{
    $auth.error = "scheduled task result missing; last_result=$lastTaskResult"
    try {{
      $auth.task.events = @(Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' -MaxEvents 40 -ErrorAction Stop |
        Where-Object {{ $_.Message -match $taskName }} |
        Select-Object -First 8 TimeCreated,Id,LevelDisplayName,Message)
    }} catch {{
      $auth.task.events_error = $_.Exception.Message
    }}
  }}
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}} catch {{
  $auth.error = $_.Exception.Message
}}
try {{
  $bad = & net.exe use "\\\\$dcFqdn\\E2EAuthProof" /user:$proofUser $badPassword 2>&1
  $auth.bad_password_rejected = ($LASTEXITCODE -ne 0)
  if ($LASTEXITCODE -eq 0) {{ & net.exe use "\\\\$dcFqdn\\E2EAuthProof" /delete /y | Out-Null }}
}} catch {{
  $auth.bad_password_rejected = $true
}}
$result.user_auth = $auth
$result | ConvertTo-Json -Depth 10 -Compress
"""

    def network_proof_script(self, spec: LabSpec, peer: LabSpec) -> str:
        return f"""
$ProgressPreference='SilentlyContinue'
$result=[ordered]@{{}}
$result.public_dns = [bool](Resolve-DnsName www.msftconnecttest.com -ErrorAction SilentlyContinue)
try {{
  $r = Invoke-WebRequest -UseBasicParsing -Uri {_ps_string(PUBLIC_EGRESS_URL)} -TimeoutSec 20
  $result.public_https_ok = ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400)
}} catch {{ $result.public_https_ok = $false; $result.public_https_error = $_.Exception.Message }}
$result.cross_lab = [ordered]@{{}}
foreach ($p in @(53,88,389,445)) {{
  $result.cross_lab["port_$p"] = (Test-NetConnection -ComputerName {_ps_string(peer.dc_ip)} -Port $p -InformationLevel Quiet)
}}
$result.management = [ordered]@{{
  controller_5000 = (Test-NetConnection -ComputerName '192.168.2.4' -Port 5000 -InformationLevel Quiet)
  pve_8006 = (Test-NetConnection -ComputerName '192.168.2.48' -Port 8006 -InformationLevel Quiet)
}}
$result | ConvertTo-Json -Depth 8 -Compress
"""

    def dc_cleanup_script(self, spec: LabSpec) -> str:
        return f"""
$ErrorActionPreference='Continue'
Import-Module ActiveDirectory
Remove-SmbShare -Name E2EAuthProof -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force C:\\E2EAuthProof -ErrorAction SilentlyContinue
Get-ADUser -Filter "SamAccountName -eq '{spec.proof_user}'" -ErrorAction SilentlyContinue | Remove-ADUser -Confirm:$false -ErrorAction SilentlyContinue
[ordered]@{{ok=$true}} | ConvertTo-Json -Compress
"""

    def setup_dc_proof_user(self, spec: LabSpec) -> str:
        password = _random_password(f"{spec.netbios}User")
        self.secrets.append(password)
        dc_vmid = self.lab_contexts[spec.domain]["dc_vmid"]
        self.wait_for_qga(dc_vmid)
        proof = self.qm_guest_exec_json(dc_vmid, self.dc_setup_script(spec, password), timeout=300)
        self.record(f"{spec.netbios}_dc_setup", proof)
        if not proof.get("ok"):
            raise E2EError(f"DC setup failed for {spec.domain}: {proof}")
        self.lab_contexts[spec.domain]["proof_user_password"] = password
        return password

    def collect_workstation_proof(self, spec: LabSpec, vmid: int, name: str) -> dict[str, Any]:
        self.wait_for_qga(vmid)
        password = self.lab_contexts[spec.domain]["proof_user_password"]
        proof = self.qm_guest_exec_json(vmid, self.workstation_proof_script(spec, password), timeout=480)
        failures = validate_workstation_proof(proof, spec)
        self.record(f"{name}_domain_auth_proof", {"proof": proof, "failures": failures})
        if failures:
            raise E2EError(f"{name} proof failed: {failures}")
        return proof

    def collect_network_proof(self, spec: LabSpec, peer: LabSpec, vmid: int, name: str) -> dict[str, Any]:
        proof = self.qm_guest_exec_json(vmid, self.network_proof_script(spec, peer), timeout=300)
        findings = []
        if proof.get("public_dns") is not True:
            findings.append("public_dns_failed")
        if proof.get("public_https_ok") is not True:
            findings.append("public_https_failed")
        cross = proof.get("cross_lab") or {}
        for key, value in cross.items():
            if value is True:
                findings.append(f"cross_lab_{key}_reachable")
        management = proof.get("management") or {}
        management_reachable = [key for key, value in management.items() if value is True]
        if management_reachable:
            findings.append("management_reachable_product_gap:" + ",".join(sorted(management_reachable)))
        self.record(f"{name}_network_proof", {"proof": proof, "findings": findings})
        hard = [item for item in findings if not item.startswith("management_reachable_product_gap")]
        if hard:
            raise E2EError(f"{name} network proof failed: {hard}")
        return proof

    def reboot_and_wait(self, vmids: list[int], *, label: str) -> None:
        self.log(f"rebooting {label}: {vmids}")
        for vmid in vmids:
            self.pve_cmd(f"qm guest exec {int(vmid)} -- shutdown.exe /r /t 0 /f", check=False)
        for vmid in vmids:
            self.wait_for_qga(vmid, timeout_seconds=2400)

    def run_baseline_cxw(self) -> None:
        if not self.args.include_existing_cxw:
            return
        self.log("capturing existing CXW baseline before destructive teardown")
        baseline: dict[str, Any] = {}
        for vmid in self.cxw_vmids:
            status_result = self.ssh_result(self.pve, f"qm status {int(vmid)}", timeout=60, check=False)
            config_result = self.ssh_result(self.pve, f"qm config {int(vmid)}", timeout=60, check=False)
            if status_result.returncode != 0:
                baseline[str(vmid)] = {
                    "present": False,
                    "status_error": (status_result.stderr or status_result.stdout or "").strip(),
                    "config_error": (config_result.stderr or config_result.stdout or "").strip(),
                }
                continue
            self.cxw_present_vmids.append(vmid)
            entry: dict[str, Any] = {
                "present": True,
                "status": status_result.stdout.strip(),
                "config": config_result.stdout,
            }
            script = """
$ProgressPreference='SilentlyContinue'
$cs=Get-CimInstance Win32_ComputerSystem
[ordered]@{
  computer=$env:COMPUTERNAME
  domain=$cs.Domain
  part_of_domain=[bool]$cs.PartOfDomain
  ipv4=@(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -like '10.77.20.*'} | Select-Object -ExpandProperty IPAddress)
} | ConvertTo-Json -Compress
"""
            try:
                self.wait_for_qga(vmid, timeout_seconds=300)
                entry["qga_proof"] = self.qm_guest_exec_json(vmid, script, timeout=120)
            except Exception as exc:
                entry["qga_error"] = str(exc)
            baseline[str(vmid)] = entry
        self.record("cxw_baseline", baseline)
        self.cxw_baseline_captured = True

    def destroy_vm(self, vmid: int) -> None:
        allowed = set(self.created_vmids)
        if should_include_existing_cxw(self.args.include_existing_cxw, self.cxw_baseline_captured):
            allowed.update(self.cxw_present_vmids)
        if vmid not in allowed:
            raise E2EError(f"refusing to destroy VMID {vmid}; not tracked as E2E")
        self.log(f"destroying VMID {vmid}")
        self.pve_cmd(f"qm stop {int(vmid)} --timeout 120", timeout=180, check=False)
        self.pve_cmd(f"qm destroy {int(vmid)} --purge 1 --destroy-unreferenced-disks 1", timeout=600, check=False)

    def archive_and_delete_records(self, action: TeardownAction) -> None:
        kind = action.kind
        args = action.args
        if kind == "archive_cloudosd":
            self.controller_python(
                f"""
import json
from web import db_pg, cloudosd_pg
with db_pg.connect() as conn:
    run = cloudosd_pg.archive_run(conn, {args['run_id']!r}, reason='sdn lab stress e2e teardown', archived_by='sdn-lab-stress-e2e')
print(json.dumps({{'ok': bool(run)}}, default=str))
"""
            )
        elif kind == "archive_osdeploy":
            self.controller_python(
                f"""
import json
from web import db_pg, osdeploy_pg
with db_pg.connect() as conn:
    run = osdeploy_pg.archive_run(conn, {args['run_id']!r}, reason='sdn lab stress e2e teardown')
print(json.dumps({{'ok': bool(run)}}, default=str))
"""
            )
        elif kind == "delete_bubble":
            self.controller_python(
                f"""
import json
from web import db_pg, lab_bubbles_pg
with db_pg.connect() as conn:
    ok = lab_bubbles_pg.delete_bubble(conn, {args['bubble_id']!r})
print(json.dumps({{'ok': ok}}))
"""
            )

    def pve_teardown_scope_vms(self, *, include_cxw: bool) -> list[dict[str, Any]]:
        vm_list = self.pvesh_json(f"pvesh get /nodes/{shlex.quote(self.node)}/qemu", check=False) or []
        allowed_vmids = set(self.created_vmids + (self.cxw_present_vmids if include_cxw else []))
        scoped: list[dict[str, Any]] = []
        for item in vm_list:
            vmid = int(item.get("vmid") or 0)
            name = str(item.get("name") or "")
            if vmid in allowed_vmids or vm_name_in_teardown_scope(name, include_cxw=include_cxw):
                scoped.append({"vmid": vmid, "name": name})
        return scoped

    def cleanup_late_e2e_vms(self, *, planned_vmids: set[int], include_cxw: bool) -> list[int]:
        try:
            self.refresh_tracked_vmids_from_db()
        except Exception as exc:
            self.record("late_vmid_refresh_warning", {"error": str(exc)})
        late: list[int] = []
        for item in self.pve_teardown_scope_vms(include_cxw=include_cxw):
            vmid = int(item.get("vmid") or 0)
            if vmid <= 0 or vmid in planned_vmids:
                continue
            if vmid not in self.created_vmids:
                self.created_vmids.append(vmid)
            late.append(vmid)
        if not late:
            return []
        self.record("late_e2e_vmids_discovered", {"vmids": late})
        for vmid in late:
            self.log(f"late teardown action qm_stop_{vmid}")
            self.pve_cmd(f"qm stop {int(vmid)} --timeout 120", timeout=180, check=False)
            self.log(f"late teardown action qm_destroy_{vmid}")
            self.destroy_vm(vmid)
        return late

    def retry_sdn_teardown(self, objects: list[SdnObject]) -> None:
        if not objects:
            return
        self.log("retrying SDN object teardown")
        order = {"subnet": 0, "vnet": 1, "zone": 2}
        for obj in sorted(objects, key=lambda item: order.get(item.kind, 99)):
            try:
                self.delete_sdn_object(obj)
            except Exception as exc:
                self.record(
                    f"sdn_retry_delete_warning_{obj.kind}_{safe_record_name(obj.name or obj.vnet)}",
                    {"error": str(exc), "object": obj.__dict__},
                )
        self.apply_sdn()

    def teardown(self) -> None:
        self.log("starting teardown")
        try:
            self.clear_active_e2e_jobs(reason="teardown")
        except Exception as exc:
            self.record("teardown_job_cleanup_warning", {"error": str(exc)})
        try:
            self.refresh_tracked_vmids_from_db()
        except Exception as exc:
            self.record("tracked_vmid_refresh_warning", {"error": str(exc)})
        include_cxw = should_include_existing_cxw(self.args.include_existing_cxw, self.cxw_baseline_captured)
        if self.args.include_existing_cxw and not include_cxw:
            self.record("cxw_teardown_skipped", {"reason": "baseline_not_captured", "vmids": self.cxw_vmids})
        cxw_vmids = self.cxw_present_vmids if include_cxw else []
        resources = TeardownResources(
            vmids=sorted(set(self.created_vmids + cxw_vmids)),
            cloudosd_run_ids=list(dict.fromkeys(self.created_cloudosd_runs)),
            osdeploy_run_ids=list(dict.fromkeys(self.created_osdeploy_runs)),
            bubble_ids=list(dict.fromkeys(self.created_bubbles)),
            sdn_objects=list(dict.fromkeys(self.created_sdn + ([
                SdnObject(kind="subnet", vnet="cxwlab1", name="10.77.20.0/24"),
                SdnObject(kind="vnet", name="cxwlab1"),
                SdnObject(kind="zone", name="cxwz1"),
            ] if include_cxw else []))),
        )
        actions = build_teardown_actions(resources)
        self.record("teardown_plan", [action.__dict__ for action in actions])
        for action in actions:
            self.log(f"teardown action {action.name}")
            try:
                if action.kind == "qm_stop":
                    self.pve_cmd(f"qm stop {int(action.args['vmid'])} --timeout 120", timeout=180, check=False)
                elif action.kind == "qm_destroy":
                    self.destroy_vm(int(action.args["vmid"]))
                elif action.kind in {"archive_cloudosd", "archive_osdeploy", "delete_bubble"}:
                    self.archive_and_delete_records(action)
                elif action.kind.startswith("delete_"):
                    if action.kind == "delete_subnet":
                        self.delete_sdn_object(SdnObject(kind="subnet", vnet=action.args["vnet"], name=action.args["subnet"]))
                    elif action.kind == "delete_vnet":
                        self.delete_sdn_object(SdnObject(kind="vnet", name=action.args["vnet"]))
                    elif action.kind == "delete_zone":
                        self.delete_sdn_object(SdnObject(kind="zone", name=action.args["zone"]))
                elif action.kind == "apply_sdn":
                    self.apply_sdn()
            except Exception as exc:
                self.record(f"teardown_error_{action.name}", {"error": str(exc), "action": action.__dict__})
                self.log(f"teardown action failed but continuing: {action.name}: {exc}")
        self.cleanup_late_e2e_vms(planned_vmids=set(resources.vmids), include_cxw=include_cxw)
        self.retry_sdn_teardown(resources.sdn_objects)
        self.verify_teardown()

    def verify_teardown(self) -> None:
        self.log("verifying teardown")
        include_cxw = should_include_existing_cxw(self.args.include_existing_cxw, self.cxw_baseline_captured)
        vnets = self.pvesh_json("pvesh get /cluster/sdn/vnets", check=False) or []
        present = {str(item.get("vnet") or item.get("id") or "") for item in vnets}
        forbidden = {"e2ev30", "e2ev40"}
        if include_cxw:
            forbidden.add("cxwlab1")
        remaining = sorted(forbidden & present)
        remaining_vms = self.pve_teardown_scope_vms(include_cxw=include_cxw)
        self.record("teardown_verification", {"remaining_vnets": remaining, "remaining_vms": remaining_vms})
        if remaining or remaining_vms:
            raise E2EError(f"teardown verification failed: vnets={remaining}, vms={remaining_vms}")

    def cleanup_dc_objects(self) -> None:
        for spec in self.labs:
            ctx = self.lab_contexts.get(spec.domain) or {}
            vmid = int(ctx.get("dc_vmid") or 0)
            if not vmid:
                continue
            try:
                self.qm_guest_exec_json(vmid, self.dc_cleanup_script(spec), timeout=180)
            except Exception as exc:
                self.record(f"{spec.netbios}_dc_cleanup_warning", {"error": str(exc)})

    def discover_orphan_runs_and_bubbles(self, *, include_cxw: bool) -> dict[str, Any]:
        name_filter = "'E2E30-%','E2E40-%'" + (",'CXW-%'" if include_cxw else "")
        bubble_filter = "'E2E%'" + (",'CXW%'" if include_cxw else "")
        payload = self.controller_python(
            f"""
import json
from web import db_pg
with db_pg.connect() as conn:
    cloudosd = [row['run_id'] for row in conn.execute(
        \"\"\"
        select run_id::text as run_id
        from cloudosd_runs
        where archived_at is null
          and (expected_computer_name like any (array[{name_filter}]))
        \"\"\"
    ).fetchall()]
    osdeploy = [row['run_id'] for row in conn.execute(
        \"\"\"
        select run_id::text as run_id
        from osdeploy_runs
        where archived_at is null
          and (expected_computer_name like any (array[{name_filter}]))
        \"\"\"
    ).fetchall()]
    bubbles = [row['id'] for row in conn.execute(
        \"\"\"
        select id::text as id from lab_bubbles where name like any (array[{bubble_filter}])
        \"\"\"
    ).fetchall()]
print(json.dumps({{'cloudosd': cloudosd, 'osdeploy': osdeploy, 'bubbles': bubbles}}, default=str))
""",
            timeout=120,
        )
        return payload

    def static_lab_sdn_objects(self, *, include_cxw: bool) -> list[SdnObject]:
        objects: list[SdnObject] = []
        for spec in self.labs:
            objects.append(SdnObject(kind="subnet", vnet=spec.vnet, name=spec.cidr))
            objects.append(SdnObject(kind="vnet", name=spec.vnet))
            objects.append(SdnObject(kind="zone", name=spec.zone))
        if include_cxw:
            objects.extend([
                SdnObject(kind="subnet", vnet="cxwlab1", name="10.77.20.0/24"),
                SdnObject(kind="vnet", name="cxwlab1"),
                SdnObject(kind="zone", name="cxwz1"),
            ])
        return objects

    def teardown_only(self) -> None:
        self.log("starting teardown-only recovery")
        include_cxw = bool(self.args.include_existing_cxw)
        try:
            self.clear_active_e2e_jobs(reason="teardown_only")
        except Exception as exc:
            self.record("teardown_only_job_cleanup_warning", {"error": str(exc)})

        try:
            scoped_vms = self.pve_teardown_scope_vms(include_cxw=include_cxw)
        except Exception as exc:
            self.record("teardown_only_vm_discover_warning", {"error": str(exc)})
            scoped_vms = []
        for item in scoped_vms:
            vmid = int(item.get("vmid") or 0)
            if vmid > 0 and vmid not in self.created_vmids:
                self.created_vmids.append(vmid)
        self.record("teardown_only_vms_discovered", {"vms": scoped_vms})

        try:
            orphans = self.discover_orphan_runs_and_bubbles(include_cxw=include_cxw)
        except Exception as exc:
            self.record("teardown_only_db_discover_warning", {"error": str(exc)})
            orphans = {"cloudosd": [], "osdeploy": [], "bubbles": []}
        for run_id in orphans.get("cloudosd") or []:
            if run_id and run_id not in self.created_cloudosd_runs:
                self.created_cloudosd_runs.append(run_id)
        for run_id in orphans.get("osdeploy") or []:
            if run_id and run_id not in self.created_osdeploy_runs:
                self.created_osdeploy_runs.append(run_id)
        for bubble_id in orphans.get("bubbles") or []:
            if bubble_id and bubble_id not in self.created_bubbles:
                self.created_bubbles.append(bubble_id)
        self.record("teardown_only_db_discovered", orphans)

        self.created_sdn.extend(
            obj for obj in self.static_lab_sdn_objects(include_cxw=include_cxw)
            if obj not in self.created_sdn
        )

        if include_cxw:
            self.cxw_baseline_captured = True

        self.teardown()

    def run(self) -> None:
        self.preflight()
        primary_error: Exception | None = None
        try:
            self.run_baseline_cxw()
            for spec in self.labs:
                self.create_sdn_objects(spec)
            self.apply_sdn()
            for spec in self.labs:
                bubble_id = self.create_lab_record(spec)
                dc_run = self.create_dc_run(spec, bubble_id)
                self.record(f"{spec.netbios}_dc_run_created", dc_run)
            dc_job_by_run = {
                self.lab_contexts[spec.domain]["dc_run_id"]: str(
                    self.lab_contexts[spec.domain].get("dc_provision_job_id") or ""
                )
                for spec in self.labs
            }
            dc_status = self.wait_for_runs_complete(
                [self.lab_contexts[spec.domain]["dc_run_id"] for spec in self.labs],
                timeout_seconds=self.args.dc_timeout_seconds,
                label="domain_controllers",
                required_jobs=dc_job_by_run,
            )
            self.sync_run_status(dc_status)
            self.switch_labs_to_dc_dns()
            for spec in self.labs:
                self.setup_dc_proof_user(spec)

            all_workstation_runs: list[dict[str, Any]] = []
            bad_run: dict[str, Any] | None = None
            for spec in self.labs:
                good_sequence = self.create_cloudosd_sequence(spec)
                self.lab_contexts[spec.domain]["cloudosd_sequence_id"] = good_sequence.sequence_id
                for index in range(1, 5):
                    run = self.create_cloudosd_run(spec, f"{spec.workstation_prefix}-{index:02d}", good_sequence)
                    all_workstation_runs.append({"spec": spec, "run": run, "name": f"{spec.workstation_prefix}-{index:02d}"})
                if spec == self.labs[0]:
                    bad_sequence = self.create_cloudosd_sequence(spec, bad_password=True)
                    bad_run = self.create_cloudosd_run(spec, "E2E30-BAD-01", bad_sequence, asset_role="negative_workstation")
                    self.record("negative_run_created", bad_run)

            workstation_status = self.wait_for_runs_complete(
                [item["run"]["run_id"] for item in all_workstation_runs],
                timeout_seconds=self.args.cloudosd_timeout_seconds,
                label="cloudosd_workstations",
                negative_run=bad_run,
                required_jobs={
                    item["run"]["run_id"]: str(item["run"].get("provision_job_id") or "")
                    for item in all_workstation_runs
                },
            )
            self.sync_run_status(workstation_status)
            self.attach_vmids_to_run_items(all_workstation_runs, workstation_status)
            self.verify_negative_run(bad_run)

            proofs: list[dict[str, Any]] = []
            for item in all_workstation_runs:
                run = item["run"]
                vmid = int(run["vmid"])
                spec = item["spec"]
                proofs.append(self.collect_workstation_proof(spec, vmid, item["name"]))
                peer = self.labs[1] if spec == self.labs[0] else self.labs[0]
                self.collect_network_proof(spec, peer, vmid, item["name"])

            self.reboot_and_wait(
                [int(self.lab_contexts[spec.domain]["dc_vmid"]) for spec in self.labs],
                label="domain controllers",
            )
            for item in all_workstation_runs:
                self.collect_workstation_proof(item["spec"], int(item["run"]["vmid"]), item["name"] + "_post_dc_reboot")

            self.reboot_and_wait([int(item["run"]["vmid"]) for item in all_workstation_runs], label="workstations")
            for item in all_workstation_runs:
                self.collect_workstation_proof(item["spec"], int(item["run"]["vmid"]), item["name"] + "_post_ws_reboot")

            leaks = scan_secret_leaks(self.evidence_dir, self.secrets)
            self.record("secret_scan", {"leaks": leaks})
            if leaks:
                raise E2EError(f"secret leak detected in evidence: {leaks}")
            self.record("summary", {"ok": True, "workstation_count": len(proofs)})
        except Exception as exc:
            primary_error = exc
            try:
                self.collect_failure_evidence(str(exc))
            except Exception as evidence_exc:
                self.record("failure_evidence_warning", {"error": str(evidence_exc)})
            self.record("primary_failure", {"error": str(exc), "type": type(exc).__name__})
            raise
        finally:
            if self.args.teardown:
                try:
                    self.cleanup_dc_objects()
                    self.teardown()
                except Exception as exc:
                    self.record(
                        "teardown_failure",
                        {
                            "error": str(exc),
                            "type": type(exc).__name__,
                            "primary_error": str(primary_error) if primary_error else "",
                        },
                    )
                    raise

    def verify_negative_run(self, run: dict[str, Any] | None) -> None:
        if not run:
            return
        self.log("verifying negative-path workstation behavior")
        run_id = run["run_id"]
        status = self.fetch_run_status([run_id])
        state = ""

        def predicate() -> tuple[bool, Any]:
            nonlocal state
            current = self.fetch_run_status([run_id])
            self.sync_run_status(current)
            rows = current.get("cloudosd", [])
            state = str(rows[0].get("ts_state") or rows[0].get("state")) if rows else ""
            events = current.get("events", [])
            has_join_signal = negative_join_signal(rows, events)
            return state in {"failed", "full_os_waiting_domain_join", "done"} and has_join_signal, current

        observed = wait_until(
            predicate,
            timeout_seconds=self.args.negative_timeout_seconds,
            interval_seconds=60,
        )
        rendered = json.dumps(redact_secrets(observed), default=str)
        leaked = [secret for secret in self.secrets if secret and secret in rendered]
        self.record("negative_run_result", {"state": state, "observed": observed, "leaked": bool(leaked)})
        if leaked:
            raise E2EError("negative path leaked a secret in run evidence")
        if state == "done":
            raise E2EError("negative-path workstation unexpectedly completed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", default="root@192.168.2.4")
    parser.add_argument("--pve", default="root@192.168.2.48")
    parser.add_argument("--node", default="pve2")
    parser.add_argument("--osdeploy-artifact")
    parser.add_argument("--cloudosd-artifact")
    parser.add_argument("--cloudosd-os-version", default=DEFAULT_CLOUDOSD_OS_VERSION)
    parser.add_argument("--include-existing-cxw", action="store_true")
    parser.add_argument("--teardown", action="store_true")
    parser.add_argument(
        "--teardown-only",
        action="store_true",
        help="Skip the stress run; discover and tear down orphaned E2E VMs, "
             "SDN objects, lab bubbles, and cloudosd/osdeploy run rows left "
             "behind by an interrupted run.",
    )
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--dc-timeout-seconds", type=int, default=7200)
    parser.add_argument("--cloudosd-timeout-seconds", type=int, default=14400)
    parser.add_argument("--negative-timeout-seconds", type=int, default=7200)
    args = parser.parse_args(argv)
    if not args.teardown_only:
        missing = [
            flag
            for flag, value in (
                ("--osdeploy-artifact", args.osdeploy_artifact),
                ("--cloudosd-artifact", args.cloudosd_artifact),
            )
            if not value
        ]
        if missing:
            parser.error(f"the following arguments are required: {', '.join(missing)}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    harness = StressHarness(args)
    try:
        if args.teardown_only:
            harness.teardown_only()
        else:
            harness.run()
    except KeyboardInterrupt as exc:
        harness.record("failure", {"error": "interrupted", "type": type(exc).__name__})
        harness.log("FAILED: interrupted")
        print(f"Evidence: {harness.evidence_dir}", file=sys.stderr)
        return 130
    except Exception as exc:
        harness.record("failure", {"error": str(exc), "type": type(exc).__name__})
        harness.log(f"FAILED: {exc}")
        print(f"Evidence: {harness.evidence_dir}", file=sys.stderr)
        return 1
    print(f"Evidence: {harness.evidence_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
