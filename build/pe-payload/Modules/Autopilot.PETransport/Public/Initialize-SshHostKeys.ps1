function Initialize-SshHostKeys {
    <#
    .SYNOPSIS
        Regenerate SSH host keys at PE boot to dodge Plan 1 KNOWN-ISSUES #2
        (host keys baked into the WIM at build time inherit BUILTIN\Users
        read access). Fresh keys generated at runtime get the default ACL
        of files created by SYSTEM/Administrator with no Users access.

        After regeneration, attempts to start sshd. If sshd starts cleanly
        the function returns; if it fails, throws so the bootstrap drops
        to the debug shell.

    .DESCRIPTION
        No-ops if SshKeygen path doesn't exist (PE was built without OpenSSH).
    #>
    [CmdletBinding()]
    param(
        [string] $SshDir = 'X:\ProgramData\ssh',
        [string] $SshKeygen = 'X:\Program Files\OpenSSH\ssh-keygen.exe'
    )

    if (-not (Test-Path $SshKeygen)) {
        Write-Host "Initialize-SshHostKeys: $SshKeygen not present; skipping (PE built without OpenSSH)"
        return
    }

    if (-not (Test-Path $SshDir)) {
        New-Item -ItemType Directory -Path $SshDir -Force | Out-Null
    }

    foreach ($keyType in @('rsa','ecdsa','ed25519')) {
        $keyPath = [System.IO.Path]::Combine($SshDir, "ssh_host_${keyType}_key")
        if (Test-Path $keyPath) {
            Remove-Item $keyPath -Force -ErrorAction SilentlyContinue
            Remove-Item "${keyPath}.pub" -Force -ErrorAction SilentlyContinue
        }
    }

    Invoke-Expression "& `"$SshKeygen`" -A -f `"$SshDir`""

    Start-Service sshd -ErrorAction Stop
    $svc = Get-Service sshd
    if ($svc.Status -ne 'Running') {
        throw "Initialize-SshHostKeys: sshd did not enter Running state (Status=$($svc.Status))"
    }
    Write-Host "Initialize-SshHostKeys: regenerated keys in $SshDir; sshd Running"
}
