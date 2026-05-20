# Installer Resume and Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a restart-aware Proxmox VE console installer that detects existing stack state, recommends the safest next repair point, refuses ambiguous or destructive automatic work, and creates sanitized GitHub issue/support artifacts when setup fails.

**Architecture:** Keep `install-proxmox-ve.sh` as the thin console/menu layer over `init-proxmox-ve.sh`. Add a small Python helper, `installer_state.py`, for deterministic detection classification, recommendation, redaction, issue draft generation, and support bundle assembly. Existing init phases remain the only mutation path.

**Tech Stack:** Bash, Python 3 standard library, pytest, existing ProxmoxVEAutopilot shell scripts and script-contract tests.

---

## Reference Inputs

- Approved spec: `docs/superpowers/specs/2026-05-19-installer-resume-and-support-design.md`
- Current shell installer: `autopilot-proxmox/scripts/install-proxmox-ve.sh`
- Current init entrypoint: `autopilot-proxmox/scripts/init-proxmox-ve.sh`
- Existing script tests: `autopilot-proxmox/tests/test_first_run_init_scripts.py`

## Implementation File Structure

```text
autopilot-proxmox/
  scripts/
    install-proxmox-ve.sh          # update CLI actions, menus, failure capture, recommended flow
    installer_state.py             # new pure helper for detect/support/redaction
    init-proxmox-ve.sh             # minimal touch only if stable state keys/check IDs are missing
  tests/
    test_first_run_init_scripts.py # update shell contract tests
    test_installer_state_helper.py # new pure Python tests
```

## Data Contracts

### Detection JSON

`installer_state.py detect` writes this JSON shape to `output/setup/installer_detect.json` and prints a readable summary unless `--json` is passed.

```json
{
  "schema": 1,
  "classification": "partial_install",
  "confidence": "high",
  "recommended_action": "bootstrap",
  "recommended_phases": ["bootstrap"],
  "safe_to_auto_run": false,
  "current_step_id": "bootstrap.media",
  "current_step": "Bootstrap media",
  "failed_check_id": "bootstrap.media.windows_iso_missing",
  "failed_check": "Windows ISO media is missing",
  "blocked_reasons": ["windows_iso_ready=false"],
  "dirty_reasons": ["foundation complete but media gate incomplete"],
  "conflicts": [],
  "safe_repairs": ["rescan media", "publish setup state"],
  "planned_commands": [
    "bash scripts/init-proxmox-ve.sh --phase bootstrap --resume"
  ]
}
```

### Last Failure JSON

`install-proxmox-ve.sh` writes this file only when a phase exits non-zero:

```json
{
  "schema": 1,
  "timestamp": "2026-05-19T23:15:00Z",
  "action": "bootstrap",
  "phase": "bootstrap",
  "step_id": "bootstrap.media",
  "step_label": "Bootstrap media",
  "check_id": "bootstrap.media.windows_iso_missing",
  "check_label": "Windows ISO media is missing",
  "exit_code": 20,
  "classification": "partial_install",
  "confidence": "high",
  "blocked_reasons": ["windows_iso_ready=false"],
  "recommended_action": "bootstrap",
  "sanitized_planned_commands": [
    "bash scripts/init-proxmox-ve.sh --phase bootstrap --resume"
  ]
}
```

### Support Output

```text
autopilot-proxmox/output/support/
  github-issue-YYYY-MM-DD-HHMMSS.md
  support-bundle-YYYY-MM-DD-HHMMSS.tar.gz
  redaction-report-YYYY-MM-DD-HHMMSS.json
```

---

## Tasks

### 1. Add Python Helper Skeleton and Unit-Testable Detection Model

- [ ] Create `autopilot-proxmox/scripts/installer_state.py`.
- [ ] Keep it standard-library only.
- [ ] Implement command parsing for `detect`, `support`, and `redact-check`.
- [ ] Implement pure functions first; CLI glue should call those functions.

Start with this shape:

```python
#!/usr/bin/env python3
"""Installer detection, recommendation, and support helpers."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
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
```

Implement state loading defensively:

```python
def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"_load_error": str(exc)}
    return value if isinstance(value, dict) else {"_load_error": "state root is not an object"}
```

Add a deterministic classifier that only consumes dictionaries, so tests do not need Proxmox:

```python
def classify_state(
    state: dict[str, Any],
    probes: dict[str, Any],
    *,
    allow_windows_download: bool,
    allow_virtio_download: bool,
    windows_iso_url: str = "",
) -> Detection:
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
    if bool_value(merged.get("controller_runtime_ready")) or bool_value(merged.get("controller_vm_ready")):
        return bootstrap_detection(
            merged,
            allow_windows_download=allow_windows_download,
            allow_virtio_download=allow_virtio_download,
            windows_iso_url=windows_iso_url,
        )
    return foundation_detection(merged)
```

