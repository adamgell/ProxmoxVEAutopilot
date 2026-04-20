"""Sequence assembler: merge StepOutputs into cloud-init user-data + per-clone cloud-init.

We target plain cloud-init on an Ubuntu cloud image. The compiled output is a
top-level `#cloud-config` document (no `autoinstall:` wrapper) — cloud-init
applies it on first boot of the cloned/booted VM.
"""
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

_BASE_PATH = Path(__file__).resolve().parents[2] / "files" / "ubuntu_cloudinit_base.yaml"


def _load_base() -> dict[str, Any]:
    # Load as a plain dict via a fresh YAML instance (typ=safe) so we don't
    # carry ruamel's round-trip tokens into the merged output. Base file may
    # be empty (just the `#cloud-config` header), in which case we start with {}.
    safe = YAML(typ="safe")
    with _BASE_PATH.open("r", encoding="utf-8") as fh:
        return safe.load(fh) or {}


def _merge_into(cc: dict[str, Any], contribution: dict[str, Any]) -> None:
    """Shallow-merge `contribution` into the top-level cloud-config dict `cc`.

    - LIST_KEYS concatenate (step order preserved).
    - `snap` is a dict whose `commands` list concatenates.
    - Everything else: scalars overwrite.
    """
    LIST_KEYS = {"packages", "runcmd", "users"}
    for k, v in contribution.items():
        if k in LIST_KEYS:
            cc.setdefault(k, []).extend(v)
        elif k == "snap" and isinstance(v, dict):
            existing = cc.setdefault("snap", {})
            if "commands" in v:
                existing.setdefault("commands", []).extend(v["commands"])
            # Forward any other snap-module keys literally.
            for kk, vv in v.items():
                if kk != "commands":
                    existing[kk] = vv
        else:
            cc[k] = v


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
    cc: dict[str, Any] = dict(base)
    # Append-order lists start empty.
    cc.setdefault("runcmd", [])

    firstboot_runcmd: list[str] = []

    for step in steps:
        if step.get("enabled", True) is False:
            continue
        out: StepOutput = compile_step(
            step["step_type"], step.get("params", {}), credentials
        )
        if out.cloud_config:
            _merge_into(cc, out.cloud_config)
        if out.runcmd:
            cc["runcmd"].extend(out.runcmd)
        if out.firstboot_runcmd:
            firstboot_runcmd.extend(out.firstboot_runcmd)

    # Drop empty runcmd (keeps the compiled file tidy).
    if not cc.get("runcmd"):
        cc.pop("runcmd", None)

    user_data = _dump(cc, cloud_config_header=True)
    meta_data = _dump({"instance-id": instance_id}, cloud_config_header=False)

    # Per-clone cloud-init: every clone runs an idempotent install of
    # qemu-guest-agent on first boot. The cloud-image template should already
    # have it, but this is cheap insurance (dpkg -s is a no-op if installed).
    firstboot: dict[str, Any] = {"hostname": hostname}
    agent_runcmd = [
        "dpkg -s qemu-guest-agent >/dev/null 2>&1 || "
        "(apt-get update && apt-get install -y qemu-guest-agent)",
        "systemctl enable --now qemu-guest-agent",
    ]
    firstboot["runcmd"] = agent_runcmd + firstboot_runcmd
    firstboot_user_data = _dump(firstboot, cloud_config_header=True)
    firstboot_meta_data = _dump(
        {"instance-id": f"firstboot-{instance_id}"}, cloud_config_header=False
    )

    return user_data, meta_data, firstboot_user_data, firstboot_meta_data
