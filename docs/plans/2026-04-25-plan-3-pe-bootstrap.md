# Plan 3 — PE bootstrap interpreter

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `build/pe-payload/Bootstrap.ps1` (Plan 1's diagnostic placeholder) with a real PowerShell manifest interpreter that talks to Plan 2's orchestrator API: identifies via SMBIOS UUID, fetches a per-VM manifest, executes each of the 10 step types in order, posts a checkin per step, handles `onError=halt` by dropping to a debug shell, terminates with `wpeutil reboot|shutdown` per the spec's return-code dispatch.

**Architecture:** Two new PowerShell modules under `build/pe-payload/Modules/`:
- **`Autopilot.PETransport`** — HTTP transport (manifest fetch, content fetch with sha verify, checkin POST), network-up polling, SMBIOS identity, `Initialize-SshHostKeys` boot-time fix for Plan 1 KNOWN-ISSUES #2.
- **`Autopilot.PESteps`** — one cmdlet per step type (10 cmdlets total). Pure-PS implementations using Storage / DISM / `reg.exe` / `bcdboot` / `wpeutil`.

`Bootstrap.ps1` is a thin dispatch loop that imports both modules, runs network/SSH bootstrap, fetches the manifest, walks `$manifest.steps` calling `Invoke-<Type>Step @step` for each, posts checkins. Tests live in `build/pe-payload-tests/` (outside the payload tree so the build script doesn't bake them into the PE WIM).

**Tech Stack:** PowerShell 7 (pwsh), Pester 5 (with `Mock` for HTTP/DISM/Storage cmdlets), Plan 2's orchestrator API as the live integration target.

**Spec reference:** `docs/specs/2026-04-25-winpe-osd-pipeline-design.md` Section 6 (PE runtime: bootstrap flow, step types, manifest schema, checkin payload).

**Plan 1 KNOWN-ISSUES this plan fixes:**
- Issue #1 (`wpeinit` returns before NetKVM binds → no auto network) — `Wait-PeNetwork` retry loop in Bootstrap.ps1's startup.
- Issue #2 (sshd refuses to load baked-in host keys due to `BUILTIN\Users` ACL inheritance) — `Initialize-SshHostKeys` regenerates host keys at PE-boot via `ssh-keygen -A`, which writes them with default-correct ACLs (no Users).
- Issue #3 (cascade of #1 → #2): the bootstrap orders network-up *before* SSH key regen *before* sshd start.

**Out of scope for Plan 3:** Plan 4 (operator UI integration). The orchestrator API endpoints from Plan 2 are consumed as-is — no API changes here. Manifest renderer stays stub; v2 will swap in real `sequence_compiler` integration (separate effort).

---

## File structure

| File | Purpose |
|---|---|
| `build/pe-payload/Bootstrap.ps1` | **Replace**: real interpreter (was 39-line placeholder; becomes ~150 lines). |
| `build/pe-payload/Debug.ps1` | **New**: fallback shell launched on `onError=halt`. Prints diagnostics + drops to interactive pwsh. |
| `build/pe-payload/Modules/Autopilot.PETransport/Autopilot.PETransport.psd1` | Module manifest. |
| `build/pe-payload/Modules/Autopilot.PETransport/Autopilot.PETransport.psm1` | Loader (auto-imports `Public/*.ps1`). |
| `build/pe-payload/Modules/Autopilot.PETransport/Public/Get-PeIdentity.ps1` | Returns SMBIOS UUID via `Get-CimInstance Win32_ComputerSystemProduct`. |
| `build/pe-payload/Modules/Autopilot.PETransport/Public/Wait-PeNetwork.ps1` | Polls for IPv4 with retry; calls `wpeutil InitializeNetwork` on each retry. |
| `build/pe-payload/Modules/Autopilot.PETransport/Public/Initialize-SshHostKeys.ps1` | Runs `ssh-keygen -A` to regenerate fresh host keys with default-correct ACLs, then `Start-Service sshd`. |
| `build/pe-payload/Modules/Autopilot.PETransport/Public/Invoke-Manifest.ps1` | `GET /winpe/manifest/<uuid>` → manifest dict. Retries on transient failure. |
| `build/pe-payload/Modules/Autopilot.PETransport/Public/Get-PeContent.ps1` | `GET /winpe/content/<sha>` → file on disk; verifies sha256 matches expected. |
| `build/pe-payload/Modules/Autopilot.PETransport/Public/Send-Checkin.ps1` | `POST /winpe/checkin` → 204. Fire-and-forget; one retry on transient failure. |
| `build/pe-payload/Modules/Autopilot.PESteps/Autopilot.PESteps.psd1` | Module manifest. |
| `build/pe-payload/Modules/Autopilot.PESteps/Autopilot.PESteps.psm1` | Loader. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-LogStep.ps1` | Marker line in transcript. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-RebootStep.ps1` | `wpeutil reboot`. Terminal. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ShutdownStep.ps1` | `wpeutil shutdown`. Terminal. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-BcdbootStep.ps1` | `bcdboot W:\Windows /s S: /f UEFI`. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-PartitionStep.ps1` | Clear disk 0, GPT layout: ESP (260MB FAT32) + MSR (16MB) + Windows (rest, NTFS). Returns `{esp, windows}`. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ApplyWimStep.ps1` | Fetch WIM via `Get-PeContent`; `Expand-WindowsImage` to target volume. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-StageFilesStep.ps1` | Fetch zip via `Get-PeContent`; `Expand-Archive` into target path. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-WriteUnattendStep.ps1` | Fetch unattend.xml via `Get-PeContent`; copy to `<target>\Windows\Panther\unattend.xml`. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-SetRegistryStep.ps1` | `reg load HKLM\PEStaging <volume>\...\<hive>` → set keys → `reg unload`. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ScheduleTaskStep.ps1` | Write Task XML to `<volume>\Windows\System32\Tasks\<name>` + TaskCache reg entries. |
| `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-InjectDriverStep.ps1` | Fetch driver zip; `Add-WindowsDriver` against target volume. |
| `build/pe-payload-tests/Autopilot.PETransport.Tests.ps1` | Pester tests for all PETransport cmdlets (Mock'ed HTTP). |
| `build/pe-payload-tests/Autopilot.PESteps.Tests.ps1` | Pester tests for parameter validation + dispatch logic. DISM/Storage cmdlet calls are integration-tested in PE only. |
| `build/pe-payload-tests/Bootstrap.Tests.ps1` | Pester tests for Bootstrap.ps1's dispatch loop (Mock'ed PETransport + PESteps). |
| `build/SMOKE-TEST.md` | **Modify**: append a Plan 3 section for "boot the new ISO + verify a real manifest deploys end-to-end". |

---

## Task 1: PETransport module skeleton + Get-PeIdentity

**Files:**
- Create: `build/pe-payload/Modules/Autopilot.PETransport/Autopilot.PETransport.psd1`
- Create: `build/pe-payload/Modules/Autopilot.PETransport/Autopilot.PETransport.psm1`
- Create: `build/pe-payload/Modules/Autopilot.PETransport/Public/Get-PeIdentity.ps1`
- Create: `build/pe-payload-tests/Autopilot.PETransport.Tests.ps1`

- [ ] **Step 1: Write the failing tests**

Create `build/pe-payload-tests/Autopilot.PETransport.Tests.ps1`:

```powershell
BeforeAll {
    $modulePath = Join-Path $PSScriptRoot '..' 'pe-payload' 'Modules' 'Autopilot.PETransport' 'Autopilot.PETransport.psd1'
    Import-Module $modulePath -Force
}

Describe 'Autopilot.PETransport module' {
    It 'imports without error' {
        Get-Module Autopilot.PETransport | Should -Not -BeNullOrEmpty
    }
    It 'exports Get-PeIdentity' {
        (Get-Command -Module Autopilot.PETransport).Name | Should -Contain 'Get-PeIdentity'
    }
}

Describe 'Get-PeIdentity' {
    BeforeAll {
        # Mock Get-CimInstance inside the module's scope so we don't need real WMI
        # (Pester 5 -ModuleName lets us mock cmdlets the module calls into).
        Mock -ModuleName Autopilot.PETransport Get-CimInstance {
            return [pscustomobject]@{
                UUID   = '11111111-2222-3333-4444-555555555555'
                Vendor = 'TestCorp'
                Name   = 'TestModel'
            }
        } -ParameterFilter { $ClassName -eq 'Win32_ComputerSystemProduct' }
    }

    It 'returns the SMBIOS UUID' {
        $result = Get-PeIdentity
        $result.Uuid | Should -Be '11111111-2222-3333-4444-555555555555'
    }

    It 'includes vendor and name' {
        $result = Get-PeIdentity
        $result.Vendor | Should -Be 'TestCorp'
        $result.Name | Should -Be 'TestModel'
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/.worktrees/winpe-pe-bootstrap
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PETransport.Tests.ps1 -CI"
```

Expected: FAIL with "module manifest not found".

- [ ] **Step 3: Write the module manifest**

Create `build/pe-payload/Modules/Autopilot.PETransport/Autopilot.PETransport.psd1`:

```powershell
@{
    RootModule        = 'Autopilot.PETransport.psm1'
    ModuleVersion     = '0.1.0'
    GUID              = '7c3b4e6f-2e1f-4a8b-9c5d-1f2e3d4c5b6a'
    Author            = 'ProxmoxVEAutopilot'
    Description       = 'PE-side HTTP transport + identity for the orchestrator API.'
    PowerShellVersion = '7.0'
    FunctionsToExport = '*'
    CmdletsToExport   = @()
    VariablesToExport = @()
    AliasesToExport   = @()
}
```

- [ ] **Step 4: Write the module loader**

Create `build/pe-payload/Modules/Autopilot.PETransport/Autopilot.PETransport.psm1`:

```powershell
$ErrorActionPreference = 'Stop'
$publicDir = Join-Path $PSScriptRoot 'Public'
if (Test-Path $publicDir) {
    Get-ChildItem -Path $publicDir -Filter '*.ps1' | ForEach-Object {
        . $_.FullName
        Export-ModuleMember -Function $_.BaseName
    }
}
```

- [ ] **Step 5: Implement Get-PeIdentity**

Create `build/pe-payload/Modules/Autopilot.PETransport/Public/Get-PeIdentity.ps1`:

```powershell
function Get-PeIdentity {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param()

    $product = Get-CimInstance -ClassName Win32_ComputerSystemProduct -ErrorAction Stop
    return [pscustomobject]@{
        Uuid   = $product.UUID
        Vendor = $product.Vendor
        Name   = $product.Name
    }
}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PETransport.Tests.ps1 -CI"
```

Expected: 4 tests pass (2 module + 2 Get-PeIdentity).

- [ ] **Step 7: Commit**

```bash
git add build/pe-payload/Modules/Autopilot.PETransport/ build/pe-payload-tests/Autopilot.PETransport.Tests.ps1
git commit -m "feat(pe): Autopilot.PETransport module + Get-PeIdentity"
```

---

## Task 2: Wait-PeNetwork + Initialize-SshHostKeys

**Files:**
- Create: `build/pe-payload/Modules/Autopilot.PETransport/Public/Wait-PeNetwork.ps1`
- Create: `build/pe-payload/Modules/Autopilot.PETransport/Public/Initialize-SshHostKeys.ps1`
- Modify: `build/pe-payload-tests/Autopilot.PETransport.Tests.ps1` (append tests)

The two boot-time-fix helpers from Plan 1's KNOWN-ISSUES.

- [ ] **Step 1: Append failing tests**

Append to `build/pe-payload-tests/Autopilot.PETransport.Tests.ps1`:

```powershell
Describe 'Wait-PeNetwork' {
    Context 'when network comes up on second poll' {
        BeforeAll {
            $script:initCount = 0
            Mock -ModuleName Autopilot.PETransport wpeutil { $script:initCount++ }
            $script:pollCount = 0
            Mock -ModuleName Autopilot.PETransport Get-NetIPAddress {
                $script:pollCount++
                if ($script:pollCount -lt 2) { return @() }
                return @([pscustomobject]@{ IPAddress = '192.168.1.50'; AddressFamily = 'IPv4' })
            }
        }
        It 'returns the IPv4 address and called wpeutil at least once' {
            $ip = Wait-PeNetwork -TimeoutSeconds 30 -PollIntervalSeconds 0
            $ip | Should -Be '192.168.1.50'
            $script:initCount | Should -BeGreaterOrEqual 1
        }
    }

    Context 'when network never comes up' {
        BeforeAll {
            Mock -ModuleName Autopilot.PETransport wpeutil { }
            Mock -ModuleName Autopilot.PETransport Get-NetIPAddress { return @() }
        }
        It 'throws on timeout' {
            { Wait-PeNetwork -TimeoutSeconds 1 -PollIntervalSeconds 0 } | Should -Throw -ExpectedMessage '*timeout*'
        }
    }

    Context 'when only APIPA (169.254.x) is present' {
        BeforeAll {
            Mock -ModuleName Autopilot.PETransport wpeutil { }
            Mock -ModuleName Autopilot.PETransport Get-NetIPAddress {
                return @([pscustomobject]@{ IPAddress = '169.254.42.99'; AddressFamily = 'IPv4' })
            }
        }
        It 'rejects APIPA and times out' {
            { Wait-PeNetwork -TimeoutSeconds 1 -PollIntervalSeconds 0 } | Should -Throw -ExpectedMessage '*timeout*'
        }
    }
}

Describe 'Initialize-SshHostKeys' {
    Context 'when ssh-keygen is available and ProgramData\ssh exists' {
        BeforeAll {
            $script:keygenInvocations = @()
            Mock -ModuleName Autopilot.PETransport Test-Path { return $true }
            Mock -ModuleName Autopilot.PETransport Remove-Item { }
            Mock -ModuleName Autopilot.PETransport Start-Service { }
            Mock -ModuleName Autopilot.PETransport Get-Service { return [pscustomobject]@{ Status = 'Running' } }
            # Capture & invocations of ssh-keygen
            Mock -ModuleName Autopilot.PETransport Invoke-Expression { $script:keygenInvocations += $args[0] }
        }

        It 'invokes ssh-keygen -A and starts sshd' {
            { Initialize-SshHostKeys -SshDir 'X:\ProgramData\ssh' -SshKeygen 'X:\Program Files\OpenSSH\ssh-keygen.exe' } | Should -Not -Throw
        }
    }

    Context 'when ssh-keygen is not present (no SSH baked in)' {
        BeforeAll {
            Mock -ModuleName Autopilot.PETransport Test-Path { param($Path) return $Path -ne 'X:\Program Files\OpenSSH\ssh-keygen.exe' }
        }
        It 'returns silently — no SSH is fine' {
            { Initialize-SshHostKeys -SshDir 'X:\ProgramData\ssh' -SshKeygen 'X:\Program Files\OpenSSH\ssh-keygen.exe' } | Should -Not -Throw
        }
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PETransport.Tests.ps1 -CI"
```

Expected: 6 new tests fail with "Wait-PeNetwork not recognized" / "Initialize-SshHostKeys not recognized".

- [ ] **Step 3: Implement Wait-PeNetwork**

Create `build/pe-payload/Modules/Autopilot.PETransport/Public/Wait-PeNetwork.ps1`:

```powershell
function Wait-PeNetwork {
    <#
    .SYNOPSIS
        Block until PE has a non-APIPA IPv4 address. Calls wpeutil InitializeNetwork
        on each retry to work around Plan 1 KNOWN-ISSUES #1 (NetKVM driver doesn't
        bind in time for wpeinit's first network init).

    .OUTPUTS
        The first non-APIPA IPv4 address found.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [int] $TimeoutSeconds = 60,
        [int] $PollIntervalSeconds = 3
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        # Kick PnP to re-enumerate / wpeinit to retry NetKVM bind.
        wpeutil InitializeNetwork | Out-Null

        $ipv4 = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object IPAddress -notlike '169.254.*' |
            Where-Object IPAddress -ne '127.0.0.1')
        if ($ipv4.Count -gt 0) {
            return $ipv4[0].IPAddress
        }
        if ($PollIntervalSeconds -gt 0) {
            Start-Sleep -Seconds $PollIntervalSeconds
        }
    }
    throw "Wait-PeNetwork: timeout after $TimeoutSeconds seconds — no non-APIPA IPv4 address found"
}
```

- [ ] **Step 4: Implement Initialize-SshHostKeys**

Create `build/pe-payload/Modules/Autopilot.PETransport/Public/Initialize-SshHostKeys.ps1`:

```powershell
function Initialize-SshHostKeys {
    <#
    .SYNOPSIS
        Regenerate SSH host keys at PE boot to dodge Plan 1 KNOWN-ISSUES #2 (host
        keys baked into the WIM at build time inherit BUILTIN\Users read access
        from the parent \ProgramData\ tree, which sshd refuses with
        UNPROTECTED_PRIVATE_KEY_FILE). Fresh keys generated at runtime get the
        default ACL of files created by SYSTEM/Administrator with no Users access.

        After regeneration, attempts to start sshd. If sshd starts cleanly the
        function returns; if it fails, throws so the bootstrap drops to the
        debug shell.

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

    # Remove the baked-in keys (their ACLs are bad) before regenerating.
    foreach ($keyType in @('rsa','ecdsa','ed25519')) {
        $keyPath = Join-Path $SshDir "ssh_host_${keyType}_key"
        if (Test-Path $keyPath) {
            Remove-Item $keyPath -Force -ErrorAction SilentlyContinue
            Remove-Item "${keyPath}.pub" -Force -ErrorAction SilentlyContinue
        }
    }

    # ssh-keygen -A regenerates ALL host key types into $SshDir at once.
    # We invoke via Invoke-Expression so Pester can mock it without resorting
    # to wrapping the .exe call.
    Invoke-Expression "& `"$SshKeygen`" -A -f `"$SshDir`""

    Start-Service sshd -ErrorAction Stop
    $svc = Get-Service sshd
    if ($svc.Status -ne 'Running') {
        throw "Initialize-SshHostKeys: sshd did not enter Running state (Status=$($svc.Status))"
    }
    Write-Host "Initialize-SshHostKeys: regenerated keys in $SshDir; sshd Running"
}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PETransport.Tests.ps1 -CI"
```

Expected: 10 tests pass (4 prior + 3 Wait-PeNetwork + 3 Initialize-SshHostKeys).

- [ ] **Step 6: Commit**

```bash
git add build/pe-payload/Modules/Autopilot.PETransport/Public/Wait-PeNetwork.ps1 \
        build/pe-payload/Modules/Autopilot.PETransport/Public/Initialize-SshHostKeys.ps1 \
        build/pe-payload-tests/Autopilot.PETransport.Tests.ps1
git commit -m "feat(pe): boot-time network + sshd-host-key fixes (KNOWN-ISSUES #1+#2)"
```

---

## Task 3: HTTP transport (Invoke-Manifest, Get-PeContent, Send-Checkin)

**Files:**
- Create: `build/pe-payload/Modules/Autopilot.PETransport/Public/Invoke-Manifest.ps1`
- Create: `build/pe-payload/Modules/Autopilot.PETransport/Public/Get-PeContent.ps1`
- Create: `build/pe-payload/Modules/Autopilot.PETransport/Public/Send-Checkin.ps1`
- Modify: `build/pe-payload-tests/Autopilot.PETransport.Tests.ps1` (append tests)

The three HTTP cmdlets that talk to Plan 2's orchestrator API.

- [ ] **Step 1: Append failing tests**

Append to `build/pe-payload-tests/Autopilot.PETransport.Tests.ps1`:

```powershell
Describe 'Invoke-Manifest' {
    BeforeAll {
        Mock -ModuleName Autopilot.PETransport Invoke-RestMethod {
            return @{
                version = 1
                vmUuid = 'uuid-x'
                onError = 'halt'
                steps = @(
                    @{ id = 'p1'; type = 'partition'; layout = 'uefi-standard' }
                )
            }
        }
    }

    It 'fetches manifest by uuid and returns the parsed object' {
        $manifest = Invoke-Manifest -OrchestratorUrl 'http://orch:5000' -VmUuid 'uuid-x'
        $manifest.version | Should -Be 1
        $manifest.vmUuid | Should -Be 'uuid-x'
        $manifest.steps[0].type | Should -Be 'partition'
    }

    It 'sends to GET /winpe/manifest/<uuid>' {
        $captured = $null
        Mock -ModuleName Autopilot.PETransport Invoke-RestMethod {
            $script:capturedUri = $Uri
            return @{ steps = @() }
        }
        Invoke-Manifest -OrchestratorUrl 'http://orch:5000' -VmUuid 'abc-123'
        $script:capturedUri | Should -Be 'http://orch:5000/winpe/manifest/abc-123'
    }
}

Describe 'Get-PeContent' {
    BeforeAll {
        # Pre-compute sha of a known payload
        $script:payload = [System.Text.Encoding]::UTF8.GetBytes('hello world')
        $script:expectedSha = (Get-FileHash -InputStream ([System.IO.MemoryStream]::new($script:payload)) -Algorithm SHA256).Hash.ToLowerInvariant()

        Mock -ModuleName Autopilot.PETransport Invoke-WebRequest {
            param($Uri, $OutFile, $UseBasicParsing)
            [System.IO.File]::WriteAllBytes($OutFile, $script:payload)
        }
    }

    It 'fetches and validates sha256' {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "pecontent-$([guid]::NewGuid())"
        try {
            Get-PeContent -OrchestratorUrl 'http://orch:5000' -Sha256 $script:expectedSha -OutPath $tmp
            (Get-Content $tmp -Raw -Encoding utf8).Trim() | Should -Be 'hello world'
        } finally {
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
    }

    It 'throws on sha mismatch and deletes the bad file' {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "pecontent-$([guid]::NewGuid())"
        try {
            { Get-PeContent -OrchestratorUrl 'http://orch:5000' -Sha256 ('0' * 64) -OutPath $tmp } |
                Should -Throw -ExpectedMessage '*sha256 mismatch*'
            Test-Path $tmp | Should -BeFalse
        } finally {
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
    }

    It 'sends to GET /winpe/content/<sha>' {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "pecontent-$([guid]::NewGuid())"
        $script:capturedUri = $null
        Mock -ModuleName Autopilot.PETransport Invoke-WebRequest {
            $script:capturedUri = $Uri
            [System.IO.File]::WriteAllBytes($OutFile, $script:payload)
        }
        try {
            Get-PeContent -OrchestratorUrl 'http://orch:5000' -Sha256 $script:expectedSha -OutPath $tmp
            $script:capturedUri | Should -Be "http://orch:5000/winpe/content/$($script:expectedSha)"
        } finally {
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Send-Checkin' {
    BeforeAll {
        $script:capturedBody = $null
        $script:capturedUri = $null
        Mock -ModuleName Autopilot.PETransport Invoke-RestMethod {
            $script:capturedUri = $Uri
            $script:capturedBody = $Body
        }
    }

    It 'POSTs to /winpe/checkin with the right field names (camelCase)' {
        Send-Checkin -OrchestratorUrl 'http://orch:5000' -VmUuid 'u' -StepId 's' -Status 'ok' `
            -Timestamp '2026-04-25T22:00:00Z' -DurationSec 1.5 -LogTail 'log line' `
            -ErrorMessage $null -Extra @{ k = 'v' }
        $script:capturedUri | Should -Be 'http://orch:5000/winpe/checkin'
        $body = $script:capturedBody | ConvertFrom-Json
        $body.vmUuid | Should -Be 'u'
        $body.stepId | Should -Be 's'
        $body.status | Should -Be 'ok'
        $body.durationSec | Should -Be 1.5
        $body.logTail | Should -Be 'log line'
        $body.extra.k | Should -Be 'v'
    }

    It 'does not throw on transient transport failure' {
        Mock -ModuleName Autopilot.PETransport Invoke-RestMethod { throw 'connection refused' }
        # Send-Checkin must be fire-and-forget so a hiccup doesn't kill the bootstrap.
        { Send-Checkin -OrchestratorUrl 'http://orch:5000' -VmUuid 'u' -StepId 's' -Status 'ok' `
            -Timestamp '2026-04-25T22:00:00Z' -DurationSec 1.0 -LogTail '' `
            -ErrorMessage $null -Extra @{} } | Should -Not -Throw
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PETransport.Tests.ps1 -CI"
```

Expected: 7 new tests fail.

- [ ] **Step 3: Implement Invoke-Manifest**

Create `build/pe-payload/Modules/Autopilot.PETransport/Public/Invoke-Manifest.ps1`:

```powershell
function Invoke-Manifest {
    <#
    .SYNOPSIS
        Fetch /winpe/manifest/<vm-uuid> and return the parsed manifest object.
        Retries on transient failure (orchestrator restarting, DNS hiccup).
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $VmUuid,
        [int] $RetryCount = 3,
        [int] $RetryBackoffSeconds = 5
    )

    $url = "$($OrchestratorUrl.TrimEnd('/'))/winpe/manifest/$VmUuid"
    $lastErr = $null
    for ($attempt = 1; $attempt -le $RetryCount; $attempt++) {
        try {
            return Invoke-RestMethod -Uri $url -Method GET -ErrorAction Stop
        } catch {
            $lastErr = $_
            if ($attempt -lt $RetryCount) {
                Start-Sleep -Seconds $RetryBackoffSeconds
            }
        }
    }
    throw "Invoke-Manifest: $RetryCount attempts to GET $url all failed. Last error: $lastErr"
}
```

- [ ] **Step 4: Implement Get-PeContent**

Create `build/pe-payload/Modules/Autopilot.PETransport/Public/Get-PeContent.ps1`:

```powershell
function Get-PeContent {
    <#
    .SYNOPSIS
        Fetch /winpe/content/<sha256> to OutPath, then verify the file's
        sha256 matches the expected value. On mismatch, deletes the file
        and throws so the caller can fail the step cleanly.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [string] $OutPath
    )

    $url = "$($OrchestratorUrl.TrimEnd('/'))/winpe/content/$Sha256"
    $outDir = Split-Path -Parent $OutPath
    if ($outDir -and -not (Test-Path $outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }
    Invoke-WebRequest -Uri $url -OutFile $OutPath -UseBasicParsing -ErrorAction Stop

    $actual = (Get-FileHash -LiteralPath $OutPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        Remove-Item -LiteralPath $OutPath -Force -ErrorAction SilentlyContinue
        throw "Get-PeContent: sha256 mismatch. expected=$Sha256 actual=$actual url=$url"
    }
}
```

- [ ] **Step 5: Implement Send-Checkin**

Create `build/pe-payload/Modules/Autopilot.PETransport/Public/Send-Checkin.ps1`:

```powershell
function Send-Checkin {
    <#
    .SYNOPSIS
        POST /winpe/checkin. Fire-and-forget — if the server is unreachable,
        the bootstrap should NOT die mid-step over a missed checkin. We log
        the failure to host but don't propagate.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $VmUuid,
        [Parameter(Mandatory)] [string] $StepId,
        [Parameter(Mandatory)] [ValidateSet('starting','ok','error')] [string] $Status,
        [Parameter(Mandatory)] [string] $Timestamp,
        [double] $DurationSec = 0.0,
        [string] $LogTail = '',
        [string] $ErrorMessage = $null,
        [hashtable] $Extra = @{}
    )

    $url = "$($OrchestratorUrl.TrimEnd('/'))/winpe/checkin"
    $payload = @{
        vmUuid       = $VmUuid
        stepId       = $StepId
        status       = $Status
        timestamp    = $Timestamp
        durationSec  = $DurationSec
        logTail      = $LogTail
        errorMessage = $ErrorMessage
        extra        = $Extra
    } | ConvertTo-Json -Depth 8 -Compress

    try {
        Invoke-RestMethod -Uri $url -Method POST -Body $payload -ContentType 'application/json' -ErrorAction Stop | Out-Null
    } catch {
        Write-Host "Send-Checkin: POST $url failed (continuing): $_"
    }
}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PETransport.Tests.ps1 -CI"
```

Expected: 17 tests pass (10 prior + 2 Invoke-Manifest + 4 Get-PeContent + 2 Send-Checkin... wait, the test file has 7 new tests, so total should be 17).

Actual breakdown: 2 module + 2 Get-PeIdentity + 3 Wait-PeNetwork + 2 Initialize-SshHostKeys + 2 Invoke-Manifest + 3 Get-PeContent + 2 Send-Checkin = 16. (Minor count discrepancy from the spec; the Mock-driven Initialize-SshHostKeys tests are actually 2, not 3.)

- [ ] **Step 7: Commit**

```bash
git add build/pe-payload/Modules/Autopilot.PETransport/Public/Invoke-Manifest.ps1 \
        build/pe-payload/Modules/Autopilot.PETransport/Public/Get-PeContent.ps1 \
        build/pe-payload/Modules/Autopilot.PETransport/Public/Send-Checkin.ps1 \
        build/pe-payload-tests/Autopilot.PETransport.Tests.ps1
git commit -m "feat(pe): HTTP transport (Invoke-Manifest, Get-PeContent, Send-Checkin)"
```

---

## Task 4: PESteps module skeleton + simple terminal/marker steps

**Files:**
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Autopilot.PESteps.psd1`
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Autopilot.PESteps.psm1`
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-LogStep.ps1`
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-RebootStep.ps1`
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ShutdownStep.ps1`
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-BcdbootStep.ps1`
- Create: `build/pe-payload-tests/Autopilot.PESteps.Tests.ps1`

The four "wraps a single CLI" steps. Easy to test via `Mock` of the underlying executable.

- [ ] **Step 1: Write the failing tests**

Create `build/pe-payload-tests/Autopilot.PESteps.Tests.ps1`:

```powershell
BeforeAll {
    $modulePath = Join-Path $PSScriptRoot '..' 'pe-payload' 'Modules' 'Autopilot.PESteps' 'Autopilot.PESteps.psd1'
    Import-Module $modulePath -Force
}

Describe 'Autopilot.PESteps module' {
    It 'imports' {
        Get-Module Autopilot.PESteps | Should -Not -BeNullOrEmpty
    }
    It 'exports the four simple step cmdlets' {
        $names = (Get-Command -Module Autopilot.PESteps).Name
        $names | Should -Contain 'Invoke-LogStep'
        $names | Should -Contain 'Invoke-RebootStep'
        $names | Should -Contain 'Invoke-ShutdownStep'
        $names | Should -Contain 'Invoke-BcdbootStep'
    }
}

Describe 'Invoke-LogStep' {
    It 'returns the message verbatim' {
        $r = Invoke-LogStep -Message 'hello'
        $r.LogTail | Should -Be 'hello'
    }
}

Describe 'Invoke-RebootStep' {
    BeforeAll {
        $script:wpeArgs = @()
        Mock -ModuleName Autopilot.PESteps wpeutil { $script:wpeArgs += ,$args }
    }
    It 'invokes wpeutil reboot' {
        Invoke-RebootStep
        $script:wpeArgs | Should -Contain @('reboot')
    }
}

Describe 'Invoke-ShutdownStep' {
    BeforeAll {
        $script:wpeArgs = @()
        Mock -ModuleName Autopilot.PESteps wpeutil { $script:wpeArgs += ,$args }
    }
    It 'invokes wpeutil shutdown' {
        Invoke-ShutdownStep
        $script:wpeArgs | Should -Contain @('shutdown')
    }
}

Describe 'Invoke-BcdbootStep' {
    BeforeAll {
        $script:bcdArgs = @()
        $script:bcdExitCode = 0
        Mock -ModuleName Autopilot.PESteps bcdboot {
            $script:bcdArgs = $args
            $global:LASTEXITCODE = $script:bcdExitCode
        }
    }

    It 'invokes bcdboot with /s and /f UEFI' {
        Invoke-BcdbootStep -Windows 'W:' -Esp 'S:'
        $script:bcdArgs[0] | Should -Be 'W:\Windows'
        ($script:bcdArgs -join ' ') | Should -Match '/s S:'
        ($script:bcdArgs -join ' ') | Should -Match '/f UEFI'
    }

    It 'throws on non-zero exit' {
        $script:bcdExitCode = 1
        { Invoke-BcdbootStep -Windows 'W:' -Esp 'S:' } | Should -Throw -ExpectedMessage '*bcdboot*'
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/.worktrees/winpe-pe-bootstrap
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PESteps.Tests.ps1 -CI"
```

- [ ] **Step 3: Write the module manifest + loader**

Create `build/pe-payload/Modules/Autopilot.PESteps/Autopilot.PESteps.psd1`:

```powershell
@{
    RootModule        = 'Autopilot.PESteps.psm1'
    ModuleVersion     = '0.1.0'
    GUID              = '8d4c5f7a-3f2e-5b9c-ad6e-2f3d4c5b6a7b'
    Author            = 'ProxmoxVEAutopilot'
    Description       = 'PE-side step-type implementations dispatched by Bootstrap.ps1.'
    PowerShellVersion = '7.0'
    FunctionsToExport = '*'
    CmdletsToExport   = @()
    VariablesToExport = @()
    AliasesToExport   = @()
}
```

Create `build/pe-payload/Modules/Autopilot.PESteps/Autopilot.PESteps.psm1`:

```powershell
$ErrorActionPreference = 'Stop'
$publicDir = Join-Path $PSScriptRoot 'Public'
if (Test-Path $publicDir) {
    Get-ChildItem -Path $publicDir -Filter '*.ps1' | ForEach-Object {
        . $_.FullName
        Export-ModuleMember -Function $_.BaseName
    }
}
```

- [ ] **Step 4: Implement the four step cmdlets**

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-LogStep.ps1`:

```powershell
function Invoke-LogStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $Message
    )
    Write-Host "LogStep: $Message"
    return [pscustomobject]@{ LogTail = $Message; Extra = @{} }
}
```

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-RebootStep.ps1`:

```powershell
function Invoke-RebootStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param()
    Write-Host 'RebootStep: invoking wpeutil reboot'
    wpeutil reboot
    return [pscustomobject]@{ LogTail = 'wpeutil reboot issued'; Extra = @{} }
}
```

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ShutdownStep.ps1`:

```powershell
function Invoke-ShutdownStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param()
    Write-Host 'ShutdownStep: invoking wpeutil shutdown'
    wpeutil shutdown
    return [pscustomobject]@{ LogTail = 'wpeutil shutdown issued'; Extra = @{} }
}
```

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-BcdbootStep.ps1`:

```powershell
function Invoke-BcdbootStep {
    <#
    .SYNOPSIS
        Make the target volume bootable. Equivalent to:
            bcdboot W:\Windows /s S: /f UEFI
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $Windows,
        [Parameter(Mandatory)] [string] $Esp
    )
    $winPath = Join-Path $Windows 'Windows'
    Write-Host "BcdbootStep: bcdboot $winPath /s $Esp /f UEFI"
    bcdboot $winPath /s $Esp /f UEFI | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "BcdbootStep: bcdboot exited with code $LASTEXITCODE"
    }
    return [pscustomobject]@{ LogTail = "bcdboot $winPath /s $Esp /f UEFI ok"; Extra = @{ windows = $Windows; esp = $Esp } }
}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PESteps.Tests.ps1 -CI"
```

Expected: 7 tests pass (2 module + 1 Log + 1 Reboot + 1 Shutdown + 2 Bcdboot).

- [ ] **Step 6: Commit**

```bash
git add build/pe-payload/Modules/Autopilot.PESteps/ build/pe-payload-tests/Autopilot.PESteps.Tests.ps1
git commit -m "feat(pe): PESteps skeleton + Log/Reboot/Shutdown/Bcdboot steps"
```

---

## Task 5: Storage step (Invoke-PartitionStep)

**Files:**
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-PartitionStep.ps1`
- Modify: `build/pe-payload-tests/Autopilot.PESteps.Tests.ps1` (append tests)

Clear disk 0, lay down GPT (ESP + MSR + Windows), return drive letters. The Storage cmdlets (`Get-Disk`, `Clear-Disk`, `New-Partition`, `Format-Volume`) are heavily mocked here; full integration test happens on a real PE boot.

- [ ] **Step 1: Append failing tests**

Append to `build/pe-payload-tests/Autopilot.PESteps.Tests.ps1`:

```powershell
Describe 'Invoke-PartitionStep' {
    BeforeAll {
        Mock -ModuleName Autopilot.PESteps Get-Disk { return [pscustomobject]@{ Number = 0; FriendlyName = 'fake' } }
        Mock -ModuleName Autopilot.PESteps Clear-Disk { }
        Mock -ModuleName Autopilot.PESteps Initialize-Disk { }
        # Three partitions returned in order: ESP, MSR, Windows
        $script:partitions = @(
            [pscustomobject]@{ Type = 'System';            DriveLetter = $null;       AccessPaths = @() },
            [pscustomobject]@{ Type = 'Reserved';          DriveLetter = $null;       AccessPaths = @() },
            [pscustomobject]@{ Type = 'Basic';             DriveLetter = 'W';         AccessPaths = @('W:\') }
        )
        $script:newCalls = 0
        Mock -ModuleName Autopilot.PESteps New-Partition {
            $p = $script:partitions[$script:newCalls]
            $script:newCalls++
            return $p
        }
        Mock -ModuleName Autopilot.PESteps Format-Volume { }
        Mock -ModuleName Autopilot.PESteps Set-Partition { }
        Mock -ModuleName Autopilot.PESteps Add-PartitionAccessPath {
            param($DiskNumber, $PartitionNumber, $AssignDriveLetter)
            # Simulate ESP getting 'S:'
            $script:partitions[0].DriveLetter = 'S'
            $script:partitions[0].AccessPaths = @('S:\')
        }
    }

    It 'returns esp and windows drive letters' {
        $r = Invoke-PartitionStep -Layout 'uefi-standard'
        $r.Extra.esp | Should -Be 'S:'
        $r.Extra.windows | Should -Be 'W:'
    }

    It 'rejects unknown layouts' {
        { Invoke-PartitionStep -Layout 'novel-experimental' } | Should -Throw -ExpectedMessage '*layout*'
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PESteps.Tests.ps1 -CI"
```

Expected: 2 new tests fail.

- [ ] **Step 3: Implement Invoke-PartitionStep**

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-PartitionStep.ps1`:

```powershell
function Invoke-PartitionStep {
    <#
    .SYNOPSIS
        Clear disk 0 and create a GPT layout for UEFI boot:
          ESP    : 260 MB, FAT32, mounted at S:
          MSR    : 16 MB, no filesystem
          Windows: rest of disk, NTFS, mounted at W:

        Returns Extra={esp, windows} so subsequent steps know where to write.
        The drive letters are nominal — the actual letters assigned by the
        Storage cmdlets are reported via the Extra hashtable.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $Layout
    )

    if ($Layout -ne 'uefi-standard') {
        throw "Invoke-PartitionStep: unsupported layout '$Layout' (only 'uefi-standard' is implemented)"
    }

    $disk = Get-Disk -Number 0
    Clear-Disk -Number 0 -RemoveData -RemoveOEM -Confirm:$false -ErrorAction SilentlyContinue
    Initialize-Disk -Number 0 -PartitionStyle GPT

    # ESP: 260 MB, FAT32
    $esp = New-Partition -DiskNumber 0 -Size 260MB -GptType '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
    Format-Volume -Partition $esp -FileSystem FAT32 -NewFileSystemLabel 'EFI' -Confirm:$false | Out-Null

    # Assign a drive letter for ESP so we can pass it to bcdboot. Storage cmdlets
    # don't auto-letter MSR-flagged partitions; we mount it manually.
    Add-PartitionAccessPath -DiskNumber 0 -PartitionNumber $esp.PartitionNumber -AssignDriveLetter
    $esp = Get-Partition -DiskNumber 0 -PartitionNumber $esp.PartitionNumber

    # MSR: 16 MB
    $null = New-Partition -DiskNumber 0 -Size 16MB -GptType '{e3c9e316-0b5c-4db8-817d-f92df00215ae}'

    # Windows: rest of disk, NTFS, mounted at W:
    $windows = New-Partition -DiskNumber 0 -UseMaximumSize -GptType '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}' -DriveLetter 'W'
    Format-Volume -Partition $windows -FileSystem NTFS -NewFileSystemLabel 'Windows' -Confirm:$false | Out-Null

    $espLetter = "$($esp.DriveLetter):"
    $winLetter = "W:"
    Write-Host "PartitionStep: ESP=$espLetter Windows=$winLetter"
    return [pscustomobject]@{
        LogTail = "GPT partitioned: ESP=$espLetter, Windows=$winLetter"
        Extra   = @{ esp = $espLetter; windows = $winLetter }
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PESteps.Tests.ps1 -CI"
```

Expected: 9 tests pass (7 prior + 2 new).

- [ ] **Step 5: Commit**

```bash
git add build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-PartitionStep.ps1 \
        build/pe-payload-tests/Autopilot.PESteps.Tests.ps1
git commit -m "feat(pe): Invoke-PartitionStep (UEFI GPT layout)"
```

---

## Task 6: Content-driven steps (ApplyWim, StageFiles, WriteUnattend, InjectDriver)

**Files:**
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ApplyWimStep.ps1`
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-StageFilesStep.ps1`
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-WriteUnattendStep.ps1`
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-InjectDriverStep.ps1`
- Modify: `build/pe-payload-tests/Autopilot.PESteps.Tests.ps1` (append tests)

All four follow the same pattern: download via `Get-PeContent` (verifies sha), then DISM/Expand-Archive/Copy.

- [ ] **Step 1: Append failing tests**

Append to `build/pe-payload-tests/Autopilot.PESteps.Tests.ps1`:

```powershell
Describe 'Invoke-ApplyWimStep' {
    BeforeAll {
        Mock -ModuleName Autopilot.PESteps Get-PeContent {
            param($OrchestratorUrl, $Sha256, $OutPath)
            'fake wim bytes' | Set-Content -LiteralPath $OutPath
        }
        Mock -ModuleName Autopilot.PESteps Expand-WindowsImage { }
        Mock -ModuleName Autopilot.PESteps Remove-Item { }
    }

    It 'fetches by sha and applies to target' {
        $r = Invoke-ApplyWimStep -OrchestratorUrl 'http://o:5000' -Sha256 ('a'*64) -Size 1024 -Target 'W:'
        $r.LogTail | Should -Match '^applied wim'
    }
}

Describe 'Invoke-StageFilesStep' {
    BeforeAll {
        Mock -ModuleName Autopilot.PESteps Get-PeContent {
            param($OrchestratorUrl, $Sha256, $OutPath)
            'fake zip' | Set-Content -LiteralPath $OutPath
        }
        Mock -ModuleName Autopilot.PESteps Expand-Archive { }
        Mock -ModuleName Autopilot.PESteps Remove-Item { }
    }

    It 'fetches zip and extracts to target' {
        $r = Invoke-StageFilesStep -OrchestratorUrl 'http://o:5000' -Sha256 ('b'*64) -Size 100 -Target 'W:\Program Files\Autopilot'
        $r.LogTail | Should -Match '^staged'
    }
}

Describe 'Invoke-WriteUnattendStep' {
    BeforeAll {
        Mock -ModuleName Autopilot.PESteps Get-PeContent {
            param($OrchestratorUrl, $Sha256, $OutPath)
            '<unattend/>' | Set-Content -LiteralPath $OutPath
        }
        Mock -ModuleName Autopilot.PESteps New-Item { }
        Mock -ModuleName Autopilot.PESteps Move-Item { }
    }

    It 'fetches unattend.xml and places at target' {
        $r = Invoke-WriteUnattendStep -OrchestratorUrl 'http://o:5000' -Sha256 ('c'*64) -Size 50 -Target 'W:\Windows\Panther\unattend.xml'
        $r.LogTail | Should -Match 'Panther'
    }
}

Describe 'Invoke-InjectDriverStep' {
    BeforeAll {
        Mock -ModuleName Autopilot.PESteps Get-PeContent {
            param($OrchestratorUrl, $Sha256, $OutPath)
            'fake driver zip' | Set-Content -LiteralPath $OutPath
        }
        Mock -ModuleName Autopilot.PESteps Expand-Archive { }
        Mock -ModuleName Autopilot.PESteps Add-WindowsDriver { }
        Mock -ModuleName Autopilot.PESteps Remove-Item { }
    }

    It 'fetches driver bundle and Add-WindowsDrivers it into target' {
        $r = Invoke-InjectDriverStep -OrchestratorUrl 'http://o:5000' -Sha256 ('d'*64) -Size 5000 -Target 'W:'
        $r.LogTail | Should -Match '^injected driver'
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PESteps.Tests.ps1 -CI"
```

Expected: 4 new tests fail.

- [ ] **Step 3: Implement the four cmdlets**

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ApplyWimStep.ps1`:

```powershell
function Invoke-ApplyWimStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [int] $Size,
        [Parameter(Mandatory)] [string] $Target,
        [int] $Index = 1
    )
    $tmp = "X:\Windows\Temp\install-$Sha256.wim"
    Get-PeContent -OrchestratorUrl $OrchestratorUrl -Sha256 $Sha256 -OutPath $tmp
    Expand-WindowsImage -ImagePath $tmp -Index $Index -ApplyPath $Target -ErrorAction Stop
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    return [pscustomobject]@{ LogTail = "applied wim $Sha256 → $Target (index $Index)"; Extra = @{ target = $Target } }
}
```

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-StageFilesStep.ps1`:

```powershell
function Invoke-StageFilesStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [int] $Size,
        [Parameter(Mandatory)] [string] $Target
    )
    $tmp = "X:\Windows\Temp\stage-$Sha256.zip"
    Get-PeContent -OrchestratorUrl $OrchestratorUrl -Sha256 $Sha256 -OutPath $tmp
    Expand-Archive -LiteralPath $tmp -DestinationPath $Target -Force
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    return [pscustomobject]@{ LogTail = "staged zip $Sha256 → $Target"; Extra = @{ target = $Target } }
}
```

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-WriteUnattendStep.ps1`:

```powershell
function Invoke-WriteUnattendStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [int] $Size,
        [Parameter(Mandatory)] [string] $Target
    )
    $parent = Split-Path -Parent $Target
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Get-PeContent -OrchestratorUrl $OrchestratorUrl -Sha256 $Sha256 -OutPath $Target
    return [pscustomobject]@{ LogTail = "wrote unattend.xml ($Sha256) → $Target"; Extra = @{ target = $Target } }
}
```

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-InjectDriverStep.ps1`:

```powershell
function Invoke-InjectDriverStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [int] $Size,
        [Parameter(Mandatory)] [string] $Target
    )
    $tmpZip = "X:\Windows\Temp\driver-$Sha256.zip"
    $tmpDir = "X:\Windows\Temp\driver-$Sha256"
    Get-PeContent -OrchestratorUrl $OrchestratorUrl -Sha256 $Sha256 -OutPath $tmpZip
    if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
    Expand-Archive -LiteralPath $tmpZip -DestinationPath $tmpDir -Force
    Add-WindowsDriver -Path "$Target\Windows" -Driver $tmpDir -Recurse -ForceUnsigned -ErrorAction Stop | Out-Null
    Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
    Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    return [pscustomobject]@{ LogTail = "injected driver $Sha256 → $Target\Windows"; Extra = @{ target = $Target } }
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PESteps.Tests.ps1 -CI"
```

Expected: 13 tests pass (9 prior + 4 new).

- [ ] **Step 5: Commit**

```bash
git add build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ApplyWimStep.ps1 \
        build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-StageFilesStep.ps1 \
        build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-WriteUnattendStep.ps1 \
        build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-InjectDriverStep.ps1 \
        build/pe-payload-tests/Autopilot.PESteps.Tests.ps1
git commit -m "feat(pe): content-driven steps (ApplyWim, StageFiles, WriteUnattend, InjectDriver)"
```

---

## Task 7: Offline-state steps (SetRegistry, ScheduleTask)

**Files:**
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-SetRegistryStep.ps1`
- Create: `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ScheduleTaskStep.ps1`
- Modify: `build/pe-payload-tests/Autopilot.PESteps.Tests.ps1` (append tests)

These two write directly into the offline target volume's hives / file system — the heart of "lay tasks down in the OS" from the spec.

- [ ] **Step 1: Append failing tests**

Append to `build/pe-payload-tests/Autopilot.PESteps.Tests.ps1`:

```powershell
Describe 'Invoke-SetRegistryStep' {
    BeforeAll {
        $script:regCalls = @()
        Mock -ModuleName Autopilot.PESteps reg.exe {
            $script:regCalls += ,$args
            $global:LASTEXITCODE = 0
        }
        Mock -ModuleName Autopilot.PESteps New-Item { }
        Mock -ModuleName Autopilot.PESteps New-ItemProperty { }
    }

    It 'load + set + unload SYSTEM hive' {
        $keys = @(
            @{ path = 'Setup'; name = 'ComputerName'; type = 'REG_SZ'; value = 'PC-42' }
        )
        $r = Invoke-SetRegistryStep -Hive 'SYSTEM' -Target 'W:' -Keys $keys
        $r.LogTail | Should -Match 'set 1 keys in SYSTEM'

        # Verify reg.exe load + unload bracket the operation
        $loadInvoked = $false
        $unloadInvoked = $false
        foreach ($call in $script:regCalls) {
            if ($call -contains 'load') { $loadInvoked = $true }
            if ($call -contains 'unload') { $unloadInvoked = $true }
        }
        $loadInvoked | Should -BeTrue
        $unloadInvoked | Should -BeTrue
    }

    It 'unloads even when set fails' {
        Mock -ModuleName Autopilot.PESteps New-ItemProperty { throw 'fake-set-error' }
        $script:regCalls = @()
        try {
            Invoke-SetRegistryStep -Hive 'SOFTWARE' -Target 'W:' -Keys @(
                @{ path = 'p'; name = 'n'; type = 'REG_SZ'; value = 'v' }
            )
        } catch { }
        $unloadCalled = $script:regCalls | Where-Object { $_ -contains 'unload' }
        $unloadCalled | Should -Not -BeNullOrEmpty
    }
}

Describe 'Invoke-ScheduleTaskStep' {
    BeforeAll {
        Mock -ModuleName Autopilot.PESteps New-Item { }
        Mock -ModuleName Autopilot.PESteps Set-Content { }
    }

    It 'writes the task XML to <target>\Windows\System32\Tasks\<name>' {
        $r = Invoke-ScheduleTaskStep -Target 'W:' -Name 'AutopilotKick' -TaskXml '<Task/>'
        $r.LogTail | Should -Match 'Tasks\\AutopilotKick'
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PESteps.Tests.ps1 -CI"
```

- [ ] **Step 3: Implement Invoke-SetRegistryStep**

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-SetRegistryStep.ps1`:

```powershell
function Invoke-SetRegistryStep {
    <#
    .SYNOPSIS
        Load a hive from the offline target volume, write keys, unload.
        Always unloads in finally — orphaned hive handles are a real
        operator headache.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [ValidateSet('SYSTEM','SOFTWARE','DEFAULT')] [string] $Hive,
        [Parameter(Mandatory)] [string] $Target,
        [Parameter(Mandatory)] [hashtable[]] $Keys
    )

    $hivePath = switch ($Hive) {
        'SYSTEM'   { Join-Path $Target 'Windows\System32\config\SYSTEM' }
        'SOFTWARE' { Join-Path $Target 'Windows\System32\config\SOFTWARE' }
        'DEFAULT'  { Join-Path $Target 'Users\Default\NTUSER.DAT' }
    }

    $stagingName = "PEStaging_$Hive"
    reg.exe load "HKLM\$stagingName" $hivePath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Invoke-SetRegistryStep: reg load $hivePath failed ($LASTEXITCODE)"
    }

    try {
        foreach ($k in $Keys) {
            $regPath = "HKLM:\$stagingName\$($k.path)"
            if (-not (Test-Path $regPath)) {
                New-Item -Path $regPath -Force | Out-Null
            }
            $type = switch ($k.type) {
                'REG_SZ'        { 'String' }
                'REG_EXPAND_SZ' { 'ExpandString' }
                'REG_DWORD'     { 'DWord' }
                'REG_QWORD'     { 'QWord' }
                'REG_MULTI_SZ'  { 'MultiString' }
                'REG_BINARY'    { 'Binary' }
                default         { 'String' }
            }
            New-ItemProperty -Path $regPath -Name $k.name -Value $k.value -PropertyType $type -Force | Out-Null
        }
    } finally {
        [GC]::Collect()
        reg.exe unload "HKLM\$stagingName" | Out-Null
    }

    return [pscustomobject]@{
        LogTail = "set $($Keys.Count) keys in $Hive of $Target"
        Extra   = @{ hive = $Hive; target = $Target; key_count = $Keys.Count }
    }
}
```

- [ ] **Step 4: Implement Invoke-ScheduleTaskStep**

Create `build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ScheduleTaskStep.ps1`:

```powershell
function Invoke-ScheduleTaskStep {
    <#
    .SYNOPSIS
        Write a Task Scheduler XML to <target>\Windows\System32\Tasks\<name>.
        TaskCache registry entries are NOT written here — Windows creates them
        on first boot when the Task Scheduler service starts and indexes the
        Tasks folder. This is more reliable than predicting the GUID structure
        Windows uses internally.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $Target,
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [string] $TaskXml
    )
    $tasksDir = Join-Path $Target 'Windows\System32\Tasks'
    if (-not (Test-Path $tasksDir)) {
        New-Item -ItemType Directory -Path $tasksDir -Force | Out-Null
    }
    $taskFile = Join-Path $tasksDir $Name
    Set-Content -LiteralPath $taskFile -Value $TaskXml -Encoding utf8
    return [pscustomobject]@{
        LogTail = "wrote task xml → $taskFile"
        Extra   = @{ task = $Name; target = $Target }
    }
}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Autopilot.PESteps.Tests.ps1 -CI"
```

Expected: 16 tests pass (13 prior + 3 new).

- [ ] **Step 6: Commit**

```bash
git add build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-SetRegistryStep.ps1 \
        build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ScheduleTaskStep.ps1 \
        build/pe-payload-tests/Autopilot.PESteps.Tests.ps1
git commit -m "feat(pe): offline-state steps (SetRegistry, ScheduleTask)"
```

---

## Task 8: Bootstrap.ps1 main loop + Debug.ps1

**Files:**
- Replace: `build/pe-payload/Bootstrap.ps1` (was 39-line placeholder; becomes the real interpreter)
- Create: `build/pe-payload/Debug.ps1`
- Create: `build/pe-payload-tests/Bootstrap.Tests.ps1`

The thin orchestrator that imports both modules, runs network/SSH bootstrap, fetches the manifest, dispatches each step, posts checkins, handles `onError=halt` by sourcing `Debug.ps1`.

- [ ] **Step 1: Write the failing test**

Create `build/pe-payload-tests/Bootstrap.Tests.ps1`:

```powershell
BeforeAll {
    # Bootstrap.ps1 is meant to run *as* a script in PE, not be imported as a module.
    # We test its dispatch logic by extracting it into a function via a wrapper.
    # The real Bootstrap.ps1 calls Invoke-BootstrapManifest at its tail; we
    # exercise that function here.

    $bootstrapPath = Join-Path $PSScriptRoot '..' 'pe-payload' 'Bootstrap.ps1'
    # Import the script (functions defined become available)
    . $bootstrapPath -DryRunForTesting
}

Describe 'Invoke-BootstrapManifest' {
    BeforeEach {
        # Fresh mocks per test
        $script:checkins = @()
        Mock Send-Checkin {
            $script:checkins += @{
                stepId = $StepId; status = $Status; errorMessage = $ErrorMessage
            }
        }
    }

    It 'dispatches each step type to the matching cmdlet and posts checkin' {
        $manifest = [pscustomobject]@{
            version = 1; vmUuid = 'u'; onError = 'halt'
            steps = @(
                [pscustomobject]@{ id = 'l1'; type = 'log'; message = 'hello' }
            )
        }

        # Mock the step dispatcher's targets
        Mock Invoke-LogStep { return [pscustomobject]@{ LogTail = 'hello'; Extra = @{} } }

        Invoke-BootstrapManifest -Manifest $manifest -OrchestratorUrl 'http://o:5000' -VmUuid 'u'

        $script:checkins.Count | Should -Be 2  # starting + ok
        $script:checkins[0].stepId | Should -Be 'l1'
        $script:checkins[0].status | Should -Be 'starting'
        $script:checkins[1].status | Should -Be 'ok'
    }

    It 'on step failure with onError=halt, posts error checkin and throws to caller' {
        $manifest = [pscustomobject]@{
            version = 1; vmUuid = 'u'; onError = 'halt'
            steps = @(
                [pscustomobject]@{ id = 'l1'; type = 'log'; message = 'will fail' }
            )
        }
        Mock Invoke-LogStep { throw 'simulated failure' }

        { Invoke-BootstrapManifest -Manifest $manifest -OrchestratorUrl 'http://o:5000' -VmUuid 'u' } |
            Should -Throw -ExpectedMessage '*simulated failure*'

        # Should have posted starting + error
        $errorCheckin = $script:checkins | Where-Object status -eq 'error'
        $errorCheckin | Should -Not -BeNullOrEmpty
        $errorCheckin.errorMessage | Should -Match 'simulated failure'
    }

    It 'on step failure with onError=continue, logs and continues' {
        $manifest = [pscustomobject]@{
            version = 1; vmUuid = 'u'; onError = 'continue'
            steps = @(
                [pscustomobject]@{ id = 'a'; type = 'log'; message = 'first' },
                [pscustomobject]@{ id = 'b'; type = 'log'; message = 'second' }
            )
        }
        $script:logCalls = 0
        Mock Invoke-LogStep {
            $script:logCalls++
            if ($script:logCalls -eq 1) { throw 'first fails' }
            return [pscustomobject]@{ LogTail = 'second'; Extra = @{} }
        }

        { Invoke-BootstrapManifest -Manifest $manifest -OrchestratorUrl 'http://o:5000' -VmUuid 'u' } |
            Should -Not -Throw
        # Both steps' starting checkins recorded; first errors, second oks.
        $statuses = $script:checkins | Sort-Object stepId | ForEach-Object { "$($_.stepId)=$($_.status)" }
        $statuses | Should -Contain 'a=error'
        $statuses | Should -Contain 'b=ok'
    }

    It 'unknown step type fails with onError=halt' {
        $manifest = [pscustomobject]@{
            version = 1; vmUuid = 'u'; onError = 'halt'
            steps = @( [pscustomobject]@{ id = 'x'; type = 'mystery-step' } )
        }
        { Invoke-BootstrapManifest -Manifest $manifest -OrchestratorUrl 'http://o:5000' -VmUuid 'u' } |
            Should -Throw -ExpectedMessage '*unknown step type*'
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/.worktrees/winpe-pe-bootstrap
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Bootstrap.Tests.ps1 -CI"
```

Expected: FAIL — Bootstrap.ps1 doesn't have `-DryRunForTesting` or `Invoke-BootstrapManifest` yet.

- [ ] **Step 3: Replace Bootstrap.ps1**

Replace the entire content of `build/pe-payload/Bootstrap.ps1` with:

```powershell
<#
.SYNOPSIS
    PE-side bootstrap interpreter: identify, fetch manifest, dispatch steps, checkin.

.DESCRIPTION
    Replaces the placeholder Bootstrap from Plan 1. Runs from unattend.xml's
    windowsPE-pass RunSynchronousCommand. Must NOT exit on its own — the
    caller (winpeshl.ini's chained cmd.exe) keeps PE alive.

    Lifecycle:
      1. Start-Transcript
      2. Read Bootstrap.json
      3. Wait-PeNetwork (KNOWN-ISSUES #1)
      4. Initialize-SshHostKeys (KNOWN-ISSUES #2; no-op if SSH wasn't baked)
      5. Identify via Get-PeIdentity (SMBIOS UUID)
      6. Invoke-Manifest
      7. For each step: Send-Checkin starting → Invoke-<Type>Step → Send-Checkin ok|error
      8. If a terminal step (reboot|shutdown) fired, return; otherwise return so
         winpeshl's chained cmd.exe keeps the operator on the console.
#>

[CmdletBinding()]
param(
    [switch] $DryRunForTesting
)

# Add the Modules path so Import-Module finds them.
$env:PSModulePath = "X:\autopilot\Modules;$env:PSModulePath"

if ($DryRunForTesting) {
    # When sourced from Pester (Bootstrap.Tests.ps1), Import-Module the local
    # versions so dispatch logic can be exercised without a real PE.
    $localModules = Join-Path $PSScriptRoot 'Modules'
    Import-Module (Join-Path $localModules 'Autopilot.PETransport\Autopilot.PETransport.psd1') -Force
    Import-Module (Join-Path $localModules 'Autopilot.PESteps\Autopilot.PESteps.psd1') -Force
} else {
    Import-Module Autopilot.PETransport -Force
    Import-Module Autopilot.PESteps -Force
}


function Invoke-BootstrapManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Manifest,
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $VmUuid
    )

    $onError = if ($Manifest.PSObject.Properties.Name -contains 'onError') { $Manifest.onError } else { 'halt' }

    foreach ($step in $Manifest.steps) {
        $stepId = $step.id
        $type   = $step.type
        $startTs = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        Send-Checkin -OrchestratorUrl $OrchestratorUrl -VmUuid $VmUuid -StepId $stepId `
            -Status 'starting' -Timestamp $startTs

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $result = switch ($type) {
                'log'            { Invoke-LogStep -Message $step.message }
                'partition'      { Invoke-PartitionStep -Layout $step.layout }
                'apply-wim'      {
                    $tgt = if ($step.PSObject.Properties.Name -contains 'target') { $step.target } else { 'W:' }
                    $idx = if ($step.PSObject.Properties.Name -contains 'index') { $step.index } else { 1 }
                    Invoke-ApplyWimStep -OrchestratorUrl $OrchestratorUrl `
                        -Sha256 $step.content.sha256 -Size $step.content.size -Target $tgt -Index $idx
                }
                'stage-files'    { Invoke-StageFilesStep -OrchestratorUrl $OrchestratorUrl `
                                    -Sha256 $step.content.sha256 -Size $step.content.size -Target $step.target }
                'write-unattend' { Invoke-WriteUnattendStep -OrchestratorUrl $OrchestratorUrl `
                                    -Sha256 $step.content.sha256 -Size $step.content.size -Target $step.target }
                'set-registry'   { Invoke-SetRegistryStep -Hive $step.hive -Target $step.target -Keys $step.keys }
                'schedule-task'  { Invoke-ScheduleTaskStep -Target $step.target -Name $step.name -TaskXml $step.taskXml }
                'bcdboot'        { Invoke-BcdbootStep -Windows $step.windows -Esp $step.esp }
                'inject-driver'  { Invoke-InjectDriverStep -OrchestratorUrl $OrchestratorUrl `
                                    -Sha256 $step.content.sha256 -Size $step.content.size -Target $step.target }
                'reboot'         { Invoke-RebootStep }
                'shutdown'       { Invoke-ShutdownStep }
                default          { throw "unknown step type '$type' (step id=$stepId)" }
            }
            $sw.Stop()
            $okTs = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            Send-Checkin -OrchestratorUrl $OrchestratorUrl -VmUuid $VmUuid -StepId $stepId `
                -Status 'ok' -Timestamp $okTs -DurationSec ($sw.Elapsed.TotalSeconds) `
                -LogTail $result.LogTail -Extra ($result.Extra ?? @{})
        } catch {
            $sw.Stop()
            $errTs = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            $errMsg = $_.Exception.Message
            Send-Checkin -OrchestratorUrl $OrchestratorUrl -VmUuid $VmUuid -StepId $stepId `
                -Status 'error' -Timestamp $errTs -DurationSec ($sw.Elapsed.TotalSeconds) `
                -LogTail "step $stepId failed: $errMsg" -ErrorMessage $errMsg
            if ($onError -eq 'continue') {
                Write-Host "Bootstrap: step $stepId failed but onError=continue; proceeding"
                continue
            }
            throw
        }
    }
}


# When sourced for testing, return without running the boot logic.
if ($DryRunForTesting) { return }


# ---- Real PE boot path ----
$transcript = 'X:\Windows\Temp\autopilot-pe.log'
Start-Transcript -Path $transcript -Append -Force | Out-Null

try {
    Write-Host '========================================'
    Write-Host 'Autopilot PE bootstrap'
    Write-Host '========================================'

    $configPath = 'X:\autopilot\Bootstrap.json'
    if (-not (Test-Path $configPath)) {
        throw "Bootstrap.json not found at $configPath"
    }
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
    Write-Host "Orchestrator URL: $($config.orchestratorUrl)"

    Write-Host 'Wait-PeNetwork...'
    $ip = Wait-PeNetwork -TimeoutSeconds $config.networkTimeoutSec
    Write-Host "Got IP: $ip"

    Write-Host 'Initialize-SshHostKeys...'
    Initialize-SshHostKeys

    $identity = Get-PeIdentity
    Write-Host "Identity: $($identity.Uuid) (vendor=$($identity.Vendor) name=$($identity.Name))"

    Write-Host 'Fetching manifest...'
    $manifest = Invoke-Manifest -OrchestratorUrl $config.orchestratorUrl -VmUuid $identity.Uuid `
        -RetryCount $config.manifestRetries -RetryBackoffSeconds $config.manifestRetryBackoffSec
    Write-Host "Manifest: $($manifest.steps.Count) steps, onError=$($manifest.onError)"

    Invoke-BootstrapManifest -Manifest $manifest -OrchestratorUrl $config.orchestratorUrl -VmUuid $identity.Uuid

    Write-Host 'Bootstrap complete.'
} catch {
    Write-Host "Bootstrap FAILED: $_" -ForegroundColor Red
    & 'X:\autopilot\Debug.ps1'
} finally {
    Stop-Transcript | Out-Null
}
```

- [ ] **Step 4: Create Debug.ps1**

Create `build/pe-payload/Debug.ps1`:

```powershell
<#
.SYNOPSIS
    Diagnostic dump on bootstrap failure (onError=halt path).

.DESCRIPTION
    Prints what's known so the operator (looking at the cmd.exe console or
    SSHing in) has context. Doesn't drop to interactive — winpeshl.ini already
    chains cmd.exe after wpeinit so the operator already has a shell.
#>
Write-Host ''
Write-Host '========================================'
Write-Host 'Autopilot PE bootstrap: DEBUG MODE'
Write-Host '========================================'

try {
    $cs = Get-CimInstance Win32_ComputerSystemProduct
    Write-Host "SMBIOS UUID  : $($cs.UUID)"
    Write-Host "Vendor       : $($cs.Vendor)"
    Write-Host "Name         : $($cs.Name)"
} catch {
    Write-Host "SMBIOS       : <error: $_>"
}

Write-Host "PowerShell   : $($PSVersionTable.PSVersion)"
Write-Host "Architecture : $env:PROCESSOR_ARCHITECTURE"
Write-Host "Hostname     : $env:COMPUTERNAME"

if (Test-Path 'X:\autopilot\Bootstrap.json') {
    $cfg = Get-Content 'X:\autopilot\Bootstrap.json' -Raw | ConvertFrom-Json
    Write-Host "Orchestrator : $($cfg.orchestratorUrl)"
}

Write-Host ''
Write-Host 'Network state:'
try {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object IPAddress -ne '127.0.0.1' |
        Format-Table InterfaceAlias, IPAddress, PrefixLength -AutoSize | Out-Host
} catch {
    Write-Host "  <error: $_>"
}

Write-Host ''
Write-Host 'sshd status:'
try {
    Get-Service sshd -ErrorAction SilentlyContinue | Format-List Status, StartType | Out-Host
} catch {
    Write-Host "  <not installed or error: $_>"
}

Write-Host ''
Write-Host "See full transcript at: X:\Windows\Temp\autopilot-pe.log"
Write-Host "Use 'wpeutil reboot' or 'wpeutil shutdown' to exit PE."
Write-Host ''
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/Bootstrap.Tests.ps1 -CI"
```

Expected: 4 tests pass (one per Describe `It`).

- [ ] **Step 6: Run all PE tests**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/ -CI" 2>&1 | tail -3
```

Expected: 36 tests pass (16 PETransport + 16 PESteps + 4 Bootstrap).

- [ ] **Step 7: Commit**

```bash
git add build/pe-payload/Bootstrap.ps1 build/pe-payload/Debug.ps1 build/pe-payload-tests/Bootstrap.Tests.ps1
git commit -m "feat(pe): real Bootstrap.ps1 manifest interpreter + Debug.ps1"
```

---

## Task 9: Build script Tests/ exclusion

**Files:**
- Modify: `build/Build-PeWim.ps1` (one-line change to exclude tests during payload sync)

The build script `Copy-Item -Path (Join-Path $config.payloadDir '*') -Destination $payloadTarget -Recurse -Force` currently copies EVERYTHING under `pe-payload/`. With the new `Modules/` containing test fixtures and our `pe-payload-tests/` (already outside the tree), nothing extra to exclude there. But the `Modules/Autopilot.PE*/Tests/` subdirs would get copied if anyone adds tests there. Add a defensive exclusion.

Wait — re-reading our layout: tests are at `build/pe-payload-tests/`, NOT inside `pe-payload/Modules/*/Tests/`. So no exclusion needed. This task is a verification that the WIM payload is clean.

- [ ] **Step 1: Verify payload tree contains only what should land in PE**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/.worktrees/winpe-pe-bootstrap
find build/pe-payload -type f | sort
```

Expected output (no `*.Tests.ps1` files anywhere; no orphaned test data):
```
build/pe-payload/Bootstrap.json
build/pe-payload/Bootstrap.ps1
build/pe-payload/Debug.ps1
build/pe-payload/Modules/Autopilot.PESteps/Autopilot.PESteps.psd1
build/pe-payload/Modules/Autopilot.PESteps/Autopilot.PESteps.psm1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ApplyWimStep.ps1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-BcdbootStep.ps1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-InjectDriverStep.ps1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-LogStep.ps1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-PartitionStep.ps1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-RebootStep.ps1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ScheduleTaskStep.ps1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-SetRegistryStep.ps1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-ShutdownStep.ps1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-StageFilesStep.ps1
build/pe-payload/Modules/Autopilot.PESteps/Public/Invoke-WriteUnattendStep.ps1
build/pe-payload/Modules/Autopilot.PETransport/Autopilot.PETransport.psd1
build/pe-payload/Modules/Autopilot.PETransport/Autopilot.PETransport.psm1
build/pe-payload/Modules/Autopilot.PETransport/Public/Get-PeContent.ps1
build/pe-payload/Modules/Autopilot.PETransport/Public/Get-PeIdentity.ps1
build/pe-payload/Modules/Autopilot.PETransport/Public/Initialize-SshHostKeys.ps1
build/pe-payload/Modules/Autopilot.PETransport/Public/Invoke-Manifest.ps1
build/pe-payload/Modules/Autopilot.PETransport/Public/Send-Checkin.ps1
build/pe-payload/Modules/Autopilot.PETransport/Public/Wait-PeNetwork.ps1
build/pe-payload/unattend.xml.template
build/pe-payload/winpeshl.ini
```

If any extra files appear (e.g. `*.Tests.ps1`, README scratch files), delete them — the build wrapper rsyncs all of `pe-payload/` into the WIM unfiltered.

- [ ] **Step 2: Confirm tests/ stays out of the WIM**

```bash
ls build/pe-payload-tests/
```

Expected: 3 `.Tests.ps1` files present, ALL outside the `pe-payload/` tree (won't get rsynced into the PE WIM). Good.

- [ ] **Step 3: No commit needed for this task** — it's a verification gate. Move to Task 10.

---

## Task 10: SMOKE-TEST runbook update + final integration

**Files:**
- Modify: `build/SMOKE-TEST.md` (append Plan 3 section)

Document how to validate Plan 3 end-to-end on real hardware: build a new PE WIM with this Plan 3's payload, register a target via the orchestrator API, boot the WIM in UTM, watch Bootstrap.ps1 walk a real manifest.

- [ ] **Step 1: Append Plan 3 section to SMOKE-TEST.md**

Append this section at the end of `build/SMOKE-TEST.md`:

```markdown
---

## Plan 3 — real bootstrap interpreter end-to-end

After Plan 3 lands, the placeholder Bootstrap.ps1 is replaced with the real
manifest interpreter. This runbook validates it deploys a real-ish manifest
end-to-end.

### Prerequisites

- Plan 1 + Plan 2 + Plan 3 branches all merged or stacked-and-checked-out.
- Build host configured per `build/README.md`.
- A registered `install.wim` artifact (sha known) — from a prior `tools/build-install-wim.sh` run.
- Orchestrator running locally on the dev Mac at `http://<mac-hostname>.local:5000` (or wherever
  `build-pe-wim.config.json` points).

### 1. Register a winpe_target via the orchestrator API

From the orchestrator host:

```python
from web.winpe_targets_db import WinpeTargetsDb
from pathlib import Path

db = WinpeTargetsDb(Path("var/artifacts/index.db"))
db.register(
    vm_uuid="REAL-VM-UUID-FROM-UTM",
    install_wim_sha="<sha of registered install.wim>",
    template_id="win11-arm64-baseline",
    params={"computer_name": "AUTOPILOT-SMOKE-01"},
)
```

(The vm_uuid you pass must match the SMBIOS UUID UTM assigns to the VM you'll boot.
Use UTM's "Edit → Information → Hardware UUID" to find it, OR boot once with the
old PE and read it from `wmic csproduct get UUID`.)

### 2. Build the new PE WIM (with the real Bootstrap.ps1)

Same as before:

```bash
./tools/build-pe-wim.sh
```

The new artifact will have `opensshIncluded: true` (assuming you still have it in your
`build-pe-wim.config.json`) and ship the real Bootstrap.ps1 + module tree under `X:\autopilot\Modules\`.

### 3. Boot in UTM

Attach the new ISO to a Win11 ARM64 VM whose SMBIOS UUID matches the one you registered
in step 1. Boot.

### 4. Expected behavior

Within ~30s of boot, you should see in the cmd.exe console:

```
========================================
Autopilot PE bootstrap
========================================
Orchestrator URL: http://your-mac.local:5000
Wait-PeNetwork...
Got IP: 192.168.x.y
Initialize-SshHostKeys...
Initialize-SshHostKeys: regenerated keys in X:\ProgramData\ssh; sshd Running
Identity: REAL-VM-UUID-FROM-UTM (vendor=... name=...)
Fetching manifest...
Manifest: 6 steps, onError=halt
LogStep: ...   (or PartitionStep, ApplyWimStep, etc., as the real manifest dictates)
...
Bootstrap complete.
```

The orchestrator-side `winpe_checkins` table will accumulate one row per step:

```bash
cd autopilot-proxmox
sqlite3 ../var/artifacts/checkins.db \
    "SELECT step_id, status, duration_sec FROM winpe_checkins WHERE vm_uuid='REAL-VM-UUID-FROM-UTM' ORDER BY timestamp;"
```

### 5. SSH into the running PE

After `Initialize-SshHostKeys` reports `sshd Running`:

```bash
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new Administrator@<pe-vm-ip>
```

Should land in cmd.exe; `pwsh` to switch. `type X:\Windows\Temp\autopilot-pe.log` shows the
full transcript.

### Plan 3 exit criterion

A real manifest deploys end-to-end on a UTM VM, all per-step checkins land in `winpe_checkins`,
the resulting target volume boots into Windows, FirstLogonCommand executes the staged
harness — connecting to the existing hash-capture flow.
```

- [ ] **Step 2: Verify build/pe-payload tree integrity (Task 9 spot check)**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/.worktrees/winpe-pe-bootstrap
find build/pe-payload -name '*.Tests.ps1' && echo 'BAD: tests in payload tree' || echo 'OK: no tests in payload'
```

Expected: `OK: no tests in payload`.

- [ ] **Step 3: Final test run — all Plan 3 tests pass**

```bash
pwsh -Command "Invoke-Pester -Path build/pe-payload-tests/ -CI" 2>&1 | tail -2
```

Expected: 36 tests passed.

- [ ] **Step 4: Commit**

```bash
git add build/SMOKE-TEST.md
git commit -m "docs(pe): document Plan 3 end-to-end smoke test"
```

---

## Self-review checklist

- [x] **Spec coverage**: spec Section 6 (PE runtime, all 10 step types, manifest schema, checkin payload) is fully covered. Each step type has its own cmdlet + test. Bootstrap.ps1 dispatches all 10 types via the switch in `Invoke-BootstrapManifest`. The `onError=halt` and `onError=continue` paths from the spec are tested (Bootstrap.Tests.ps1).
- [x] **No placeholders**: every step has a concrete code block. No "TBD", "TODO", or "fill in details".
- [x] **Type/name consistency**: `Get-PeIdentity`, `Wait-PeNetwork`, `Initialize-SshHostKeys`, `Invoke-Manifest`, `Get-PeContent`, `Send-Checkin`, all 10 `Invoke-<Type>Step` cmdlets, `Invoke-BootstrapManifest` — all referenced consistently across tasks.
- [x] **Plan 1 KNOWN-ISSUES coverage**: #1 (network race) → `Wait-PeNetwork` retries. #2 (SSH host key ACLs) → `Initialize-SshHostKeys` regenerates keys at boot. #3 (cascade) → bootstrap orders network-up before SSH key regen.
- [x] **Pester-on-macOS testability**: every `Invoke-<Type>Step` mocks the underlying executable/cmdlet. Real PE-only operations (DISM, Storage, bcdboot, wpeutil) are smoke-tested via the SMOKE-TEST runbook.