Acceptance for this task:

- `python3 autopilot-proxmox/scripts/installer_state.py detect --state-file /tmp/missing.json --json` exits 0.
- Missing state recommends Foundation.
- The helper never imports repo web modules or Proxmox libraries.

### 2. Add Pure Detection Tests Before Wiring Bash

- [ ] Create `autopilot-proxmox/tests/test_installer_state_helper.py`.
- [ ] Import the helper with `importlib.util.spec_from_file_location`.
- [ ] Cover all required classifications from the spec.

Test loader snippet:

```python
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "installer_state.py"

spec = importlib.util.spec_from_file_location("installer_state", HELPER_PATH)
installer_state = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = installer_state
spec.loader.exec_module(installer_state)
```

Required tests:

```python
def test_clean_state_recommends_foundation():
    detection = installer_state.classify_state({}, {}, allow_windows_download=False, allow_virtio_download=False)

    assert detection.classification == "clean"
    assert detection.confidence == "high"
    assert detection.recommended_action == "foundation"
    assert detection.recommended_phases == ["foundation"]
    assert detection.safe_to_auto_run is True
    assert detection.current_step_id == "foundation.start"


def test_foundation_complete_state_recommends_bootstrap_without_silent_windows_download():
    detection = installer_state.classify_state(
        {
            "controller_vm_ready": True,
            "controller_runtime_ready": True,
            "windows_iso_ready": False,
            "virtio_iso_ready": True,
            "media_ready": False,
        },
        {},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.classification == "partial_install"
    assert detection.recommended_action == "bootstrap"
    assert detection.recommended_phases == ["bootstrap"]
    assert detection.safe_to_auto_run is False
    assert detection.failed_check_id == "bootstrap.media.windows_iso_missing"
    assert "--download-windows" not in " ".join(detection.planned_commands)


def test_foundation_complete_state_with_download_flag_can_auto_run_bootstrap():
    detection = installer_state.classify_state(
        {
            "controller_vm_ready": True,
            "controller_runtime_ready": True,
            "windows_iso_ready": False,
            "virtio_iso_ready": True,
            "media_ready": False,
        },
        {},
        allow_windows_download=True,
        allow_virtio_download=True,
    )

    assert detection.safe_to_auto_run is True
    assert "--download-windows" in " ".join(detection.planned_commands)


def test_foundation_complete_state_with_direct_url_uses_url_not_resolver():
    detection = installer_state.classify_state(
        {
            "controller_vm_ready": True,
            "controller_runtime_ready": True,
            "windows_iso_ready": False,
            "virtio_iso_ready": True,
            "media_ready": False,
        },
        {},
        allow_windows_download=True,
        allow_virtio_download=False,
        windows_iso_url="https://example.test/windows.iso",
    )

    planned = " ".join(detection.planned_commands)
    assert detection.safe_to_auto_run is True
    assert "--windows-iso-url https://example.test/windows.iso" in planned
    assert "--download-windows" not in planned


def test_bootstrap_complete_state_recommends_operational():
    detection = installer_state.classify_state(
        {
            "controller_runtime_ready": True,
            "windows_iso_ready": True,
            "virtio_iso_ready": True,
            "media_ready": True,
            "promoted_artifacts_ready": False,
        },
        {},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.recommended_action == "operational"
    assert detection.recommended_phases == ["operational"]
    assert detection.failed_check_id == "operational.artifacts.not_promoted"


def test_ready_but_runtime_config_stale_recommends_runtime_config_repair():
    detection = installer_state.classify_state(
        {
            "operational_ready": True,
            "controller_runtime_config_synced": False,
        },
        {},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.classification == "drifted"
    assert detection.recommended_action == "runtime-config"
    assert detection.safe_to_auto_run is True


def test_controller_identity_conflict_blocks_auto_run():
    detection = installer_state.classify_state(
        {"controller_vmid": "181", "controller_vm_name": "autopilot-controller-01"},
        {"controller_vmid": "182", "controller_vm_name": "autopilot-controller-01"},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.classification == "conflicted"
    assert detection.confidence == "low"
    assert detection.safe_to_auto_run is False
    assert detection.conflicts
    assert detection.failed_check_id == "foundation.controller_vm.identity_conflict"


def test_name_only_discovery_is_not_high_confidence():
    detection = installer_state.classify_state(
        {},
        {"controller_vm_name": "autopilot-controller-01"},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.confidence in {"medium", "low"}
    assert detection.safe_to_auto_run is False
```

Acceptance for this task:

- `pytest autopilot-proxmox/tests/test_installer_state_helper.py` fails for missing implementation first, then passes after task 1 is complete.
- Tests do not shell out to `qm`, `pvesm`, `curl`, or Docker.

### 3. Implement Redaction, Issue Draft, and Fail-Closed Support Bundle

