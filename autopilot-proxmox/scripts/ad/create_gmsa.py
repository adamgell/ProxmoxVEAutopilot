"""Idempotent: create the ``svc-apmon`` gMSA + delegate read on
``OU=WorkspaceLabs`` to it.

Runs from inside the autopilot container. Uses the current Kerberos
ticket (``kinit adam_admin`` first) to authenticate via WinRM to the
DC — the DC's QEMU guest agent has been flaky, so WinRM is our
out-of-band channel for PowerShell on the DC.

Usage::

    kinit adam_admin@HOME.GELL.ONE
    python3 scripts/ad/create_gmsa.py

sAMAccountName is constrained to 15 characters (NetBIOS legacy).
``svc-apmon$`` is 10 chars; DisplayName keeps the readable
``svc-autopilot-monitor`` so the directory is still self-documenting.
"""
import sys
from pypsrp.client import Client

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory

$rootKey = Get-KdsRootKey -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $rootKey) {
    Write-Output 'creating KDS root key (effective -10h)'
    $rootKey = Add-KdsRootKey -EffectiveTime ((Get-Date).AddHours(-10))
}
Write-Output "KDS root key: $($rootKey.KeyId)"

$gmsaName = 'svc-apmon'
$existing = Get-ADServiceAccount -Filter "Name -eq '$gmsaName'" -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Output "creating gMSA $gmsaName"
    $dcGroup = Get-ADGroup -Identity 'Domain Controllers'
    # DisplayName stays the long, readable name; SamAccountName (derived
    # from -Name, capped at 15 chars) is the one AD actually uses.
    New-ADServiceAccount -Name $gmsaName `
        -DisplayName 'svc-autopilot-monitor' `
        -Description 'Autopilot monitoring — read-only on OU=WorkspaceLabs' `
        -DNSHostName "$gmsaName.home.gell.one" `
        -PrincipalsAllowedToRetrieveManagedPassword $dcGroup `
        -KerberosEncryptionType AES256 `
        -Enabled $true
    $existing = Get-ADServiceAccount -Identity $gmsaName
}
Write-Output "gMSA DN: $($existing.DistinguishedName)"

$ouDN = 'OU=WorkspaceLabs,DC=home,DC=gell,DC=one'
$principal = "HOME\$gmsaName`$"
Write-Output "granting GR on $ouDN to $principal"
$dsaclsOut = & dsacls $ouDN /G "${principal}:GR" /I:T 2>&1 | Out-String
Write-Output "dsacls exit: $LASTEXITCODE"

$pw = Get-ADServiceAccount -Identity $gmsaName -Properties 'msDS-ManagedPassword' -ErrorAction SilentlyContinue
if ($pw -and $pw.'msDS-ManagedPassword') {
    Write-Output "managed-password blob: $($pw.'msDS-ManagedPassword'.Length) bytes"
} else {
    Write-Output 'WARN: managed-password blob empty'
}
Write-Output 'done'
"""

try:
    client = Client(
        "dns.home.gell.one",
        auth="kerberos",
        ssl=False,
        encryption="always",
    )
    stdout, streams, had_errors = client.execute_ps(SCRIPT)
    print("=== stdout ===")
    print(stdout or "(empty)")
    print(f"=== had_errors: {had_errors} ===")
    if had_errors and hasattr(streams, "error"):
        for err in streams.error:
            print("ERR:", err)
    sys.exit(1 if had_errors else 0)
except Exception as e:
    print(f"WinRM FAIL: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(2)
