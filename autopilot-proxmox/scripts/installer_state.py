#!/usr/bin/env python3
"""Installer detection, recommendation, and support helpers."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import shlex
import sys
import tarfile
from pathlib import Path
from typing import Any


CLASS_CLEAN = "clean"
CLASS_PARTIAL = "partial_install"
CLASS_READY = "ready"
CLASS_DRIFTED = "drifted"
CLASS_CONFLICTED = "conflicted"


@dataclasses.dataclass(frozen=True)
class Detection:
    schema: int
    classification: str
    confidence: str
    recommended_action: str
    recommended_phases: list[str]
    safe_to_auto_run: bool
    current_step_id: str
    current_step: str
    failed_check_id: str
    failed_check: str
    blocked_reasons: list[str]
    dirty_reasons: list[str]
    conflicts: list[str]
    safe_repairs: list[str]
    planned_commands: list[str]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|apikey|api_key|client_secret|authorization|cookie|key)",
    re.I,
)
PRIVATE_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.S,
)
HEADER_SECRET_RE = re.compile(r"(?im)^(authorization|cookie):\s*.+$")
PVE_TOKEN_RE = re.compile(r"PVEAPIToken=[^\s'\"`]+")
DOTENV_SECRET_RE = re.compile(
    r"(?im)^([A-Z0-9_]*(TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|CLIENT_SECRET|KEY)[A-Z0-9_]*)=.*$"
)
JSON_SECRET_RE = re.compile(
    r'(?i)("(?:[^"]*(?:token|secret|password|passwd|apikey|api_key|client_secret|authorization|cookie|key)[^"]*)"\s*:\s*)"[^"]*"'
)
GENERIC_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(token|secret|password|passwd|apikey|api_key|client_secret|authorization|cookie|key)\s*[:=]\s*(?P<value>\S+)"
)


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ready", "ok"}
    return bool(value)


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"_load_error": str(exc)}
    return value if isinstance(value, dict) else {"_load_error": "state root is not an object"}


def detect_conflicts(state: dict[str, Any], probes: dict[str, Any]) -> list[str]:
    conflicts: list[str] = []
    for key in ("controller_vmid", "controller_vm_name", "controller_url", "controller_ip"):
        state_value = str(state.get(key, "")).strip()
        probe_value = str(probes.get(key, "")).strip()
        if state_value and probe_value and state_value != probe_value:
            conflicts.append(f"{key} differs: state={state_value} probe={probe_value}")
    return conflicts


def base_command(phase: str) -> str:
    return f"bash scripts/init-proxmox-ve.sh --phase {phase} --resume"


def shell_arg(value: str) -> str:
    return shlex.quote(value)


def clean_detection() -> Detection:
    return Detection(
        schema=1,
        classification=CLASS_CLEAN,
        confidence="high",
        recommended_action="foundation",
        recommended_phases=["foundation"],
        safe_to_auto_run=True,
        current_step_id="foundation.start",
        current_step="Foundation",
        failed_check_id="foundation.start.no_state",
        failed_check="No setup state was found",
        blocked_reasons=[],
        dirty_reasons=["no setup state detected"],
        conflicts=[],
        safe_repairs=["start foundation"],
        planned_commands=[base_command("foundation")],
    )


def foundation_detection(merged: dict[str, Any]) -> Detection:
    confidence = "medium" if merged.get("controller_vm_name") and not merged.get("controller_vmid") else "high"
    safe = confidence == "high"
    return Detection(
        schema=1,
        classification=CLASS_PARTIAL,
        confidence=confidence,
        recommended_action="foundation",
        recommended_phases=["foundation"],
        safe_to_auto_run=safe,
        current_step_id="foundation.setup",
        current_step="Foundation setup",
        failed_check_id="foundation.controller_runtime.not_ready",
        failed_check="Controller runtime is not ready",
        blocked_reasons=[] if safe else ["controller discovery is name-only or incomplete"],
        dirty_reasons=["foundation is incomplete"],
        conflicts=[],
        safe_repairs=["repair PVE access", "repair controller runtime"],
        planned_commands=[base_command("foundation")],
    )


def bootstrap_detection(
    merged: dict[str, Any],
    *,
    allow_windows_download: bool,
    allow_virtio_download: bool,
    windows_iso_url: str = "",
) -> Detection:
    windows_ready = bool_value(merged.get("windows_iso_ready"))
    virtio_ready = bool_value(merged.get("virtio_iso_ready"))
    blocked: list[str] = []
    command = base_command("bootstrap")
    safe_to_auto_run = True
    failed_check_id = "bootstrap.media.not_ready"
    failed_check = "Bootstrap media is not ready"

    if not windows_ready:
        failed_check_id = "bootstrap.media.windows_iso_missing"
        failed_check = "Windows ISO media is missing"
        blocked.append("windows_iso_ready=false")
        if windows_iso_url:
            command += f" --windows-iso-url {shell_arg(windows_iso_url)}"
        elif allow_windows_download:
            command += " --download-windows"
        else:
            safe_to_auto_run = False

    if not virtio_ready:
        if failed_check_id == "bootstrap.media.not_ready":
            failed_check_id = "bootstrap.media.virtio_iso_missing"
            failed_check = "VirtIO ISO media is missing"
        blocked.append("virtio_iso_ready=false")
        if allow_virtio_download:
            command += " --download-virtio"
        else:
            safe_to_auto_run = False

    if windows_ready and virtio_ready:
        blocked.append("media_ready=false")

    return Detection(
        schema=1,
        classification=CLASS_PARTIAL,
        confidence="high",
        recommended_action="bootstrap",
        recommended_phases=["bootstrap"],
        safe_to_auto_run=safe_to_auto_run,
        current_step_id="bootstrap.media",
        current_step="Bootstrap media",
        failed_check_id=failed_check_id,
        failed_check=failed_check,
        blocked_reasons=blocked,
        dirty_reasons=["foundation complete but media gate incomplete"],
        conflicts=[],
        safe_repairs=["rescan media", "publish setup state"],
        planned_commands=[command],
    )


def operational_detection(merged: dict[str, Any]) -> Detection:
    return Detection(
        schema=1,
        classification=CLASS_PARTIAL,
        confidence="high",
        recommended_action="operational",
        recommended_phases=["operational"],
        safe_to_auto_run=True,
        current_step_id="operational.artifacts",
        current_step="Operational repair",
        failed_check_id="operational.artifacts.not_promoted",
        failed_check="Setup artifacts are not promoted",
        blocked_reasons=["promoted_artifacts_ready=false"],
        dirty_reasons=["bootstrap complete but operational readiness incomplete"],
        conflicts=[],
        safe_repairs=["verify controller health", "sync runtime config", "promote artifacts"],
        planned_commands=[base_command("operational")],
    )


def ready_detection(merged: dict[str, Any]) -> Detection:
    if "controller_runtime_config_synced" in merged and not bool_value(
        merged.get("controller_runtime_config_synced")
    ):
        return Detection(
            schema=1,
            classification=CLASS_DRIFTED,
            confidence="high",
            recommended_action="runtime-config",
            recommended_phases=["runtime-config"],
            safe_to_auto_run=True,
            current_step_id="runtime_config.repair",
            current_step="Runtime config repair",
            failed_check_id="runtime_config.controller.config_stale",
            failed_check="Controller runtime config is stale",
            blocked_reasons=["controller_runtime_config_synced=false"],
            dirty_reasons=["operational stack is ready but runtime config drifted"],
            conflicts=[],
            safe_repairs=["sync controller runtime config", "republish setup state"],
            planned_commands=[base_command("runtime-config")],
        )
    return Detection(
        schema=1,
        classification=CLASS_READY,
        confidence="high",
        recommended_action="status",
        recommended_phases=[],
        safe_to_auto_run=True,
        current_step_id="ready.status",
        current_step="Ready",
        failed_check_id="",
        failed_check="",
        blocked_reasons=[],
        dirty_reasons=[],
        conflicts=[],
        safe_repairs=[],
        planned_commands=[],
    )


def conflict_detection(conflicts: list[str]) -> Detection:
    return Detection(
        schema=1,
        classification=CLASS_CONFLICTED,
        confidence="low",
        recommended_action="manual",
        recommended_phases=[],
        safe_to_auto_run=False,
        current_step_id="foundation.controller_vm",
        current_step="Controller VM identity",
        failed_check_id="foundation.controller_vm.identity_conflict",
        failed_check="Controller VM identity conflict",
        blocked_reasons=conflicts,
        dirty_reasons=[],
        conflicts=conflicts,
        safe_repairs=[],
        planned_commands=[],
    )


def unreadable_state_detection(reason: str) -> Detection:
    return Detection(
        schema=1,
        classification=CLASS_CONFLICTED,
        confidence="low",
        recommended_action="manual",
        recommended_phases=[],
        safe_to_auto_run=False,
        current_step_id="foundation.state",
        current_step="Setup state",
        failed_check_id="foundation.state.unreadable",
        failed_check="Setup state file is unreadable",
        blocked_reasons=[reason],
        dirty_reasons=[],
        conflicts=[reason],
        safe_repairs=[],
        planned_commands=[],
    )


def classify_state(
    state: dict[str, Any],
    probes: dict[str, Any],
    *,
    allow_windows_download: bool,
    allow_virtio_download: bool,
    windows_iso_url: str = "",
) -> Detection:
    if state.get("_load_error"):
        return unreadable_state_detection(str(state["_load_error"]))
    if probes.get("_load_error"):
        return unreadable_state_detection(str(probes["_load_error"]))
    merged = {**state, **{k: v for k, v in probes.items() if v not in (None, "")}}
    conflicts = detect_conflicts(state, probes)
    if conflicts:
        return conflict_detection(conflicts)
    if not merged:
        return clean_detection()
    if bool_value(merged.get("operational_ready")):
        return ready_detection(merged)
    if bool_value(merged.get("media_ready")):
        return operational_detection(merged)
    if bool_value(merged.get("controller_runtime_ready")) or bool_value(
        merged.get("controller_vm_ready")
    ):
        return bootstrap_detection(
            merged,
            allow_windows_download=allow_windows_download,
            allow_virtio_download=allow_virtio_download,
            windows_iso_url=windows_iso_url,
        )
    return foundation_detection(merged)


def print_detection_summary(detection: Detection, *, show_commands: bool = False) -> None:
    print("Detection summary")
    print("-----------------")
    print(f"Classification: {detection.classification}")
    print(f"Confidence: {detection.confidence}")
    print(f"Current step: {detection.current_step}")
    if detection.failed_check:
        print(f"Failing check: {detection.failed_check}")
    print(f"Recommended action: {detection.recommended_action}")
    print(f"Safe to auto-run: {'yes' if detection.safe_to_auto_run else 'no'}")
    if detection.blocked_reasons:
        print("Blocked reasons:")
        for reason in detection.blocked_reasons:
            print(f"  - {reason}")
    if detection.conflicts:
        print("Conflicts:")
        for conflict in detection.conflicts:
            print(f"  - {conflict}")
    if show_commands and detection.planned_commands:
        print("Planned commands:")
        for command in detection.planned_commands:
            print(f"  {command}")


def redact_text(text: str) -> tuple[str, list[str]]:
    matches: list[str] = []
    replacements = [
        (PRIVATE_BLOCK_RE, "[REDACTED_PRIVATE_KEY]", "private_key_block"),
        (HEADER_SECRET_RE, r"\1: [REDACTED]", "secret_header"),
        (PVE_TOKEN_RE, "PVEAPIToken=[REDACTED]", "pve_api_token"),
        (DOTENV_SECRET_RE, r"\1=[REDACTED]", "dotenv_secret"),
        (JSON_SECRET_RE, r'\1"[REDACTED]"', "json_secret"),
    ]
    redacted = text
    for pattern, replacement, label in replacements:
        redacted, count = pattern.subn(replacement, redacted)
        if count:
            matches.append(label)
    return redacted, sorted(set(matches))


def has_residual_secret(text: str) -> bool:
    if PRIVATE_BLOCK_RE.search(text):
        return True
    if re.search(r"(?i)authorization:\s*bearer\s+\S+", text):
        return True
    if re.search(r"(?i)PVEAPIToken=(?!\[REDACTED\])\S+", text):
        return True
    for match in GENERIC_SECRET_ASSIGNMENT_RE.finditer(text):
        if match.group("value") != "[REDACTED]":
            return True
    return False


def tail_text(path: Path, max_lines: int = 80) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def detection_from_file(path: Path) -> Detection:
    data = load_json_file(path)
    if not data:
        return clean_detection()
    defaults = clean_detection().to_dict()
    fields = {field.name for field in dataclasses.fields(Detection)}
    normalized = {key: data.get(key, defaults[key]) for key in fields}
    for key, value in list(normalized.items()):
        if value is None:
            normalized[key] = defaults[key]
    for key in (
        "recommended_phases",
        "blocked_reasons",
        "dirty_reasons",
        "conflicts",
        "safe_repairs",
        "planned_commands",
    ):
        if not isinstance(normalized.get(key), list):
            normalized[key] = []
    return Detection(**normalized)


def build_issue_draft(
    detection: Detection,
    failure: dict[str, Any],
    log_tail: str,
    *,
    include_environment: bool,
    support_bundle_path: Path | None,
) -> str:
    step_label = failure.get("step_label") or detection.current_step or "installer setup"
    check_label = failure.get("check_label") or detection.failed_check
    body = [
        f"# Installer blocked during {step_label}",
        "",
        "## Current step",
        f"- Installer action: {failure.get('action', detection.recommended_action)}",
        f"- Phase: {failure.get('phase', detection.recommended_action)}",
        f"- Step: {step_label}",
        f"- Step ID: {failure.get('step_id', detection.current_step_id)}",
        f"- Failed check: {check_label or 'No failed check was detected'}",
        f"- Check ID: {failure.get('check_id', detection.failed_check_id)}",
        f"- Exit code: {failure.get('exit_code', '')}",
        f"- State classification: {detection.classification}",
        f"- Confidence: {detection.confidence}",
        "",
        "## What happened",
        check_label or "No failed check was detected; this is a setup support snapshot.",
        "",
        "## Recommended by installer",
        "```bash",
        detection.planned_commands[0] if detection.planned_commands else "No automatic command was recommended.",
        "```",
        "",
        "## Evidence summary",
    ]
    evidence = detection.blocked_reasons + detection.dirty_reasons + detection.conflicts
    body.extend([f"- {reason}" for reason in evidence] or ["- No failed check was detected."])
    if include_environment:
        body.extend(["", "## Environment", "- Local environment details were included by operator request."])
    body.extend(
        [
            "",
            "## Recent installer log",
            "```text",
            log_tail.strip(),
            "```",
            "",
            "## Redaction",
            "The support helper removed tokens, passwords, private keys, cookies, and authorization headers.",
        ]
    )
    if support_bundle_path:
        body.extend(["", f"Support bundle: `{support_bundle_path}`"])
    return "\n".join(body) + "\n"


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-%H%M%S")


def write_support_outputs(
    *,
    detection_file: Path,
    failure_file: Path,
    log_file: Path,
    output_dir: Path,
    no_bundle: bool,
    print_draft: bool,
    include_environment: bool,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    now = timestamp()
    detection = detection_from_file(detection_file)
    failure = load_json_file(failure_file)
    raw_tail = tail_text(log_file)
    redacted_tail, matches = redact_text(raw_tail)
    residual = has_residual_secret(redacted_tail)

    bundle_path: Path | None = None
    report = {
        "schema": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "included_files": [],
        "skipped_files": [],
        "redaction_patterns": matches,
        "refused_bundle": bool(residual),
    }
    report_path = output_dir / f"redaction-report-{now}.json"

    if not no_bundle and not residual:
        bundle_path = output_dir / f"support-bundle-{now}.tar.gz"
        safe_log = output_dir / f"install-log-redacted-{now}.txt"
        safe_detection = output_dir / f"installer-detect-{now}.json"
        safe_failure = output_dir / f"install-last-failure-{now}.json"
        safe_log.write_text(redacted_tail + "\n", encoding="utf-8")
        safe_detection.write_text(json.dumps(detection.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        safe_failure.write_text(json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with tarfile.open(bundle_path, "w:gz") as tar:
            for item in (safe_log, safe_detection, safe_failure):
                tar.add(item, arcname=item.name)
                report["included_files"].append(item.name)
    elif residual:
        report["skipped_files"].append("support bundle refused: residual secret pattern detected")

    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    draft = build_issue_draft(
        detection,
        failure,
        redacted_tail,
        include_environment=include_environment,
        support_bundle_path=bundle_path,
    )
    if has_residual_secret(draft):
        draft = "# Installer support snapshot\n\nRedaction failed. A full issue draft was not written.\n"
    draft_path = output_dir / f"github-issue-{now}.md"
    draft_path.write_text(draft, encoding="utf-8")

    print(f"Issue draft: {draft_path}")
    if bundle_path:
        print(f"Support bundle: {bundle_path}")
    print(f"Redaction report: {report_path}")
    if print_draft:
        print()
        print(draft)
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    state = load_json_file(args.state_file)
    probes = load_json_file(args.probe_file) if args.probe_file else {}
    detection = classify_state(
        state,
        probes,
        allow_windows_download=args.allow_windows_download,
        allow_virtio_download=args.allow_virtio_download,
        windows_iso_url=args.windows_iso_url or "",
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(detection.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(detection.to_dict(), indent=2, sort_keys=True))
    else:
        print_detection_summary(detection, show_commands=args.show_commands)
    return 0


def cmd_support(args: argparse.Namespace) -> int:
    return write_support_outputs(
        detection_file=args.detection_file,
        failure_file=args.failure_file,
        log_file=args.log_file,
        output_dir=args.output_dir,
        no_bundle=args.no_bundle,
        print_draft=args.print,
        include_environment=args.include_environment,
    )


def cmd_redact_check(args: argparse.Namespace) -> int:
    text = args.path.read_text(encoding="utf-8", errors="replace")
    redacted, matches = redact_text(text)
    if has_residual_secret(redacted):
        print("residual secret pattern detected", file=sys.stderr)
        return 2
    if args.output:
        args.output.write_text(redacted, encoding="utf-8")
    print(json.dumps({"redaction_patterns": matches}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    detect = sub.add_parser("detect", help="detect installer state")
    detect.add_argument("--state-file", type=Path, required=True)
    detect.add_argument("--probe-file", type=Path)
    detect.add_argument("--output", type=Path)
    detect.add_argument("--json", action="store_true")
    detect.add_argument("--allow-windows-download", action="store_true")
    detect.add_argument("--allow-virtio-download", action="store_true")
    detect.add_argument("--windows-iso-url", default="")
    detect.add_argument("--show-commands", action="store_true")
    detect.set_defaults(func=cmd_detect)

    support = sub.add_parser("support", help="create issue draft and support bundle")
    support.add_argument("--detection-file", type=Path, required=True)
    support.add_argument("--failure-file", type=Path, required=True)
    support.add_argument("--log-file", type=Path, required=True)
    support.add_argument("--output-dir", type=Path, required=True)
    support.add_argument("--no-bundle", action="store_true")
    support.add_argument("--print", action="store_true")
    support.add_argument("--include-environment", action="store_true")
    support.set_defaults(func=cmd_support)

    redact = sub.add_parser("redact-check", help="redact a file and fail on residual secrets")
    redact.add_argument("path", type=Path)
    redact.add_argument("--output", type=Path)
    redact.set_defaults(func=cmd_redact_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