- [ ] Add redaction functions to `installer_state.py`.
- [ ] Redact line-oriented logs, JSON values, dotenv-style files, and PEM/private key blocks.
- [ ] Refuse bundle generation if secret-looking content remains after redaction.
- [ ] Always allow a minimal issue draft even if bundle redaction fails.

Core redaction constants:

```python
SECRET_FIELD_PATTERN = (
    r"token|secret|password|passwd|apikey|api_key|client_secret|authorization|cookie|"
    r"(?:private|secret|api|access|session)[_-]?key|key[_-]?(?:id|secret|value)"
)
SECRET_KEY_RE = re.compile(rf"\b(?:{SECRET_FIELD_PATTERN})\b", re.I)
PRIVATE_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.S,
)
HEADER_SECRET_RE = re.compile(r"(?im)^(authorization|cookie):\s*.+$")
PVE_TOKEN_RE = re.compile(r"PVEAPIToken=[^\s'\"`]+")
DOTENV_SECRET_RE = re.compile(rf"(?im)^([A-Z0-9_]*(?:{SECRET_FIELD_PATTERN})[A-Z0-9_]*)=.*$")
GENERIC_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?:{SECRET_FIELD_PATTERN})\s*[:=]\s*(?P<value>\S+)"
)
```

Redaction function:

```python
def redact_text(text: str) -> tuple[str, list[str]]:
    matches: list[str] = []
    replacements = [
        (PRIVATE_BLOCK_RE, "[REDACTED_PRIVATE_KEY]", "private_key_block"),
        (HEADER_SECRET_RE, r"\1: [REDACTED]", "secret_header"),
        (PVE_TOKEN_RE, "PVEAPIToken=[REDACTED]", "pve_api_token"),
        (DOTENV_SECRET_RE, r"\1=[REDACTED]", "dotenv_secret"),
    ]
    redacted = text
    for pattern, replacement, label in replacements:
        redacted, count = pattern.subn(replacement, redacted)
        if count:
            matches.append(label)
    return redacted, sorted(set(matches))
```

Residual detector:

```python
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
```

Issue draft function:

```python
def build_issue_draft(
    detection: Detection,
    failure: dict[str, Any],
    log_tail: str,
    *,
    include_environment: bool,
    support_bundle_path: Path | None,
) -> str:
    title_step = detection.current_step or failure.get("step_label") or "installer setup"
    return "\n".join(
        [
            f"# Installer blocked during {title_step}",
            "",
            "## Current step",
            f"- Installer action: {failure.get('action', detection.recommended_action)}",
            f"- Phase: {failure.get('phase', detection.recommended_action)}",
            f"- Step: {failure.get('step_label', detection.current_step)}",
            f"- Step ID: {failure.get('step_id', detection.current_step_id)}",
            f"- Failed check: {failure.get('check_label', detection.failed_check)}",
            f"- Check ID: {failure.get('check_id', detection.failed_check_id)}",
            f"- Exit code: {failure.get('exit_code', '')}",
            f"- State classification: {detection.classification}",
            f"- Confidence: {detection.confidence}",
            "",
            "## What happened",
            detection.failed_check or "No failed check was detected; this is a setup support snapshot.",
            "",
            "## Recommended by installer",
            "```bash",
            detection.planned_commands[0] if detection.planned_commands else "No automatic command was recommended.",
            "```",
            "",
            "## Evidence summary",
            *[f"- {reason}" for reason in detection.blocked_reasons + detection.dirty_reasons],
            "",
            "## Recent installer log",
            "```text",
            log_tail.strip(),
            "```",
            "",
            "## Redaction",
            "The support helper removed tokens, passwords, private keys, cookies, and authorization headers.",
            *(["", f"Support bundle: `{support_bundle_path}`"] if support_bundle_path else []),
        ]
    )
```

Required tests:

```python
def test_redaction_removes_known_secret_patterns():
    text = '''
Authorization: Bearer abc123
Cookie: session=abc
PVEAPIToken=root@pam!autopilot=supersecret
AUTOPILOT_POSTGRES_PASSWORD=secret
-----BEGIN OPENSSH PRIVATE KEY-----
abc
-----END OPENSSH PRIVATE KEY-----
'''
    redacted, matches = installer_state.redact_text(text)

    assert "abc123" not in redacted
    assert "supersecret" not in redacted
    assert "AUTOPILOT_POSTGRES_PASSWORD=[REDACTED]" in redacted
    assert "PRIVATE KEY" not in redacted
    assert matches


def test_support_bundle_fails_closed_when_residual_secret_remains(tmp_path):
    path = tmp_path / "install.log"
    path.write_text("Authorization: Bearer still-secret\n", encoding="utf-8")

    redacted, _ = installer_state.redact_text(path.read_text(encoding="utf-8"))
    assert installer_state.has_residual_secret(redacted) is False

    suspicious = "token = still-present-secret-value"
    assert installer_state.has_residual_secret(suspicious) is True


