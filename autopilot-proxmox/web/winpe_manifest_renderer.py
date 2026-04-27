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


_UNATTEND_TEMPLATE = r"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="specialize">
    <component name="Microsoft-Windows-International-Core"
               processorArchitecture="__ARCH__"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <InputLocale>__INPUT_LOCALE__</InputLocale>
      <SystemLocale>__LOCALE__</SystemLocale>
      <UILanguage>__LOCALE__</UILanguage>
      <UserLocale>__LOCALE__</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup"
               processorArchitecture="__ARCH__"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <ComputerName>__COMPUTER_NAME__</ComputerName>
      <TimeZone>__TIMEZONE__</TimeZone>
    </component>
    <component name="Microsoft-Windows-Deployment"
               processorArchitecture="__ARCH__"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>powershell.exe -NoProfile -Command "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; Set-Service sshd -StartupType Automatic; Start-Service sshd"</Path>
          <Description>Install and start OpenSSH Server</Description>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="auditUser">
    <component name="Microsoft-Windows-Deployment"
               processorArchitecture="__ARCH__"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\autopilot\Collect-HardwareHash.ps1</Path>
          <Description>Collect Autopilot hardware hash</Description>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Path>C:\Windows\System32\Sysprep\sysprep.exe /oobe /shutdown</Path>
          <Description>Reseal to OOBE for Autopilot enrollment</Description>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core"
               processorArchitecture="__ARCH__"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <InputLocale>__INPUT_LOCALE__</InputLocale>
      <SystemLocale>__LOCALE__</SystemLocale>
      <UILanguage>__LOCALE__</UILanguage>
      <UserLocale>__LOCALE__</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup"
               processorArchitecture="__ARCH__"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
    </component>
    <component name="Microsoft-Windows-Deployment"
               processorArchitecture="__ARCH__"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <Reseal>
        <Mode>Audit</Mode>
      </Reseal>
    </component>
  </settings>
</unattend>
"""


def _render_unattend(params: dict) -> bytes:
    computer_name = params.get("computer_name", "AUTOPILOT-VM")
    arch = params.get("architecture", "arm64")
    locale = params.get("locale", "en-US")
    input_locale = params.get("input_locale", locale)
    timezone = params.get("timezone", "Pacific Standard Time")
    xml = (_UNATTEND_TEMPLATE
           .replace("__COMPUTER_NAME__", computer_name)
           .replace("__ARCH__", arch)
           .replace("__LOCALE__", locale)
           .replace("__INPUT_LOCALE__", input_locale)
           .replace("__TIMEZONE__", timezone))
    return xml.encode("utf-8")


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
