"""Manifest assembly for /winpe/manifest/<vm-uuid> (spec §6).

Plan 2 ships a stub renderer: the manifest has fixed step types in a fixed
order, and only the computer name is per-VM-substituted. Plan 3 replaces
this with sequence_compiler + unattend_renderer integration that produces
real per-VM artifacts.

Pure function: given a WinpeTarget and an ArtifactStore, return the
manifest dict. Rendered per-VM blobs (unattend.xml) get cached into the
store via cache_blob; the manifest references them by sha. PE fetches
each blob via /winpe/content/<sha>.
"""

from __future__ import annotations

from web.artifact_sidecar import ArtifactKind
from web.artifact_store import ArtifactStore
from web.winpe_targets_db import WinpeTarget


class RendererError(RuntimeError):
    pass


_UNATTEND_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <ComputerName>__COMPUTER_NAME__</ComputerName>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
    </component>
  </settings>
</unattend>
"""


def _render_unattend(params: dict) -> bytes:
    computer_name = params.get("computer_name", "AUTOPILOT-VM")
    return _UNATTEND_TEMPLATE.replace("__COMPUTER_NAME__", computer_name).encode("utf-8")


def render_manifest(target: WinpeTarget, store: ArtifactStore) -> dict:
    """Assemble a minimal manifest for the target. Caches rendered per-VM blobs."""
    install_record = store.lookup(target.install_wim_sha)
    if install_record is None:
        raise RendererError(
            f"target's install.wim sha {target.install_wim_sha} is not registered "
            f"in the artifact store"
        )

    unattend_xml = _render_unattend(target.params)
    unattend_record = store.cache_blob(
        unattend_xml,
        kind=ArtifactKind.UNATTEND_XML,
        extension="xml",
    )

    computer_name = target.params.get("computer_name", "AUTOPILOT-VM")

    return {
        "version": 1,
        "vmUuid": target.vm_uuid,
        "onError": "halt",
        "steps": [
            {"id": "p1", "type": "partition", "layout": "uefi-standard"},
            {
                "id": "a1",
                "type": "apply-wim",
                "content": {"sha256": install_record.sha256, "size": install_record.size},
            },
            {
                "id": "u1",
                "type": "write-unattend",
                "content": {"sha256": unattend_record.sha256, "size": unattend_record.size},
                "target": "W:\\Windows\\Panther\\unattend.xml",
            },
            {
                "id": "r1",
                "type": "set-registry",
                "hive": "SYSTEM",
                "target": "W:",
                "keys": [
                    {
                        "path": "Setup",
                        "name": "ComputerName",
                        "type": "REG_SZ",
                        "value": computer_name,
                    },
                ],
            },
            {"id": "b1", "type": "bcdboot", "windows": "W:", "esp": "S:"},
            {"id": "rb", "type": "reboot"},
        ],
    }