def test_issue_draft_includes_step_and_check_ids():
    detection = installer_state.clean_detection()
    draft = installer_state.build_issue_draft(
        detection,
        {
            "action": "recommended",
            "phase": "foundation",
            "step_id": "foundation.start",
            "check_id": "foundation.start.no_state",
            "exit_code": 1,
        },
        "recent log",
        include_environment=False,
        support_bundle_path=None,
    )

    assert "Step ID: foundation.start" in draft
    assert "Check ID: foundation.start.no_state" in draft
```

Acceptance for this task:

- Helper writes issue draft under `output/support/`.
- Full support bundle refuses to write when residual secret detection fails.
- Redaction report lists pattern names, included files, skipped files, and refusal reason without matched secret values.

### 4. Wire Read-Only Detect, Status, and Recommended Actions in Bash

- [ ] Update `autopilot-proxmox/scripts/install-proxmox-ve.sh` constants.
- [ ] Add `--action detect`, `--action recommended`, and `--action support`.
- [ ] Make `status` call the read-only detection model and then show current state.
- [ ] Ensure `detect` and `status` never call `run_init_phase`.
- [ ] Track explicit Windows download consent separately from the existing `MEDIA_MODE=auto` default.

Add constants near the top:

```bash
DETECT_FILE="${APP_DIR}/output/setup/installer_detect.json"
FAILURE_FILE="${APP_DIR}/output/setup/install-last-failure.json"
INSTALL_LOG="${APP_DIR}/output/setup/install.log"
SUPPORT_DIR="${APP_DIR}/output/support"
STATE_HELPER="${SCRIPT_DIR}/installer_state.py"
ALLOW_WINDOWS_DOWNLOAD=0
DOWNLOAD_VIRTIO=0
```

Add helper wrapper:

```bash
run_detect() {
  local args
  args=(python3 "${STATE_HELPER}" detect --state-file "${STATE_FILE}" --output "${DETECT_FILE}")
  if [[ "${ALLOW_WINDOWS_DOWNLOAD}" == "1" ]]; then
    args+=(--allow-windows-download)
  fi
  if [[ -n "${WINDOWS_ISO_URL}" ]]; then
    args+=(--windows-iso-url "${WINDOWS_ISO_URL}")
  fi
  if [[ "${DOWNLOAD_VIRTIO}" == "1" ]]; then
    args+=(--allow-virtio-download)
  fi
  "${args[@]}"
}
```

Detection is read-only and should still write `installer_detect.json` during shell `--dry-run`; otherwise `recommended --dry-run` cannot read the planned action. If future live probes are added, use an explicit helper flag such as `--no-live-probes` instead of making helper `--dry-run` skip output.

Update media argument construction so the existing `MEDIA_MODE=auto` default does not imply download consent:

```bash
build_media_args() {
  MEDIA_ARGS=()
  case "${MEDIA_MODE}" in
    auto)
      [[ "${ALLOW_WINDOWS_DOWNLOAD}" == "1" ]] && MEDIA_ARGS+=(--download-windows)
      ;;
    url)
      [[ -n "${WINDOWS_ISO_URL}" ]] || die "--windows-iso-url requires a URL"
      MEDIA_ARGS+=(--windows-iso-url "${WINDOWS_ISO_URL}")
      ;;
    manual)
      ;;
    *) die "invalid media mode: ${MEDIA_MODE}" ;;
  esac
  [[ -n "${WINDOWS_ISO_LANGUAGE}" ]] && MEDIA_ARGS+=(--windows-iso-language "${WINDOWS_ISO_LANGUAGE}")
  [[ "${DOWNLOAD_VIRTIO}" == "1" ]] && MEDIA_ARGS+=(--download-virtio)
  return 0
}
```

Split configuration prompts by phase so operational/runtime repair does not ask for Windows media unnecessarily:

```bash
configure_for_phase() {
  local phase="$1"
  [[ "${YES}" == "1" ]] && return 0
  configure_targeting_interactive
  case "${phase}" in
    guided|bootstrap|all) select_media_mode ;;
  esac
}
```

Then call `configure_for_phase "${phase}"` from `run_single_phase` and call `configure_for_phase guided` from `guided_install`.

Load recommendation fields with Python instead of fragile shell parsing:

```bash
detect_value() {
  local key="$1"
  python3 - "${DETECT_FILE}" "${key}" <<'PY'
import json
import sys
path, key = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
value = data.get(key, "")
if isinstance(value, bool):
    print("1" if value else "0")
elif isinstance(value, list):
    print(",".join(str(item) for item in value))
else:
    print(value)
PY
}
```

Add recommended runner:

```bash
run_recommended() {
  local action safe confidence
  run_detect
  action="$(detect_value recommended_action)"
  safe="$(detect_value safe_to_auto_run)"
  confidence="$(detect_value confidence)"

  if [[ "${safe}" != "1" || "${confidence}" == "low" || -z "${action}" ]]; then
    echo
    echo "Recommended repair is not safe to run automatically."
    echo "Use Detection details or Create GitHub issue / support bundle for next steps."
    return 2
  fi

  case "${action}" in
    foundation|bootstrap|operational|runtime-config)
      run_single_phase "${action}"
      ;;
    status|none)
      show_state
      ;;
    *)
      die "invalid recommended action from detection: ${action}"
      ;;
  esac
}
```

Add CLI usage line:

```text
--action menu|detect|recommended|guided|foundation|bootstrap|operational|runtime-config|status|support|reset-dev-lab
```

Acceptance for this task:

- `bash autopilot-proxmox/scripts/install-proxmox-ve.sh --action detect --dry-run` does not print `--phase`.
- `bash autopilot-proxmox/scripts/install-proxmox-ve.sh --action status --dry-run` does not print `--phase`.
- `bash autopilot-proxmox/scripts/install-proxmox-ve.sh --action recommended --yes --dry-run` prints exactly the planned safe phase command only when `safe_to_auto_run=true`.
- `bash autopilot-proxmox/scripts/install-proxmox-ve.sh --action recommended --yes --dry-run` does not include `--download-windows` unless the caller also passed `--download-windows` or selected automatic media download interactively.
- `bash autopilot-proxmox/scripts/install-proxmox-ve.sh --action bootstrap --yes --dry-run` does not include `--download-windows` unless the caller also passed `--download-windows` or `--windows-iso-url`.
- `bash autopilot-proxmox/scripts/install-proxmox-ve.sh --action bootstrap --yes --dry-run` does not include `--download-virtio` unless the caller also passed `--download-virtio` or selected VirtIO download interactively.
- `bash autopilot-proxmox/scripts/install-proxmox-ve.sh --action recommended --yes --dry-run --windows-iso-url https://example.test/windows.iso` includes `--windows-iso-url` in the planned command and does not substitute `--download-windows`.

