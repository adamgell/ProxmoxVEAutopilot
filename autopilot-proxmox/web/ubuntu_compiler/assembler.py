"""Sequence assembler: merge StepOutputs into autoinstall + per-clone cloud-init."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .registry import compile_step
from .types import StepOutput, UbuntuCompileError

_yaml = YAML()
_yaml.default_flow_style = False
_yaml.indent(mapping=2, sequence=4, offset=2)

_BASE_PATH = Path(__file__).resolve().parents[2] / "files" / "ubuntu_autoinstall_base.yaml"


def _load_base() -> dict[str, Any]:
    # Load as a plain dict via a fresh YAML instance (typ=safe) so we don't
    # carry ruamel's round-trip tokens into the merged output.
    safe = YAML(typ="safe")
    with _BASE_PATH.open("r", encoding="utf-8") as fh:
        return safe.load(fh) or {}


def _merge_into(ai_root: dict[str, Any], contribution: dict[str, Any]) -> None:
    """Shallow-merge `contribution` into `ai_root`. Lists concatenate; dicts
    shallow-merge at the first level; scalars overwrite."""
    LIST_KEYS = {"packages", "snaps", "late-commands"}
    DICT_KEYS = {"keyboard", "storage", "ssh", "user-data"}
    for k, v in contribution.items():
        if k in LIST_KEYS:
            ai_root.setdefault(k, []).extend(v)
        elif k in DICT_KEYS and isinstance(v, dict):
            existing = ai_root.setdefault(k, {})
            for kk, vv in v.items():
                # Nested list concat inside user-data (e.g. "users")
                if kk == "users" and isinstance(vv, list):
                    existing.setdefault("users", []).extend(vv)
                else:
                    existing[kk] = vv
        else:
            ai_root[k] = v


def _dump(doc: dict[str, Any], *, cloud_config_header: bool) -> str:
    buf = io.StringIO()
    if cloud_config_header:
        buf.write("#cloud-config\n")
    _yaml.dump(doc, buf)
    return buf.getvalue()


def compile_sequence(
    *,
    steps: list[dict[str, Any]],
    credentials: dict[int, dict[str, Any]],
    instance_id: str,
    hostname: str,
) -> tuple[str, str, str, str]:
    """Compile a sequence into (user-data, meta-data, firstboot-user-data,
    firstboot-meta-data) YAML documents for a NoCloud seed.

    `steps` is a list of dicts with keys {step_type, params, enabled?}. Disabled
    steps are skipped. `credentials` is {id: decrypted_payload_dict}.
    """
    base = _load_base()
    autoinstall: dict[str, Any] = dict(base.get("autoinstall", {}))
    # Append-order lists start empty.
    autoinstall.setdefault("late-commands", [])

    firstboot_runcmd: list[str] = []

    for step in steps:
        if step.get("enabled", True) is False:
            continue
        out: StepOutput = compile_step(
            step["step_type"], step.get("params", {}), credentials
        )
        if out.autoinstall_body:
            _merge_into(autoinstall, out.autoinstall_body)
        if out.late_commands:
            autoinstall["late-commands"].extend(out.late_commands)
        if out.firstboot_runcmd:
            firstboot_runcmd.extend(out.firstboot_runcmd)

    # Drop empty late-commands (keeps the compiled file tidy).
    if not autoinstall.get("late-commands"):
        autoinstall.pop("late-commands", None)

    user_data = _dump({"autoinstall": autoinstall}, cloud_config_header=True)
    meta_data = _dump({"instance-id": instance_id}, cloud_config_header=False)

    firstboot: dict[str, Any] = {"hostname": hostname}
    if firstboot_runcmd:
        firstboot["runcmd"] = firstboot_runcmd
    firstboot_user_data = _dump(firstboot, cloud_config_header=True)
    firstboot_meta_data = _dump(
        {"instance-id": f"firstboot-{instance_id}"}, cloud_config_header=False
    )

    return user_data, meta_data, firstboot_user_data, firstboot_meta_data