### 5. Capture Failure Context and Add Failure Footer

- [ ] Add an `installer_log` function that appends safe UI messages to `install.log`.
- [ ] Update `run_init_phase` to record failures with stable step/check IDs.
- [ ] Print the failure footer after a non-zero phase exit.
- [ ] Keep rc `20` mapped to the media gate IDs.

Failure mapping can be intentionally small for the first pass:

```bash
failure_step_id_for_phase() {
  local phase="$1" rc="${2:-1}"
  case "${phase}:${rc}" in
    bootstrap:20) echo "bootstrap.media|Bootstrap media|bootstrap.media.windows_iso_missing|Windows ISO media is missing" ;;
    foundation:*) echo "foundation.setup|Foundation setup|foundation.setup.phase_failed|Foundation phase failed" ;;
    bootstrap:*) echo "bootstrap.media|Bootstrap media|bootstrap.media.phase_failed|Bootstrap phase failed" ;;
    operational:*) echo "operational.repair|Operational repair|operational.repair.phase_failed|Operational phase failed" ;;
    runtime-config:*) echo "runtime_config.repair|Runtime config repair|runtime_config.repair.phase_failed|Runtime config repair failed" ;;
    *) echo "${phase}.run|${phase}|${phase}.run.phase_failed|${phase} failed" ;;
  esac
}
```

Write failure JSON safely through Python:

```bash
record_failure() {
  local action="$1" phase="$2" rc="$3" mapping
  mapping="$(failure_step_id_for_phase "${phase}" "${rc}")"
  python3 - "${FAILURE_FILE}" "${DETECT_FILE}" "${action}" "${phase}" "${rc}" "${mapping}" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

failure_path = Path(sys.argv[1])
detect_path = Path(sys.argv[2])
action, phase, rc, mapping = sys.argv[3], sys.argv[4], int(sys.argv[5]), sys.argv[6]
step_id, step_label, check_id, check_label = mapping.split("|", 3)
try:
    detection = json.loads(detect_path.read_text(encoding="utf-8"))
except Exception:
    detection = {}
payload = {
    "schema": 1,
    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    "action": action,
    "phase": phase,
    "step_id": step_id,
    "step_label": step_label,
    "check_id": check_id,
    "check_label": check_label,
    "exit_code": rc,
    "classification": detection.get("classification", ""),
    "confidence": detection.get("confidence", ""),
    "blocked_reasons": detection.get("blocked_reasons", []),
    "recommended_action": detection.get("recommended_action", ""),
    "sanitized_planned_commands": detection.get("planned_commands", []),
}
failure_path.parent.mkdir(parents=True, exist_ok=True)
failure_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}
```

Footer:

```bash
show_failure_footer() {
  local phase="$1" rc="$2" mapping
  mapping="$(failure_step_id_for_phase "${phase}" "${rc}")"
  IFS='|' read -r step_id step_label check_id check_label <<< "${mapping}"
  cat <<EOF

${step_label} did not complete.

Step: ${step_label}
Step ID: ${step_id}
Failed check: ${check_label}
Check ID: ${check_id}
Exit code: ${rc}

Next actions:
  1) Retry recommended repair
  2) Detection details
  3) Manual phase selection
  4) Create GitHub issue / support bundle
  0) Return to main menu
EOF
}
```

Acceptance for this task:

- Bootstrap rc `20` failure writes `install-last-failure.json` with `bootstrap.media.windows_iso_missing`.
- Failure footer contains step label, step ID, check label, check ID, exit code, and issue helper option.
- Existing rc behavior remains unchanged; `run_init_phase` still returns the real init script exit code.

### 6. Rework Interactive Menus Around Detection and Support Option 7

- [ ] Update `menu_loop` to run detection before printing the main menu.
- [ ] Replace the current flat menu with the approved main menu.
- [ ] Add `manual_phase_menu`.
- [ ] Move Reset Dev Lab to manual option `8`.
- [ ] Make option `7` create a GitHub issue/support bundle in both menus.

Main menu target:

```bash
echo "Main menu"
echo "---------"
echo "  1) Continue recommended repair"
echo "  2) Guided install / repair"
echo "  3) Detection details"
echo "  4) Manual phase selection"
echo "  5) Configure install inputs"
echo "  6) Show one-liners"
echo "  7) Create GitHub issue / support bundle"
echo "  0) Quit without changes"
```

Manual menu target:

```bash
manual_phase_menu() {
  local choice
  while true; do
    echo
    echo "Choose phase"
    echo "------------"
    echo "  1) Auto-detect again"
    echo "  2) Foundation"
    echo "  3) Bootstrap media"
    echo "  4) Operational repair/promote"
    echo "  5) Runtime config repair"
    echo "  6) Status only"
    echo "  7) Create GitHub issue / support bundle"
    echo "  8) Reset disposable dev lab"
    echo "  9) Return to main menu"
    echo "  0) Quit"
    read -r -p "Select phase: " choice
    case "${choice}" in
      1) run_detect ;;
      2) run_single_phase foundation || true ;;
      3) run_single_phase bootstrap || true ;;
      4) run_single_phase operational || true ;;
      5) run_single_phase runtime-config || true ;;
      6) show_state ;;
      7) create_support_bundle ;;
      8) run_reset || true; show_state ;;
      9) return 0 ;;
      0|q|Q) exit 0 ;;
      *) echo "Invalid selection." ;;
    esac
  done
}
```

Support wrapper:

```bash
create_support_bundle() {
  local args
  run_detect || true
  args=(
    python3 "${STATE_HELPER}" support
    --detection-file "${DETECT_FILE}"
    --failure-file "${FAILURE_FILE}"
    --log-file "${INSTALL_LOG}"
    --output-dir "${SUPPORT_DIR}"
  )
  [[ "${SUPPORT_NO_BUNDLE:-0}" == "1" ]] && args+=(--no-bundle)
  [[ "${SUPPORT_PRINT:-0}" == "1" ]] && args+=(--print)
  "${args[@]}"
}
```

Acceptance for this task:

- Interactive menu shows "Continue recommended repair" as option 1.
- Option 7 is the support helper in both menus.
- Reset Dev Lab is no longer top-level option 6; it is manual menu option 8.
- `configure_interactive` remains reachable from main menu option 5.

### 7. Update Shell Contract Tests

- [ ] Update `test_shell_installer_wraps_pve_init_with_console_actions`.
- [ ] Add tests for read-only actions and menu text.
- [ ] Add test for recommended dry-run with a fixture state.

Existing assertion update:

```python
assert "--action menu|detect|recommended|guided|foundation|bootstrap|operational|runtime-config|status|support|reset-dev-lab" in text
assert "Continue recommended repair" in text
assert "Create GitHub issue / support bundle" in text
assert "manual_phase_menu" in text
assert "run_detect" in text
assert "record_failure" in text
assert "installer_state.py" in text
```

Read-only test:

```python
def test_shell_installer_detect_and_status_are_read_only_dry_runs():
    for action in ("detect", "status"):
        result = subprocess.run(
            ["bash", str(INSTALLER), "--action", action, "--dry-run"],
            check=True,
            capture_output=True,
            text=True,
        )

        assert "--phase" not in result.stdout
        assert "--phase" not in result.stderr
```

Download consent test:

```python
def test_shell_installer_bootstrap_yes_does_not_download_media_without_consent():
    result = subprocess.run(
        ["bash", str(INSTALLER), "--action", "bootstrap", "--yes", "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--phase bootstrap" in result.stdout
    assert "--download-windows" not in result.stdout
    assert "--download-virtio" not in result.stdout

    with_download = subprocess.run(
        ["bash", str(INSTALLER), "--action", "bootstrap", "--yes", "--dry-run", "--download-windows", "--download-virtio"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--download-windows" in with_download.stdout
    assert "--download-virtio" in with_download.stdout
```

Recommended dry-run test:

```python
def test_shell_installer_recommended_dry_run_uses_detected_safe_action(tmp_path):
    state = ROOT / "output" / "setup" / "foundation_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    original = state.read_text(encoding="utf-8") if state.exists() else None
    state.write_text(
        json.dumps(
            {
                "controller_vm_ready": True,
                "controller_runtime_ready": True,
                "windows_iso_ready": True,
                "virtio_iso_ready": True,
                "media_ready": True,
                "promoted_artifacts_ready": False,
            }
        ),
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            ["bash", str(INSTALLER), "--action", "recommended", "--yes", "--dry-run"],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        if original is None:
            state.unlink(missing_ok=True)
        else:
            state.write_text(original, encoding="utf-8")

    assert "--phase operational" in result.stdout
    assert "--phase foundation" not in result.stdout
```

If test mutation of `output/setup/foundation_state.json` is too invasive, set an environment override instead:

```bash
INSTALLER_STATE_FILE="${tmp_path}/foundation_state.json"
```

and make `install-proxmox-ve.sh` honor:

```bash
STATE_FILE="${INSTALLER_STATE_FILE:-${APP_DIR}/output/setup/foundation_state.json}"
```

Acceptance for this task:

- Tests pin the new public menu and CLI surface.
- Tests prove detect/status do not dispatch phase commands.
- Tests prove recommended can translate detection into the next safe phase.

### 8. Add CLI Flags for Support Printing and Bundle Suppression

- [ ] Add `--support-print`.
- [ ] Add `--support-no-bundle`.
- [ ] Add examples to `usage`.
- [ ] Update media option help so VirtIO download is not described as default-enabled.
- [ ] Keep support output local-first; do not post to GitHub.

Parse args additions:

```bash
SUPPORT_PRINT=0
SUPPORT_NO_BUNDLE=0

--support-print) SUPPORT_PRINT=1; shift ;;
--support-no-bundle) SUPPORT_NO_BUNDLE=1; shift ;;
--manual-media) MEDIA_MODE="manual"; ALLOW_WINDOWS_DOWNLOAD=0; shift ;;
--download-windows) MEDIA_MODE="auto"; ALLOW_WINDOWS_DOWNLOAD=1; shift ;;
--windows-iso-url) MEDIA_MODE="url"; ALLOW_WINDOWS_DOWNLOAD=1; WINDOWS_ISO_URL="${2:-}"; shift 2 ;;
--download-virtio) DOWNLOAD_VIRTIO=1; shift ;;
--no-download-virtio) DOWNLOAD_VIRTIO=0; shift ;;
```

The `--manual-media`, `--download-windows`, `--windows-iso-url`, `--download-virtio`, and `--no-download-virtio` cases replace the existing parser branches; do not add duplicate case arms.

Interactive media selection must also set consent:

```bash
case "${choice:-1}" in
  1) MEDIA_MODE="auto"; ALLOW_WINDOWS_DOWNLOAD=1 ;;
  2) MEDIA_MODE="url"; ALLOW_WINDOWS_DOWNLOAD=1; WINDOWS_ISO_URL="$(prompt_value "Official Microsoft direct ISO URL" "${WINDOWS_ISO_URL}")" ;;
  3) MEDIA_MODE="manual"; ALLOW_WINDOWS_DOWNLOAD=0 ;;
esac
```

Usage examples:

```text
  --download-virtio          Download VirtIO ISO.
  --no-download-virtio       Do not download VirtIO ISO.

Create a sanitized issue draft:
  bash scripts/install-proxmox-ve.sh --action support --support-print

Run auto-detection only:
  bash scripts/install-proxmox-ve.sh --action detect

Continue the safest detected repair:
  bash scripts/install-proxmox-ve.sh --action recommended --yes
```

Acceptance for this task:

- `--action support --support-no-bundle --support-print` writes and prints a markdown issue draft.
- Support mode exits non-zero only for helper/runtime failures, not because no prior failure exists.
- A no-failure support snapshot clearly says no failed check was detected.

### 9. Verify No Silent Downloads and No Destructive Auto-Run

- [ ] Add helper tests that missing Windows ISO blocks auto-run when no download flag was selected.
- [ ] Add shell test or string assertion that `run_recommended` checks `safe_to_auto_run`.
- [ ] Confirm `reset-dev-lab` still goes through `confirm_reset`.

Test expectations:

```python
def test_missing_windows_media_without_download_flag_blocks_recommended_auto_run():
    detection = installer_state.classify_state(
        {
            "controller_runtime_ready": True,
            "windows_iso_ready": False,
            "virtio_iso_ready": True,
            "media_ready": False,
        },
        {},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.recommended_action == "bootstrap"
    assert detection.safe_to_auto_run is False
    assert "--download-windows" not in " ".join(detection.planned_commands)


def test_missing_virtio_media_without_download_flag_blocks_recommended_auto_run():
    detection = installer_state.classify_state(
        {
            "controller_runtime_ready": True,
            "windows_iso_ready": True,
            "virtio_iso_ready": False,
            "media_ready": False,
        },
        {},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.recommended_action == "bootstrap"
    assert detection.safe_to_auto_run is False
    assert "--download-virtio" not in " ".join(detection.planned_commands)


def test_reset_dev_lab_is_never_recommended_auto_run():
    text = INSTALLER.read_text(encoding="utf-8")

    assert "confirm_reset" in text
    assert "run_recommended" in text
    assert "reset-dev-lab" not in text[text.index("run_recommended") : text.index("show_failure_footer")]
```

Acceptance for this task:

- No recommended path can select `reset-dev-lab`.
- Missing Windows media cannot start a download unless `MEDIA_MODE=auto` came from `--download-windows` or the operator selected automatic media in the current prompt.
- Missing VirtIO media cannot start a download unless the operator selected VirtIO download in the current prompt or passed `--download-virtio`.
- Direct URL mode must include the operator-supplied URL and must not invent one.

### 10. Run Focused Verification

- [ ] Run the new helper tests.
- [ ] Run the existing first-run script tests.
- [ ] Run shell syntax checks for changed shell scripts.
- [ ] Run Python compile check for the new helper.

Commands:

```bash
cd /tmp/proxmox-installer-resume-spec
python3 -m py_compile autopilot-proxmox/scripts/installer_state.py
bash -n autopilot-proxmox/scripts/install-proxmox-ve.sh
bash -n autopilot-proxmox/scripts/init-proxmox-ve.sh
pytest autopilot-proxmox/tests/test_installer_state_helper.py autopilot-proxmox/tests/test_first_run_init_scripts.py
```

Expected result:

```text
all selected tests pass
```

If local pytest cannot import project dependencies, use the repo venv first:

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_installer_state_helper.py autopilot-proxmox/tests/test_first_run_init_scripts.py
```

Do not claim completion unless the focused verification passes or the exact blocker is documented.

---

## Risk Controls

- Detection and status are read-only by construction: they call only `installer_state.py detect` and `show_state`.
- Bash still delegates all mutations to `init-proxmox-ve.sh --phase ...`.
- Recommended repair requires `safe_to_auto_run=true` and non-low confidence.
- Missing Windows ISO without an explicit media download choice returns a blocked recommendation.
- Support bundle generation is local-only and fail-closed.
- Reset Dev Lab remains confirmation-gated and is not a recommendation target.
- Name-only and VMID-only discoveries cannot be high confidence.

## Implementation Notes

- Prefer environment overrides for tests:

```bash
STATE_FILE="${INSTALLER_STATE_FILE:-${APP_DIR}/output/setup/foundation_state.json}"
DETECT_FILE="${INSTALLER_DETECT_FILE:-${APP_DIR}/output/setup/installer_detect.json}"
FAILURE_FILE="${INSTALLER_FAILURE_FILE:-${APP_DIR}/output/setup/install-last-failure.json}"
INSTALL_LOG="${INSTALLER_LOG_FILE:-${APP_DIR}/output/setup/install.log}"
SUPPORT_DIR="${INSTALLER_SUPPORT_DIR:-${APP_DIR}/output/support}"
```

- Keep detection summaries compact in the console; detailed JSON can live in `installer_detect.json`.
- Do not expand the first pass into full live `qm` probing unless the helper remains testable. The initial version may classify from state and explicit probe JSON, then Bash can add live probes in a later pass.
- If live probes are added now, put them behind read-only commands and timeouts. Never run `qm start`, `qm stop`, `pvesm alloc`, `pveum acl modify`, `rsync`, `curl -X POST`, or artifact promotion from detection.

## Done Definition

- The new CLI actions exist and are documented in usage.
- First interactive launch performs detection before showing the menu.
- Main menu and manual phase menu match the approved support-centered flow.
- Recommended repair works for high-confidence safe states and refuses conflicts.
- Missing Windows media does not silently start a Windows ISO download.
- Phase failures write sanitized failure context and print a support-friendly footer.
- Support option 7 writes a sanitized GitHub issue draft and optional support bundle.
- Redaction tests prove token/password/key/header patterns are removed.
- Focused pytest and syntax checks pass.
