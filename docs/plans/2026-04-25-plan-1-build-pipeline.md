# Plan 1 — Build pipeline + artifact store

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a reproducible PowerShell-driven build pipeline (on a Windows-on-Proxmox VM, reachable over SSH) that produces two artifacts — `install-<arch>-<sha>.wim` (stock Win11 + virtio drivers DISM-injected) and `winpe-autopilot-<arch>-<sha>.{wim,iso}` (custom WinPE with pwsh 7, .NET 8, virtio drivers, payload skeleton) — and register them into a content-addressed artifact store on the orchestrator.

**Architecture:** PowerShell scripts run on the build host via SSH. They emit content-addressed WIM artifacts plus sidecar JSON metadata + cmtrace logs. Dev-Mac shell wrappers handle rsync (PE payload) → ssh-invoke → scp-back → `python -m web.artifact_register` (sha verification, copy into store, sqlite index upsert). Helpers (cmtrace logger, sha256, build lock) live in a shared PowerShell module unit-tested with Pester. The Python register CLI is unit-tested with pytest. Build scripts themselves are integration-tested by running them on the build host.

**Tech Stack:** PowerShell 7 (build host + dev Mac), Pester 5 (PowerShell unit tests), Windows ADK + WinPE add-on (ADK only on the build host), Python 3.12 + pytest (existing in `autopilot-proxmox/`), SQLite (existing pattern), bash + `ssh`/`rsync`/`scp` (dev Mac wrappers).

**Spec reference:** `docs/specs/2026-04-25-winpe-osd-pipeline-design.md` — see Sections 5 (build pipeline) and 8 (artifact storage) for the detailed contracts this plan implements.

**Out of scope (covered by later plans):** Plan 2 (orchestrator API endpoints `/winpe/manifest`, `/winpe/content`, `/winpe/checkin`), Plan 3 (real PE runtime — this plan ships a *placeholder* `Bootstrap.ps1` that prints diagnostics and halts; Plan 3 replaces it with the manifest interpreter), Plan 4 (web UI integration).

---

## File structure

Files this plan creates or modifies:

| File | Purpose |
|---|---|
| `build/` | Top-level dir for all build-pipeline assets (new) |
| `build/README.md` | One-time build-host setup runbook (no code) |
| `build/Build-InstallWim.ps1` | Build script for `install.wim` (run on build host) |
| `build/Build-PeWim.ps1` | Build script for the PE WIM + ISO (run on build host) |
| `build/Modules/Autopilot.Build/Autopilot.Build.psd1` | PowerShell module manifest |
| `build/Modules/Autopilot.Build/Autopilot.Build.psm1` | PowerShell module loader |
| `build/Modules/Autopilot.Build/Public/Write-CmTraceLog.ps1` | cmtrace-format logger |
| `build/Modules/Autopilot.Build/Public/Get-FileSha256.ps1` | sha256 helper |
| `build/Modules/Autopilot.Build/Public/New-BuildLock.ps1` | File-based build lock |
| `build/Modules/Autopilot.Build/Public/Write-ArtifactSidecar.ps1` | Sidecar JSON writer |
| `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1` | Pester unit tests for the module |
| `build/PrePEFlight-Check.ps1` | One-time build-host setup verifier (run after host setup) |
| `build/pe-payload/` | Payload tree dropped into the PE WIM at `X:\autopilot\` (this plan ships the skeleton; Plan 3 fleshes it out) |
| `build/pe-payload/Bootstrap.ps1` | Placeholder bootstrap (prints diagnostics, exits) |
| `build/pe-payload/Bootstrap.json` | Config template (orchestratorUrl placeholder) |
| `build/pe-payload/winpeshl.ini` | `[LaunchApps]` config — runs `wpeinit.exe` only |
| `build/pe-payload/unattend.xml` | windowsPE pass `RunSynchronousCommand` invoking `Bootstrap.ps1` |
| `tools/build-install-wim.sh` | Dev-Mac wrapper: ssh + run + scp + register |
| `tools/build-pe-wim.sh` | Dev-Mac wrapper: rsync payload + ssh + run + scp + register |
| `autopilot-proxmox/web/artifact_register.py` | `python -m web.artifact_register` CLI |
| `autopilot-proxmox/web/artifact_store.py` | Artifact-store paths, sqlite upsert helpers |
| `autopilot-proxmox/web/artifact_sidecar.py` | Sidecar JSON schema validator |
| `autopilot-proxmox/tests/test_artifact_register.py` | pytest for the CLI |
| `autopilot-proxmox/tests/test_artifact_store.py` | pytest for the store |
| `autopilot-proxmox/tests/test_artifact_sidecar.py` | pytest for the sidecar validator |
| `var/artifacts/.gitkeep` | Pin the artifact store directory in git (the store contents are gitignored) |
| `.gitignore` (modify) | Add `var/artifacts/store/`, `var/artifacts/cache/`, `var/artifacts/staging/` |

---

## Task 1: Repo scaffolding + .gitignore

**Files:**
- Create: `build/.gitkeep`
- Create: `var/artifacts/.gitkeep`
- Modify: `.gitignore`

- [ ] **Step 1: Create the directory layout**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot
mkdir -p build/Modules/Autopilot.Build/Public build/Modules/Autopilot.Build/Tests build/pe-payload
mkdir -p var/artifacts/store var/artifacts/cache var/artifacts/staging
touch build/.gitkeep var/artifacts/.gitkeep
```

- [ ] **Step 2: Update .gitignore**

Append to `/Users/Adam.Gell/repo/ProxmoxVEAutopilot/.gitignore`:

```
# Artifact store contents — large binary blobs, never committed
var/artifacts/store/
var/artifacts/cache/
var/artifacts/staging/
!var/artifacts/.gitkeep
```

- [ ] **Step 3: Verify**

Run: `git status --short`
Expected: `build/.gitkeep`, `var/artifacts/.gitkeep`, modified `.gitignore` shown; nothing under `var/artifacts/store/` listed.

- [ ] **Step 4: Commit**

```bash
git add build/.gitkeep var/artifacts/.gitkeep .gitignore
git commit -m "chore(build): scaffold build pipeline directory layout"
```

---

## Task 2: PowerShell module skeleton + Pester setup

**Files:**
- Create: `build/Modules/Autopilot.Build/Autopilot.Build.psd1`
- Create: `build/Modules/Autopilot.Build/Autopilot.Build.psm1`
- Create: `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1`

- [ ] **Step 1: Write the failing test**

Create `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1`:

```powershell
BeforeAll {
    $modulePath = Join-Path $PSScriptRoot '..' 'Autopilot.Build.psd1'
    Import-Module $modulePath -Force
}

Describe 'Autopilot.Build module' {
    It 'imports without error' {
        Get-Module Autopilot.Build | Should -Not -BeNullOrEmpty
    }
    It 'exports no functions yet (skeleton only)' {
        (Get-Command -Module Autopilot.Build).Count | Should -Be 0
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/build/Modules/Autopilot.Build
pwsh -Command "Invoke-Pester -Path Tests/Autopilot.Build.Tests.ps1 -CI"
```

Expected: FAIL with "module manifest not found" or similar import error.

- [ ] **Step 3: Write the module manifest**

Create `build/Modules/Autopilot.Build/Autopilot.Build.psd1`:

```powershell
@{
    RootModule        = 'Autopilot.Build.psm1'
    ModuleVersion     = '0.1.0'
    GUID              = 'b1c1f1aa-7e10-4f9c-9e21-2026042500001'
    Author            = 'ProxmoxVEAutopilot'
    Description       = 'Build-pipeline helpers for Autopilot WIM construction.'
    PowerShellVersion = '7.0'
    FunctionsToExport = @()  # populated as Public/* scripts are added
    CmdletsToExport   = @()
    VariablesToExport = @()
    AliasesToExport   = @()
}
```

- [ ] **Step 4: Write the module loader**

Create `build/Modules/Autopilot.Build/Autopilot.Build.psm1`:

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

- [ ] **Step 5: Run test to verify it passes**

```bash
pwsh -Command "Invoke-Pester -Path build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1 -CI"
```

Expected: 2 tests pass.

- [ ] **Step 6: Commit**

```bash
git add build/Modules/Autopilot.Build/
git commit -m "feat(build): add Autopilot.Build PowerShell module skeleton"
```

---

## Task 3: cmtrace-format logger

**Files:**
- Create: `build/Modules/Autopilot.Build/Public/Write-CmTraceLog.ps1`
- Modify: `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1` (append tests)

cmtrace.exe is the de-facto Microsoft log viewer for SCCM/MDT/DeployR builds. The log format is XML-ish: each line wraps a message with timestamp, severity, source-file, line-number metadata.

- [ ] **Step 1: Write the failing test (append to existing Tests file)**

Append to `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1`:

```powershell
Describe 'Write-CmTraceLog' {
    BeforeEach {
        $script:tmpLog = Join-Path ([System.IO.Path]::GetTempPath()) "Autopilot.Build.test-$([guid]::NewGuid()).log"
    }
    AfterEach {
        if (Test-Path $script:tmpLog) { Remove-Item $script:tmpLog -Force }
    }

    It 'writes a single line in cmtrace format' {
        Write-CmTraceLog -Path $script:tmpLog -Message 'hello' -Severity Info -Component 'Test'
        $content = Get-Content $script:tmpLog -Raw
        $content | Should -Match '<!\[LOG\[hello\]LOG\]!>'
        $content | Should -Match 'component="Test"'
        $content | Should -Match 'type="1"'  # Info = type 1
    }

    It 'maps severity to type correctly (Info=1, Warning=2, Error=3)' {
        Write-CmTraceLog -Path $script:tmpLog -Message 'a' -Severity Info -Component 'T'
        Write-CmTraceLog -Path $script:tmpLog -Message 'b' -Severity Warning -Component 'T'
        Write-CmTraceLog -Path $script:tmpLog -Message 'c' -Severity Error -Component 'T'
        $lines = Get-Content $script:tmpLog
        $lines[0] | Should -Match 'type="1"'
        $lines[1] | Should -Match 'type="2"'
        $lines[2] | Should -Match 'type="3"'
    }

    It 'appends to an existing log without truncating' {
        Write-CmTraceLog -Path $script:tmpLog -Message 'first' -Severity Info -Component 'T'
        Write-CmTraceLog -Path $script:tmpLog -Message 'second' -Severity Info -Component 'T'
        (Get-Content $script:tmpLog).Count | Should -Be 2
    }

    It 'creates the log file if it does not exist' {
        Test-Path $script:tmpLog | Should -BeFalse
        Write-CmTraceLog -Path $script:tmpLog -Message 'x' -Severity Info -Component 'T'
        Test-Path $script:tmpLog | Should -BeTrue
    }
}
```

Also update the skeleton test count expectation:

```powershell
# was: (Get-Command -Module Autopilot.Build).Count | Should -Be 0
(Get-Command -Module Autopilot.Build).Name | Should -Contain 'Write-CmTraceLog'
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot
pwsh -Command "Invoke-Pester -Path build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1 -CI"
```

Expected: 4 new tests fail with "Write-CmTraceLog not recognized."

- [ ] **Step 3: Implement Write-CmTraceLog**

Create `build/Modules/Autopilot.Build/Public/Write-CmTraceLog.ps1`:

```powershell
function Write-CmTraceLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Message,
        [Parameter(Mandatory)] [ValidateSet('Info', 'Warning', 'Error')] [string] $Severity,
        [Parameter(Mandatory)] [string] $Component
    )

    $type = switch ($Severity) {
        'Info'    { 1 }
        'Warning' { 2 }
        'Error'   { 3 }
    }

    $now = Get-Date
    $time = $now.ToString('HH:mm:ss.fff')
    $tzMinutes = [int][System.TimeZoneInfo]::Local.GetUtcOffset($now).TotalMinutes
    $tzSign = if ($tzMinutes -ge 0) { '+' } else { '-' }
    $tzAbs = [Math]::Abs($tzMinutes)
    $timeWithTz = "$time$tzSign$('{0:D3}' -f $tzAbs)"
    $date = $now.ToString('MM-dd-yyyy')

    $line = "<![LOG[$Message]LOG]!><time=`"$timeWithTz`" date=`"$date`" component=`"$Component`" context=`"`" type=`"$type`" thread=`"$PID`" file=`"`">"

    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    Add-Content -Path $Path -Value $line -Encoding utf8
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1 -CI"
```

Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add build/Modules/Autopilot.Build/Public/Write-CmTraceLog.ps1 build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1
git commit -m "feat(build): add cmtrace-format logger"
```

---

## Task 4: SHA256 helper

**Files:**
- Create: `build/Modules/Autopilot.Build/Public/Get-FileSha256.ps1`
- Modify: `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1` (append tests)

PowerShell 7 has `Get-FileHash` built in but it returns a hash object — we want a lowercase hex string with consistent semantics across the build pipeline.

- [ ] **Step 1: Write the failing test**

Append to `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1`:

```powershell
Describe 'Get-FileSha256' {
    It 'returns sha256 of a known string ("hello world\n" → b94d27...)' {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "sha256-test-$([guid]::NewGuid())"
        try {
            [System.IO.File]::WriteAllBytes($tmp, [byte[]](0x68,0x65,0x6c,0x6c,0x6f,0x20,0x77,0x6f,0x72,0x6c,0x64,0x0a))
            (Get-FileSha256 -Path $tmp) | Should -Be 'a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447'
        } finally {
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
    }

    It 'returns lowercase hex' {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "sha256-test-$([guid]::NewGuid())"
        try {
            'x' | Set-Content -Path $tmp -NoNewline -Encoding ascii
            $h = Get-FileSha256 -Path $tmp
            $h | Should -MatchExactly '^[0-9a-f]{64}$'
        } finally {
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
    }

    It 'throws on missing file' {
        { Get-FileSha256 -Path '/no/such/path/abc.bin' } | Should -Throw
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pwsh -Command "Invoke-Pester -Path build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1 -CI"
```

Expected: 3 new tests fail with "Get-FileSha256 not recognized."

- [ ] **Step 3: Implement Get-FileSha256**

Create `build/Modules/Autopilot.Build/Public/Get-FileSha256.ps1`:

```powershell
function Get-FileSha256 {
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)] [string] $Path
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "File not found: $Path"
    }
    $hash = Get-FileHash -LiteralPath $Path -Algorithm SHA256
    return $hash.Hash.ToLowerInvariant()
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1 -CI"
```

Expected: All tests pass (3 new + previous).

- [ ] **Step 5: Commit**

```bash
git add build/Modules/Autopilot.Build/Public/Get-FileSha256.ps1 build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1
git commit -m "feat(build): add Get-FileSha256 helper"
```

---

## Task 5: Build lock helper

**Files:**
- Create: `build/Modules/Autopilot.Build/Public/New-BuildLock.ps1`
- Modify: `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1` (append tests)

The build host serializes builds via a single lock file at `C:\BuildRoot\work\.build.lock`. Lock acquisition writes the script name + PID; release deletes the file. Refuse to start if the lock exists and the recorded PID is alive.

- [ ] **Step 1: Write the failing test**

Append to `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1`:

```powershell
Describe 'New-BuildLock' {
    BeforeEach {
        $script:lockDir = Join-Path ([System.IO.Path]::GetTempPath()) "buildlock-test-$([guid]::NewGuid())"
        New-Item -ItemType Directory -Path $script:lockDir | Out-Null
        $script:lockPath = Join-Path $script:lockDir '.build.lock'
    }
    AfterEach {
        if (Test-Path $script:lockDir) { Remove-Item $script:lockDir -Recurse -Force }
    }

    It 'acquires when no lock file exists' {
        $lock = New-BuildLock -Path $script:lockPath -Owner 'TestScript'
        Test-Path $script:lockPath | Should -BeTrue
        $lock | Should -Not -BeNullOrEmpty
    }

    It 'records owner and PID in the lock file' {
        $null = New-BuildLock -Path $script:lockPath -Owner 'TestScript'
        $content = Get-Content $script:lockPath -Raw | ConvertFrom-Json
        $content.owner | Should -Be 'TestScript'
        $content.pid | Should -Be $PID
    }

    It 'releases via the returned object''s Release() method' {
        $lock = New-BuildLock -Path $script:lockPath -Owner 'TestScript'
        Test-Path $script:lockPath | Should -BeTrue
        $lock.Release()
        Test-Path $script:lockPath | Should -BeFalse
    }

    It 'throws when the lock is held by a live PID' {
        $null = New-BuildLock -Path $script:lockPath -Owner 'First'
        { New-BuildLock -Path $script:lockPath -Owner 'Second' } | Should -Throw -ExpectedMessage '*held*'
    }

    It 'reclaims when the recorded PID is dead' {
        # Forge a stale lock with PID 999999 (very unlikely to exist)
        @{ owner = 'GhostScript'; pid = 999999; acquiredAt = (Get-Date).ToString('o') } |
            ConvertTo-Json | Set-Content -Path $script:lockPath
        $lock = New-BuildLock -Path $script:lockPath -Owner 'Reclaimer'
        $lock | Should -Not -BeNullOrEmpty
        $content = Get-Content $script:lockPath -Raw | ConvertFrom-Json
        $content.owner | Should -Be 'Reclaimer'
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pwsh -Command "Invoke-Pester -Path build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1 -CI"
```

Expected: 5 new tests fail.

- [ ] **Step 3: Implement New-BuildLock**

Create `build/Modules/Autopilot.Build/Public/New-BuildLock.ps1`:

```powershell
function New-BuildLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Owner
    )

    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $existing = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $alive = $false
        try {
            $proc = Get-Process -Id $existing.pid -ErrorAction Stop
            $alive = ($null -ne $proc)
        } catch {
            $alive = $false
        }
        if ($alive) {
            throw "Build lock held by PID $($existing.pid) (owner=$($existing.owner)) since $($existing.acquiredAt). Path=$Path"
        }
        # Stale lock — reclaim
        Remove-Item -LiteralPath $Path -Force
    }

    $payload = @{
        owner      = $Owner
        pid        = $PID
        acquiredAt = (Get-Date).ToString('o')
    } | ConvertTo-Json
    Set-Content -LiteralPath $Path -Value $payload -Encoding utf8

    $lockPath = $Path
    return [pscustomobject]@{
        Path    = $lockPath
        Release = { if (Test-Path -LiteralPath $lockPath) { Remove-Item -LiteralPath $lockPath -Force } }.GetNewClosure()
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1 -CI"
```

Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add build/Modules/Autopilot.Build/Public/New-BuildLock.ps1 build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1
git commit -m "feat(build): add New-BuildLock for serializing builds"
```

---

## Task 6: Sidecar JSON writer

**Files:**
- Create: `build/Modules/Autopilot.Build/Public/Write-ArtifactSidecar.ps1`
- Modify: `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1` (append tests)

Sidecar format is defined in the spec, Section 5 (Build pipeline → install/pe sidecars). Required keys: `kind`, `sha256`, `size`, plus build-specific fields.

- [ ] **Step 1: Write the failing test**

Append to `build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1`:

```powershell
Describe 'Write-ArtifactSidecar' {
    BeforeEach {
        $script:sidecarPath = Join-Path ([System.IO.Path]::GetTempPath()) "sidecar-test-$([guid]::NewGuid()).json"
    }
    AfterEach {
        if (Test-Path $script:sidecarPath) { Remove-Item $script:sidecarPath -Force }
    }

    It 'writes a JSON file with required keys' {
        Write-ArtifactSidecar -Path $script:sidecarPath -Properties @{
            kind = 'install-wim'
            sha256 = 'a' * 64
            size = 1234
            extra = @{ buildHost = 'buildhost' }
        }
        $obj = Get-Content $script:sidecarPath -Raw | ConvertFrom-Json
        $obj.kind   | Should -Be 'install-wim'
        $obj.sha256 | Should -Be ('a' * 64)
        $obj.size   | Should -Be 1234
        $obj.extra.buildHost | Should -Be 'buildhost'
    }

    It 'pretty-prints (indented) JSON' {
        Write-ArtifactSidecar -Path $script:sidecarPath -Properties @{
            kind = 'pe-wim'; sha256 = 'b' * 64; size = 0
        }
        $raw = Get-Content $script:sidecarPath -Raw
        # Pretty-printed JSON contains newlines + indentation
        $raw | Should -Match "`n  "
    }

    It 'throws if Properties lacks required keys' {
        { Write-ArtifactSidecar -Path $script:sidecarPath -Properties @{ kind = 'install-wim' } } |
            Should -Throw -ExpectedMessage '*required*'
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pwsh -Command "Invoke-Pester -Path build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1 -CI"
```

- [ ] **Step 3: Implement Write-ArtifactSidecar**

Create `build/Modules/Autopilot.Build/Public/Write-ArtifactSidecar.ps1`:

```powershell
function Write-ArtifactSidecar {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [hashtable] $Properties
    )
    $required = @('kind', 'sha256', 'size')
    foreach ($key in $required) {
        if (-not $Properties.ContainsKey($key)) {
            throw "Sidecar property '$key' is required."
        }
    }
    $json = $Properties | ConvertTo-Json -Depth 10
    Set-Content -LiteralPath $Path -Value $json -Encoding utf8
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pwsh -Command "Invoke-Pester -Path build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1 -CI"
```

- [ ] **Step 5: Commit**

```bash
git add build/Modules/Autopilot.Build/Public/Write-ArtifactSidecar.ps1 build/Modules/Autopilot.Build/Tests/Autopilot.Build.Tests.ps1
git commit -m "feat(build): add Write-ArtifactSidecar JSON writer"
```

---

## Task 7: Sidecar schema validator (Python)

**Files:**
- Create: `autopilot-proxmox/web/artifact_sidecar.py`
- Create: `autopilot-proxmox/tests/test_artifact_sidecar.py`

Python-side validator for sidecars produced by the PowerShell `Write-ArtifactSidecar`. Must be strict enough to catch schema drift but loose enough to accept the freeform `extra` block.

- [ ] **Step 1: Write the failing test**

Create `autopilot-proxmox/tests/test_artifact_sidecar.py`:

```python
import json
from pathlib import Path

import pytest

from web.artifact_sidecar import (
    ArtifactKind,
    SidecarValidationError,
    load_sidecar,
)


def _write(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "sidecar.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_valid_install_wim_sidecar(tmp_path):
    p = _write(tmp_path, {
        "kind": "install-wim",
        "sha256": "a" * 64,
        "size": 4_500_000_000,
        "edition": "Windows 11 Enterprise",
        "architecture": "arm64",
    })
    sc = load_sidecar(p)
    assert sc.kind is ArtifactKind.INSTALL_WIM
    assert sc.sha256 == "a" * 64
    assert sc.size == 4_500_000_000


def test_valid_pe_wim_sidecar(tmp_path):
    p = _write(tmp_path, {
        "kind": "pe-wim",
        "sha256": "b" * 64,
        "size": 350_000_000,
        "architecture": "arm64",
    })
    sc = load_sidecar(p)
    assert sc.kind is ArtifactKind.PE_WIM


def test_unknown_kind_rejected(tmp_path):
    p = _write(tmp_path, {"kind": "mystery", "sha256": "c" * 64, "size": 1})
    with pytest.raises(SidecarValidationError, match="kind"):
        load_sidecar(p)


def test_missing_sha256_rejected(tmp_path):
    p = _write(tmp_path, {"kind": "install-wim", "size": 1})
    with pytest.raises(SidecarValidationError, match="sha256"):
        load_sidecar(p)


def test_short_sha_rejected(tmp_path):
    p = _write(tmp_path, {"kind": "pe-wim", "sha256": "abcd", "size": 1})
    with pytest.raises(SidecarValidationError, match="sha256"):
        load_sidecar(p)


def test_uppercase_sha_rejected(tmp_path):
    p = _write(tmp_path, {"kind": "pe-wim", "sha256": "A" * 64, "size": 1})
    with pytest.raises(SidecarValidationError, match="lowercase"):
        load_sidecar(p)


def test_negative_size_rejected(tmp_path):
    p = _write(tmp_path, {"kind": "pe-wim", "sha256": "d" * 64, "size": -1})
    with pytest.raises(SidecarValidationError, match="size"):
        load_sidecar(p)


def test_extra_metadata_preserved(tmp_path):
    p = _write(tmp_path, {
        "kind": "install-wim",
        "sha256": "e" * 64,
        "size": 100,
        "buildHost": "buildhost",
        "buildTimestamp": "2026-04-25T12:00:00Z",
        "driversInjected": ["viostor", "NetKVM"],
    })
    sc = load_sidecar(p)
    assert sc.metadata["buildHost"] == "buildhost"
    assert sc.metadata["driversInjected"] == ["viostor", "NetKVM"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
pytest tests/test_artifact_sidecar.py -v
```

Expected: ImportError ("cannot import name 'ArtifactKind' from 'web.artifact_sidecar'").

- [ ] **Step 3: Implement the sidecar module**

Create `autopilot-proxmox/web/artifact_sidecar.py`:

```python
"""Sidecar JSON validation for build artifacts.

A sidecar is the metadata file written next to every WIM by Build-*.ps1.
This module is the single source of truth for the sidecar schema on the
Python side; PowerShell side is `Write-ArtifactSidecar`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ArtifactKind(str, Enum):
    INSTALL_WIM = "install-wim"
    PE_WIM = "pe-wim"
    STAGE_ZIP = "stage-zip"      # for Plan 2's per-VM rendered blobs
    UNATTEND_XML = "unattend-xml" # for Plan 2's per-VM rendered blobs
    DRIVER_ZIP = "driver-zip"     # for Plan 3's per-VM driver-override step


class SidecarValidationError(ValueError):
    pass


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Sidecar:
    kind: ArtifactKind
    sha256: str
    size: int
    metadata: dict


def load_sidecar(path: Path) -> Sidecar:
    """Parse and validate a sidecar JSON file. Raises SidecarValidationError on any issue."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SidecarValidationError("sidecar root must be an object")

    if "kind" not in raw:
        raise SidecarValidationError("sidecar missing required field: kind")
    try:
        kind = ArtifactKind(raw["kind"])
    except ValueError:
        valid = ", ".join(k.value for k in ArtifactKind)
        raise SidecarValidationError(f"unknown kind '{raw['kind']}'; valid: {valid}")

    if "sha256" not in raw:
        raise SidecarValidationError("sidecar missing required field: sha256")
    sha = raw["sha256"]
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        if isinstance(sha, str) and sha.lower() == sha and len(sha) != 64:
            raise SidecarValidationError(f"sha256 must be 64 lowercase hex chars; got {len(sha)}")
        if isinstance(sha, str) and sha.lower() != sha:
            raise SidecarValidationError("sha256 must be lowercase hex")
        raise SidecarValidationError("sha256 must be 64 lowercase hex chars")

    if "size" not in raw:
        raise SidecarValidationError("sidecar missing required field: size")
    size = raw["size"]
    if not isinstance(size, int) or size < 0:
        raise SidecarValidationError(f"size must be a non-negative int; got {size!r}")

    metadata = {k: v for k, v in raw.items() if k not in ("kind", "sha256", "size")}
    return Sidecar(kind=kind, sha256=sha, size=size, metadata=metadata)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_artifact_sidecar.py -v
```

Expected: all 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/artifact_sidecar.py autopilot-proxmox/tests/test_artifact_sidecar.py
git commit -m "feat(artifacts): add sidecar JSON validator"
```

---

## Task 8: Artifact store + sqlite index

**Files:**
- Create: `autopilot-proxmox/web/artifact_store.py`
- Create: `autopilot-proxmox/tests/test_artifact_store.py`

Store responsibilities: own the directory layout (`var/artifacts/store/<sha>.<ext>`), own the sqlite index (`var/artifacts/index.db`), expose idempotent `register_artifact(src_path, sidecar)` and `lookup(sha)` operations.

- [ ] **Step 1: Write the failing test**

Create `autopilot-proxmox/tests/test_artifact_store.py`:

```python
import hashlib
from pathlib import Path

import pytest

from web.artifact_sidecar import ArtifactKind, Sidecar
from web.artifact_store import ArtifactStore


def _make_blob(path: Path, content: bytes) -> tuple[Path, str]:
    path.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    return path, sha


def _make_sidecar(sha: str, size: int, kind: ArtifactKind = ArtifactKind.INSTALL_WIM, **extra) -> Sidecar:
    return Sidecar(kind=kind, sha256=sha, size=size, metadata=extra)


def test_init_creates_directories(tmp_path):
    store = ArtifactStore(tmp_path)
    assert (tmp_path / "store").is_dir()
    assert (tmp_path / "cache").is_dir()
    assert (tmp_path / "index.db").exists()


def test_register_copies_into_store_and_indexes(tmp_path):
    store = ArtifactStore(tmp_path)
    src = tmp_path / "src.wim"
    src, sha = _make_blob(src, b"hello world\n")
    sidecar = _make_sidecar(sha, len(b"hello world\n"), buildHost="x")

    record = store.register(src, sidecar, extension="wim")

    expected_path = tmp_path / "store" / f"{sha}.wim"
    assert expected_path.exists()
    assert record.sha256 == sha
    assert record.relative_path == f"store/{sha}.wim"


def test_register_is_idempotent(tmp_path):
    store = ArtifactStore(tmp_path)
    src = tmp_path / "src.wim"
    src, sha = _make_blob(src, b"abc")
    sidecar = _make_sidecar(sha, 3)
    store.register(src, sidecar, extension="wim")
    store.register(src, sidecar, extension="wim")  # second call is a no-op
    rows = store.list_artifacts()
    assert len(rows) == 1


def test_register_rejects_sha_mismatch(tmp_path):
    store = ArtifactStore(tmp_path)
    src, sha = _make_blob(tmp_path / "src.wim", b"abc")
    bad_sidecar = _make_sidecar("0" * 64, 3)  # wrong sha
    with pytest.raises(ValueError, match="sha256 mismatch"):
        store.register(src, bad_sidecar, extension="wim")


def test_register_rejects_size_mismatch(tmp_path):
    store = ArtifactStore(tmp_path)
    src, sha = _make_blob(tmp_path / "src.wim", b"abc")
    bad_sidecar = _make_sidecar(sha, 999)  # wrong size
    with pytest.raises(ValueError, match="size mismatch"):
        store.register(src, bad_sidecar, extension="wim")


def test_lookup_returns_record(tmp_path):
    store = ArtifactStore(tmp_path)
    src, sha = _make_blob(tmp_path / "src.wim", b"abc")
    sidecar = _make_sidecar(sha, 3, buildHost="b")
    store.register(src, sidecar, extension="wim")

    rec = store.lookup(sha)
    assert rec is not None
    assert rec.sha256 == sha
    assert rec.metadata["buildHost"] == "b"


def test_lookup_returns_none_for_unknown(tmp_path):
    store = ArtifactStore(tmp_path)
    assert store.lookup("0" * 64) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
pytest tests/test_artifact_store.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement the artifact store**

Create `autopilot-proxmox/web/artifact_store.py`:

```python
"""Content-addressed artifact storage with a sqlite index.

Layout:
    <root>/
        store/<sha256>.<ext>     # registered build artifacts (permanent)
        cache/<sha256>.<ext>     # orchestrator-rendered per-VM blobs (LRU; not used in this plan)
        index.db                 # sqlite index

Schema:
    CREATE TABLE artifacts (
        sha256          TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        size            INTEGER NOT NULL,
        relative_path   TEXT NOT NULL,
        metadata_json   TEXT NOT NULL,
        registered_at   TEXT NOT NULL,
        last_served_at  TEXT
    );
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from web.artifact_sidecar import ArtifactKind, Sidecar


@dataclass(frozen=True)
class ArtifactRecord:
    sha256: str
    kind: ArtifactKind
    size: int
    relative_path: str
    metadata: dict
    registered_at: str
    last_served_at: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    sha256          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    size            INTEGER NOT NULL,
    relative_path   TEXT NOT NULL,
    metadata_json   TEXT NOT NULL,
    registered_at   TEXT NOT NULL,
    last_served_at  TEXT
);
"""


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.store_dir = self.root / "store"
        self.cache_dir = self.root / "cache"
        self.db_path = self.root / "index.db"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def register(self, src_path: Path, sidecar: Sidecar, *, extension: str) -> ArtifactRecord:
        """Verify, copy into the store, and index. Idempotent for matching sha."""
        src_path = Path(src_path)
        actual_size = src_path.stat().st_size
        if actual_size != sidecar.size:
            raise ValueError(f"size mismatch: sidecar={sidecar.size} actual={actual_size}")
        actual_sha = self._sha256(src_path)
        if actual_sha != sidecar.sha256:
            raise ValueError(f"sha256 mismatch: sidecar={sidecar.sha256} actual={actual_sha}")

        existing = self.lookup(actual_sha)
        if existing is not None:
            return existing

        rel = f"store/{actual_sha}.{extension.lstrip('.')}"
        dest = self.root / rel
        if not dest.exists():
            shutil.copy2(src_path, dest)

        registered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO artifacts (sha256, kind, size, relative_path, metadata_json, registered_at, last_served_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (sidecar.sha256, sidecar.kind.value, sidecar.size, rel,
                 json.dumps(sidecar.metadata), registered_at),
            )
        return ArtifactRecord(
            sha256=sidecar.sha256,
            kind=sidecar.kind,
            size=sidecar.size,
            relative_path=rel,
            metadata=sidecar.metadata,
            registered_at=registered_at,
            last_served_at=None,
        )

    def lookup(self, sha256: str) -> ArtifactRecord | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT sha256, kind, size, relative_path, metadata_json, registered_at, last_served_at "
                "FROM artifacts WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
        if row is None:
            return None
        return ArtifactRecord(
            sha256=row[0],
            kind=ArtifactKind(row[1]),
            size=row[2],
            relative_path=row[3],
            metadata=json.loads(row[4]),
            registered_at=row[5],
            last_served_at=row[6],
        )

    def list_artifacts(self) -> list[ArtifactRecord]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT sha256, kind, size, relative_path, metadata_json, registered_at, last_served_at "
                "FROM artifacts ORDER BY registered_at DESC"
            ).fetchall()
        return [
            ArtifactRecord(
                sha256=r[0],
                kind=ArtifactKind(r[1]),
                size=r[2],
                relative_path=r[3],
                metadata=json.loads(r[4]),
                registered_at=r[5],
                last_served_at=r[6],
            )
            for r in rows
        ]

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_artifact_store.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/artifact_store.py autopilot-proxmox/tests/test_artifact_store.py
git commit -m "feat(artifacts): add content-addressed store with sqlite index"
```

---

## Task 9: register-artifact CLI

**Files:**
- Create: `autopilot-proxmox/web/artifact_register.py`
- Create: `autopilot-proxmox/tests/test_artifact_register.py`

CLI signature: `python -m web.artifact_register --path <wim> --sidecar <json>` from `autopilot-proxmox/`. Reads sidecar, opens store at `var/artifacts/` (relative to repo root), registers, prints the resulting record.

- [ ] **Step 1: Write the failing test**

Create `autopilot-proxmox/tests/test_artifact_register.py`:

```python
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_registers_artifact(tmp_path):
    artifact_root = tmp_path / "artifacts"
    src = tmp_path / "test.wim"
    content = b"fake wim content"
    src.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()

    sidecar_path = tmp_path / "test.json"
    sidecar_path.write_text(json.dumps({
        "kind": "pe-wim",
        "sha256": sha,
        "size": len(content),
        "buildHost": "test-host",
    }))

    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable, "-m", "web.artifact_register",
            "--path", str(src),
            "--sidecar", str(sidecar_path),
            "--artifact-root", str(artifact_root),
            "--extension", "wim",
        ],
        cwd=repo_root / "autopilot-proxmox",
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
    assert sha in result.stdout
    assert (artifact_root / "store" / f"{sha}.wim").exists()


def test_cli_rejects_sha_mismatch(tmp_path):
    artifact_root = tmp_path / "artifacts"
    src = tmp_path / "test.wim"
    src.write_bytes(b"abc")
    sidecar_path = tmp_path / "test.json"
    sidecar_path.write_text(json.dumps({
        "kind": "pe-wim",
        "sha256": "0" * 64,  # bogus
        "size": 3,
    }))
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable, "-m", "web.artifact_register",
            "--path", str(src),
            "--sidecar", str(sidecar_path),
            "--artifact-root", str(artifact_root),
            "--extension", "wim",
        ],
        cwd=repo_root / "autopilot-proxmox",
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "sha256 mismatch" in result.stderr


def test_cli_rejects_invalid_sidecar(tmp_path):
    artifact_root = tmp_path / "artifacts"
    src = tmp_path / "test.wim"
    src.write_bytes(b"abc")
    sidecar_path = tmp_path / "test.json"
    sidecar_path.write_text(json.dumps({"kind": "mystery", "sha256": "a" * 64, "size": 3}))
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable, "-m", "web.artifact_register",
            "--path", str(src),
            "--sidecar", str(sidecar_path),
            "--artifact-root", str(artifact_root),
            "--extension", "wim",
        ],
        cwd=repo_root / "autopilot-proxmox",
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "unknown kind" in result.stderr.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_artifact_register.py -v
```

Expected: `ModuleNotFoundError: No module named 'web.artifact_register'`.

- [ ] **Step 3: Implement the CLI**

Create `autopilot-proxmox/web/artifact_register.py`:

```python
"""CLI: register a built artifact (WIM/ISO/zip) into the artifact store.

Usage from autopilot-proxmox/:
    python -m web.artifact_register \\
        --path  ../var/artifacts/staging/winpe-autopilot-arm64-<sha>.wim \\
        --sidecar ../var/artifacts/staging/winpe-autopilot-arm64-<sha>.json \\
        --extension wim
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from web.artifact_sidecar import SidecarValidationError, load_sidecar
from web.artifact_store import ArtifactStore


def _default_artifact_root() -> Path:
    # When run from autopilot-proxmox/, repo root is one level up.
    return Path.cwd().parent / "var" / "artifacts"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register a build artifact into the artifact store.")
    parser.add_argument("--path", required=True, type=Path, help="Path to the artifact file (WIM/ISO/zip).")
    parser.add_argument("--sidecar", required=True, type=Path, help="Path to the sidecar JSON.")
    parser.add_argument("--extension", required=True, help="File extension to use in the store (wim, iso, zip).")
    parser.add_argument("--artifact-root", type=Path, default=None,
                        help="Artifact-store root (defaults to ../var/artifacts).")
    args = parser.parse_args(argv)

    artifact_root = args.artifact_root or _default_artifact_root()
    artifact_root.mkdir(parents=True, exist_ok=True)

    try:
        sidecar = load_sidecar(args.sidecar)
    except SidecarValidationError as exc:
        print(f"sidecar validation failed: {exc}", file=sys.stderr)
        return 2

    store = ArtifactStore(artifact_root)
    try:
        record = store.register(args.path, sidecar, extension=args.extension)
    except ValueError as exc:
        print(f"register failed: {exc}", file=sys.stderr)
        return 3

    print(f"registered {record.kind.value} {record.sha256}")
    print(f"  size:           {record.size}")
    print(f"  relative_path:  {record.relative_path}")
    print(f"  registered_at:  {record.registered_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_artifact_register.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/artifact_register.py autopilot-proxmox/tests/test_artifact_register.py
git commit -m "feat(artifacts): add register-artifact CLI"
```

---

## Task 10: PE payload skeleton

**Files:**
- Create: `build/pe-payload/Bootstrap.ps1`
- Create: `build/pe-payload/Bootstrap.json`
- Create: `build/pe-payload/winpeshl.ini`
- Create: `build/pe-payload/unattend.xml.template`

The payload skeleton ships a placeholder `Bootstrap.ps1` that prints diagnostics and halts. Plan 3 will replace it with the real manifest interpreter. The other files (`winpeshl.ini`, `unattend.xml.template`) are final and Plan 3 will not change them.

`unattend.xml.template` uses `__ARCHITECTURE__` as a placeholder that `Build-PeWim.ps1` substitutes at build time (so one template serves both arm64 and x64 builds).

- [ ] **Step 1: Create Bootstrap.ps1 (placeholder)**

Create `build/pe-payload/Bootstrap.ps1`:

```powershell
# Placeholder PE bootstrap. Plan 3 replaces this with the manifest interpreter.
# Runs from unattend.xml's RunSynchronousCommand (windowsPE pass).

$transcript = 'X:\Windows\Temp\autopilot-pe.log'
Start-Transcript -Path $transcript -Append -Force | Out-Null

Write-Host '========================================'
Write-Host 'Autopilot PE bootstrap (placeholder)'
Write-Host '========================================'
Write-Host "PowerShell version : $($PSVersionTable.PSVersion)"
Write-Host "Edition            : $($PSVersionTable.PSEdition)"
Write-Host "OS                 : $([System.Environment]::OSVersion.VersionString)"
Write-Host "Architecture       : $env:PROCESSOR_ARCHITECTURE"
Write-Host "Hostname           : $env:COMPUTERNAME"

try {
    $cs = Get-CimInstance Win32_ComputerSystemProduct -ErrorAction Stop
    Write-Host "SMBIOS UUID        : $($cs.UUID)"
    Write-Host "Vendor             : $($cs.Vendor)"
    Write-Host "Name               : $($cs.Name)"
} catch {
    Write-Host "SMBIOS UUID        : <Get-CimInstance failed: $_>"
}

if (Test-Path 'X:\autopilot\Bootstrap.json') {
    $config = Get-Content 'X:\autopilot\Bootstrap.json' -Raw | ConvertFrom-Json
    Write-Host "Orchestrator URL   : $($config.orchestratorUrl)"
} else {
    Write-Host "Orchestrator URL   : <Bootstrap.json not found>"
}

Write-Host ''
Write-Host 'Placeholder bootstrap complete. Plan 3 will replace this.'
Write-Host 'Dropping to interactive shell for inspection.'

Stop-Transcript | Out-Null

# Leave an interactive prompt so the operator can verify PE comes up correctly.
# Plan 3 replaces this with the manifest dispatch loop.
```

- [ ] **Step 2: Create Bootstrap.json template**

Create `build/pe-payload/Bootstrap.json`:

```json
{
  "version": 1,
  "orchestratorUrl": "__ORCHESTRATOR_URL__",
  "networkTimeoutSec": 60,
  "manifestRetries": 3,
  "manifestRetryBackoffSec": 5,
  "checkinRetries": 2,
  "debug": false
}
```

`__ORCHESTRATOR_URL__` is replaced by `Build-PeWim.ps1` at build time using the `orchestratorUrl` build-config field.

- [ ] **Step 3: Create winpeshl.ini**

Create `build/pe-payload/winpeshl.ini`:

```ini
[LaunchApps]
%SYSTEMROOT%\System32\wpeinit.exe
```

(Final form; bootstrap is invoked from unattend.xml after wpeinit completes.)

- [ ] **Step 4: Create unattend.xml.template**

Create `build/pe-payload/unattend.xml.template`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-Setup"
               processorArchitecture="__ARCHITECTURE__"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <EnableNetwork>false</EnableNetwork>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Description>Autopilot PE Bootstrap</Description>
          <Order>1</Order>
          <Path>"X:\Program Files\PowerShell\7\pwsh.exe" -ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File X:\autopilot\Bootstrap.ps1</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
</unattend>
```

- [ ] **Step 5: Commit**

```bash
git add build/pe-payload/
git commit -m "feat(build): add PE payload skeleton (placeholder Bootstrap, winpeshl, unattend)"
```

---

## Task 11: Build-InstallWim.ps1

**Files:**
- Create: `build/Build-InstallWim.ps1`

Single PowerShell script that builds `install.wim`. Run on the build host as `pwsh -File Build-InstallWim.ps1 -Config -` with the build-config JSON on stdin. No unit tests — orchestrates DISM cmdlets that need real ISOs and admin rights. Verified by running on the build host (Task 14 smoke-test runbook).

- [ ] **Step 1: Create the script**

Create `build/Build-InstallWim.ps1`:

```powershell
#Requires -Version 7
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Build install.wim: extract from stock Windows ISO, DISM-inject virtio drivers, export.

.DESCRIPTION
    Reads a build-config JSON from -ConfigJson (a path) or - (stdin).
    Required fields: windowsIsoPath, virtioIsoPath, edition, architecture, drivers, outputDir.
    Optional: lockPath (default C:\BuildRoot\work\.build.lock).

    Produces in outputDir:
        install-<edition-slug>-<arch>-<sha>.wim
        install-<edition-slug>-<arch>-<sha>.json   (sidecar)
        install-<edition-slug>-<arch>-<sha>.log    (cmtrace)
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $ConfigJson
)

$ErrorActionPreference = 'Stop'

# ---- Load module ----
Import-Module (Join-Path $PSScriptRoot 'Modules\Autopilot.Build\Autopilot.Build.psd1') -Force

# ---- Parse config ----
$rawJson = if ($ConfigJson -eq '-') { [Console]::In.ReadToEnd() } else { Get-Content -LiteralPath $ConfigJson -Raw }
$config = $rawJson | ConvertFrom-Json

foreach ($key in @('windowsIsoPath','virtioIsoPath','edition','architecture','drivers','outputDir')) {
    if (-not $config.PSObject.Properties.Match($key)) {
        throw "Build config missing required field: $key"
    }
}

$lockPath = if ($config.PSObject.Properties.Match('lockPath')) { $config.lockPath } else { 'C:\BuildRoot\work\.build.lock' }
$workDir  = Split-Path -Parent $lockPath
if (-not (Test-Path $workDir)) { New-Item -ItemType Directory -Path $workDir -Force | Out-Null }

# Edition slug: lowercased with non-alnum replaced by '-'
$editionSlug = ($config.edition -replace '[^A-Za-z0-9]+', '-').Trim('-').ToLowerInvariant()
$arch        = $config.architecture
$tempName    = "install-$editionSlug-$arch-staging.wim"
$tempPath    = Join-Path $workDir $tempName
$mountPath   = Join-Path $workDir "mount-install-$arch"
if (Test-Path $mountPath) { Remove-Item $mountPath -Recurse -Force }
New-Item -ItemType Directory -Path $mountPath -Force | Out-Null

# ---- Acquire lock ----
$lock = New-BuildLock -Path $lockPath -Owner 'Build-InstallWim'
try {
    # ---- Set up logging ----
    $logTempPath = Join-Path $workDir "install-$editionSlug-$arch-staging.log"
    if (Test-Path $logTempPath) { Remove-Item $logTempPath -Force }
    Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Build start. edition=$($config.edition) arch=$arch"

    # ---- Mount ISOs ----
    Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Mounting Windows ISO: $($config.windowsIsoPath)"
    $winMount = Mount-DiskImage -ImagePath $config.windowsIsoPath -PassThru
    $winLetter = ($winMount | Get-Volume).DriveLetter
    Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Windows ISO mounted at ${winLetter}:"

    Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Mounting virtio ISO: $($config.virtioIsoPath)"
    $virtioMount = Mount-DiskImage -ImagePath $config.virtioIsoPath -PassThru
    $virtioLetter = ($virtioMount | Get-Volume).DriveLetter
    Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "virtio ISO mounted at ${virtioLetter}:"

    try {
        # ---- Locate install.wim and edition index ----
        $srcWim = "${winLetter}:\sources\install.wim"
        if (-not (Test-Path $srcWim)) { throw "install.wim not found at $srcWim" }

        Copy-Item -Path $srcWim -Destination $tempPath -Force
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Copied $srcWim → $tempPath"

        $images = Get-WindowsImage -ImagePath $tempPath
        $match = $images | Where-Object { $_.ImageName -eq $config.edition }
        if (-not $match) {
            $available = ($images | ForEach-Object { $_.ImageName }) -join ', '
            throw "Edition '$($config.edition)' not found in install.wim. Available: $available"
        }
        $editionIndex = $match.ImageIndex
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Selected edition '$($config.edition)' at index $editionIndex"

        # ---- Mount the WIM ----
        Mount-WindowsImage -Path $mountPath -ImagePath $tempPath -Index $editionIndex | Out-Null
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Mounted install.wim at $mountPath"

        # ---- Inject drivers ----
        foreach ($driver in $config.drivers) {
            $driverPath = "${virtioLetter}:\$driver\w11\$arch"
            if (-not (Test-Path $driverPath)) {
                Write-CmTraceLog -Path $logTempPath -Severity Warning -Component 'Build-InstallWim' -Message "Driver path missing, skipping: $driverPath"
                continue
            }
            Add-WindowsDriver -Path $mountPath -Driver $driverPath -Recurse -ForceUnsigned | Out-Null
            Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Injected driver: $driver from $driverPath"
        }

        # ---- Dismount + commit ----
        Dismount-WindowsImage -Path $mountPath -Save | Out-Null
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Dismounted (committed)"

        # ---- Export to compact ----
        $exportTempPath = Join-Path $workDir "install-$editionSlug-$arch-export.wim"
        if (Test-Path $exportTempPath) { Remove-Item $exportTempPath -Force }
        Export-WindowsImage -SourceImagePath $tempPath -SourceIndex $editionIndex -DestinationImagePath $exportTempPath | Out-Null
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Exported to $exportTempPath"

        # ---- Hash + final filenames ----
        $sha  = Get-FileSha256 -Path $exportTempPath
        $size = (Get-Item $exportTempPath).Length
        $finalWim = Join-Path $config.outputDir "install-$editionSlug-$arch-$sha.wim"
        $finalJson= Join-Path $config.outputDir "install-$editionSlug-$arch-$sha.json"
        $finalLog = Join-Path $config.outputDir "install-$editionSlug-$arch-$sha.log"
        if (-not (Test-Path $config.outputDir)) { New-Item -ItemType Directory -Path $config.outputDir -Force | Out-Null }
        Move-Item -Path $exportTempPath -Destination $finalWim -Force

        # ---- Sidecar ----
        $sidecar = @{
            kind             = 'install-wim'
            sha256           = $sha
            size             = $size
            edition          = $config.edition
            architecture     = $arch
            sourceWindowsIso = @{
                path   = $config.windowsIsoPath
                sha256 = (Get-FileSha256 -Path $config.windowsIsoPath)
            }
            sourceVirtioIso  = @{
                path   = $config.virtioIsoPath
                sha256 = (Get-FileSha256 -Path $config.virtioIsoPath)
            }
            driversInjected  = $config.drivers
            buildHost        = $env:COMPUTERNAME
            buildTimestamp   = (Get-Date).ToUniversalTime().ToString('o')
            builderScript    = 'Build-InstallWim.ps1'
        }
        Write-ArtifactSidecar -Path $finalJson -Properties $sidecar
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Wrote sidecar $finalJson"

        # ---- Move log to final ----
        Move-Item -Path $logTempPath -Destination $finalLog -Force

        Write-Host "BUILD OK"
        Write-Host "WIM:     $finalWim"
        Write-Host "Sidecar: $finalJson"
        Write-Host "Log:     $finalLog"
        Write-Host "sha256:  $sha"
        Write-Host "size:    $size"
    } finally {
        # ---- Cleanup mounted WIM if still mounted ----
        try { Dismount-WindowsImage -Path $mountPath -Discard -ErrorAction SilentlyContinue | Out-Null } catch {}
        if (Test-Path $tempPath) { Remove-Item $tempPath -Force -ErrorAction SilentlyContinue }
        if (Test-Path $mountPath) { Remove-Item $mountPath -Recurse -Force -ErrorAction SilentlyContinue }

        # ---- Cleanup ISOs ----
        try { Dismount-DiskImage -ImagePath $config.windowsIsoPath -ErrorAction SilentlyContinue | Out-Null } catch {}
        try { Dismount-DiskImage -ImagePath $config.virtioIsoPath  -ErrorAction SilentlyContinue | Out-Null } catch {}
    }
} finally {
    $lock.Release.Invoke()
}
```

- [ ] **Step 2: Verify the script parses (syntax check, no execution)**

```bash
pwsh -NoProfile -Command "Get-Command -Syntax (Get-Item ./build/Build-InstallWim.ps1).FullName 2>&1 || pwsh -NoProfile -File ./build/Build-InstallWim.ps1 -ConfigJson nonexistent.json"
```

Expected: parses OK; runtime error about missing config file (proves the script loaded). On macOS `Mount-DiskImage` won't exist, so the script will fail at runtime — that's fine, we're verifying it parses.

Better: just run `pwsh -NoProfile -Command "& { . ./build/Build-InstallWim.ps1 } 2>&1 | head"` — should show parameter-binding error (missing -ConfigJson), not a parse error.

- [ ] **Step 3: Commit**

```bash
git add build/Build-InstallWim.ps1
git commit -m "feat(build): add Build-InstallWim.ps1 (extract + driver inject + sidecar)"
```

---

## Task 12: Build-PeWim.ps1

**Files:**
- Create: `build/Build-PeWim.ps1`

Single PowerShell script that builds the PE WIM and the bootable ISO. Run on the build host with build-config JSON on stdin. The script is long but linear; broken into clearly-commented phases.

- [ ] **Step 1: Create the script**

Create `build/Build-PeWim.ps1`:

```powershell
#Requires -Version 7
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Build winpe-autopilot-<arch>-<sha>.{wim,iso}: ADK winpe.wim + packages + drivers + pwsh7 + .NET 8 + payload.

.DESCRIPTION
    Reads a build-config JSON from -ConfigJson (a path) or - (stdin).
    Required fields: adkRoot, architecture, virtioIsoPath, drivers, pwsh7Zip,
                     dotnetRuntimeZip, payloadDir, orchestratorUrl, outputDir.

    Produces in outputDir:
        winpe-autopilot-<arch>-<sha>.wim
        winpe-autopilot-<arch>-<sha>.iso
        winpe-autopilot-<arch>-<sha>.json
        winpe-autopilot-<arch>-<sha>.log
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $ConfigJson
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Modules\Autopilot.Build\Autopilot.Build.psd1') -Force

# ---- Parse config ----
$rawJson = if ($ConfigJson -eq '-') { [Console]::In.ReadToEnd() } else { Get-Content -LiteralPath $ConfigJson -Raw }
$config = $rawJson | ConvertFrom-Json
foreach ($key in @('adkRoot','architecture','virtioIsoPath','drivers','pwsh7Zip','dotnetRuntimeZip','payloadDir','orchestratorUrl','outputDir')) {
    if (-not $config.PSObject.Properties.Match($key)) { throw "Build config missing required field: $key" }
}

$lockPath = if ($config.PSObject.Properties.Match('lockPath')) { $config.lockPath } else { 'C:\BuildRoot\work\.build.lock' }
$workDir  = Split-Path -Parent $lockPath
if (-not (Test-Path $workDir)) { New-Item -ItemType Directory -Path $workDir -Force | Out-Null }

$arch       = $config.architecture
$peStaging  = Join-Path $workDir "winpe-$arch-staging.wim"
$peMount    = Join-Path $workDir "mount-pe-$arch"
$mediaDir   = Join-Path $workDir "media-pe-$arch"
if (Test-Path $peMount)  { Remove-Item $peMount -Recurse -Force }
if (Test-Path $mediaDir) { Remove-Item $mediaDir -Recurse -Force }
New-Item -ItemType Directory -Path $peMount  -Force | Out-Null
New-Item -ItemType Directory -Path $mediaDir -Force | Out-Null

$lock = New-BuildLock -Path $lockPath -Owner 'Build-PeWim'
try {
    $logTempPath = Join-Path $workDir "winpe-autopilot-$arch-staging.log"
    if (Test-Path $logTempPath) { Remove-Item $logTempPath -Force }
    function Log([string]$Severity, [string]$Message) {
        Write-CmTraceLog -Path $logTempPath -Severity $Severity -Component 'Build-PeWim' -Message $Message
    }
    Log 'Info' "Build start. arch=$arch orchestratorUrl=$($config.orchestratorUrl)"

    # ---- Phase 1: Copy + mount ADK winpe.wim ----
    $adkWinPe = Join-Path $config.adkRoot "Assessment and Deployment Kit\Windows Preinstallation Environment\$arch\en-us\winpe.wim"
    if (-not (Test-Path $adkWinPe)) { throw "ADK winpe.wim not found: $adkWinPe" }
    Copy-Item -Path $adkWinPe -Destination $peStaging -Force
    Log 'Info' "Copied ADK winpe.wim → $peStaging"
    Mount-WindowsImage -Path $peMount -ImagePath $peStaging -Index 1 | Out-Null
    Log 'Info' "Mounted at $peMount"

    try {
        # ---- Phase 2: Add ADK packages ----
        $ocsRoot = Join-Path $config.adkRoot "Assessment and Deployment Kit\Windows Preinstallation Environment\$arch\WinPE_OCs"
        $packagesToAdd = @(
            'WinPE-WMI', 'WinPE-NetFX', 'WinPE-PowerShell',
            'WinPE-StorageWMI', 'WinPE-EnhancedStorage', 'WinPE-DismCmdlets',
            'WinPE-SecureStartup', 'WinPE-SecureBootCmdlets'
        )
        foreach ($pkg in $packagesToAdd) {
            $base = Join-Path $ocsRoot "$pkg.cab"
            $loc  = Join-Path $ocsRoot "en-us\${pkg}_en-us.cab"
            if (-not (Test-Path $base)) { throw "ADK package not found: $base" }
            Add-WindowsPackage -Path $peMount -PackagePath $base | Out-Null
            if (Test-Path $loc) { Add-WindowsPackage -Path $peMount -PackagePath $loc | Out-Null }
            Log 'Info' "Added package: $pkg"
        }

        # ---- Phase 3: Inject virtio drivers ----
        $virtioMount = Mount-DiskImage -ImagePath $config.virtioIsoPath -PassThru
        $virtioLetter = ($virtioMount | Get-Volume).DriveLetter
        try {
            foreach ($driver in $config.drivers) {
                $driverPath = "${virtioLetter}:\$driver\w11\$arch"
                if (-not (Test-Path $driverPath)) {
                    Log 'Warning' "Driver path missing, skipping: $driverPath"
                    continue
                }
                Add-WindowsDriver -Path $peMount -Driver $driverPath -Recurse -ForceUnsigned | Out-Null
                Log 'Info' "Injected driver: $driver"
            }
        } finally {
            try { Dismount-DiskImage -ImagePath $config.virtioIsoPath -ErrorAction SilentlyContinue | Out-Null } catch {}
        }

        # ---- Phase 4: Drop in .NET 8 runtime + pwsh 7 ----
        $dotnetTarget = Join-Path $peMount 'Program Files\dotnet'
        if (-not (Test-Path $dotnetTarget)) { New-Item -ItemType Directory -Path $dotnetTarget -Force | Out-Null }
        Expand-Archive -Path $config.dotnetRuntimeZip -DestinationPath $dotnetTarget -Force
        Log 'Info' "Extracted .NET 8 runtime → $dotnetTarget"

        $pwshTarget = Join-Path $peMount 'Program Files\PowerShell\7'
        if (-not (Test-Path $pwshTarget)) { New-Item -ItemType Directory -Path $pwshTarget -Force | Out-Null }
        Expand-Archive -Path $config.pwsh7Zip -DestinationPath $pwshTarget -Force
        Log 'Info' "Extracted pwsh 7 → $pwshTarget"

        # Discover dotnet version (used in registry below)
        $dotnetVersion = Get-ChildItem -Path (Join-Path $dotnetTarget 'shared\Microsoft.NETCore.App') -Directory |
                         Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty Name
        if (-not $dotnetVersion) { throw "Could not discover .NET 8 version from extracted runtime." }
        Log 'Info' ".NET runtime version discovered: $dotnetVersion"

        # ---- Phase 5: Strip PS 5.1 binaries (DeployR pattern) ----
        $ps51Path = Join-Path $peMount 'Windows\System32\WindowsPowerShell\v1.0'
        if (Test-Path $ps51Path) {
            cmd.exe /c "takeown /f `"$ps51Path\*.*`" >nul 2>&1"
            cmd.exe /c "icacls `"$ps51Path\*.*`" /grant everyone:f >nul 2>&1"
            Get-ChildItem -Path "$ps51Path\*.*" -File | Remove-Item -Force
            Log 'Info' "Stripped PS 5.1 binaries (kept v1.0\Modules)"
        }

        # ---- Phase 6: Offline registry edits ----
        $sysHive = Join-Path $peMount 'Windows\System32\config\SYSTEM'
        $swHive  = Join-Path $peMount 'Windows\System32\config\SOFTWARE'
        cmd.exe /c "reg.exe load HKLM\PESystem `"$sysHive`" >nul"   ; if ($LASTEXITCODE -ne 0) { throw 'reg load PESystem failed' }
        cmd.exe /c "reg.exe load HKLM\PESoftware `"$swHive`" >nul"  ; if ($LASTEXITCODE -ne 0) { throw 'reg load PESoftware failed' }
        try {
            $shortArch = if ($arch -eq 'amd64') { 'x64' } else { $arch }
            $sharedHostKey = "HKLM:\PESoftware\dotnet\Setup\InstalledVersions\$shortArch\sharedhost"
            New-Item -Path $sharedHostKey -Force | Out-Null
            New-ItemProperty -Path $sharedHostKey -Name Path -Value 'X:\Program Files\dotnet\' -PropertyType String -Force | Out-Null
            New-ItemProperty -Path $sharedHostKey -Name Version -Value $dotnetVersion -PropertyType String -Force | Out-Null

            $envKey = 'HKLM:\PESystem\ControlSet001\Control\Session Manager\Environment'
            $existingPath = (Get-Item -Path $envKey).GetValue('Path', $null, 'DoNotExpandEnvironmentNames')
            $newPath = "$existingPath;X:\Program Files\dotnet\;X:\Program Files\PowerShell\7"
            Set-ItemProperty -Path $envKey -Name Path -Value $newPath -Type ExpandString | Out-Null

            $existingPsmp = (Get-Item -Path $envKey).GetValue('PSModulePath', '', 'DoNotExpandEnvironmentNames')
            $newPsmp = "$existingPsmp;%ProgramFiles%\PowerShell\;%ProgramFiles%\PowerShell\7\;%SystemRoot%\system32\config\systemprofile\Documents\PowerShell\Modules\"
            New-ItemProperty -Path $envKey -Name PSModulePath -PropertyType ExpandString -Value $newPsmp -Force | Out-Null

            New-ItemProperty -Path $envKey -Name APPDATA      -PropertyType ExpandString -Value '%SystemRoot%\System32\Config\SystemProfile\AppData\Roaming' -Force | Out-Null
            New-ItemProperty -Path $envKey -Name HOMEDRIVE    -PropertyType ExpandString -Value '%SystemDrive%' -Force | Out-Null
            New-ItemProperty -Path $envKey -Name HOMEPATH     -PropertyType ExpandString -Value '%SystemRoot%\System32\Config\SystemProfile' -Force | Out-Null
            New-ItemProperty -Path $envKey -Name LOCALAPPDATA -PropertyType ExpandString -Value '%SystemRoot%\System32\Config\SystemProfile\AppData\Local' -Force | Out-Null
            New-ItemProperty -Path $envKey -Name POWERSHELL_UPDATECHECK -PropertyType String -Value 'LTS' -Force | Out-Null

            $tcpKey = 'HKLM:\PESystem\ControlSet001\Services\Tcpip\Parameters'
            New-ItemProperty -Path $tcpKey -Name TcpTimedWaitDelay -PropertyType DWord -Value 30 -Force | Out-Null
            New-ItemProperty -Path $tcpKey -Name MaxUserPort       -PropertyType DWord -Value 65534 -Force | Out-Null

            Log 'Info' "Applied offline registry edits"
        } finally {
            [GC]::Collect()
            cmd.exe /c "reg.exe unload HKLM\PESoftware >nul"  | Out-Null
            cmd.exe /c "reg.exe unload HKLM\PESystem >nul"    | Out-Null
        }

        # ---- Phase 7: Stage payload ----
        $payloadTarget = Join-Path $peMount 'autopilot'
        if (-not (Test-Path $payloadTarget)) { New-Item -ItemType Directory -Path $payloadTarget -Force | Out-Null }
        Copy-Item -Path (Join-Path $config.payloadDir '*') -Destination $payloadTarget -Recurse -Force

        # Render Bootstrap.json (substitute orchestratorUrl)
        $bootstrapJsonPath = Join-Path $payloadTarget 'Bootstrap.json'
        $rendered = (Get-Content -LiteralPath $bootstrapJsonPath -Raw) -replace '__ORCHESTRATOR_URL__', $config.orchestratorUrl
        Set-Content -LiteralPath $bootstrapJsonPath -Value $rendered -Encoding utf8

        # Render unattend.xml (substitute architecture)
        $unattendTpl = Join-Path $payloadTarget 'unattend.xml.template'
        $unattendOut = Join-Path $peMount 'unattend.xml'
        $unattendRendered = (Get-Content -LiteralPath $unattendTpl -Raw) -replace '__ARCHITECTURE__', $arch
        Set-Content -LiteralPath $unattendOut -Value $unattendRendered -Encoding utf8
        Remove-Item -Path $unattendTpl -Force  # template stays out of the WIM

        # winpeshl.ini → System32
        Copy-Item -Path (Join-Path $payloadTarget 'winpeshl.ini') -Destination (Join-Path $peMount 'Windows\System32\winpeshl.ini') -Force
        Remove-Item -Path (Join-Path $payloadTarget 'winpeshl.ini') -Force  # not needed in payload tree post-build

        Log 'Info' "Staged payload at X:\autopilot and rendered unattend.xml"

        # ---- Phase 8: Dismount + commit ----
        Dismount-WindowsImage -Path $peMount -Save | Out-Null
        Log 'Info' "Dismounted (committed)"

        # ---- Phase 9: Export to compact ----
        $exportTempPath = Join-Path $workDir "winpe-autopilot-$arch-export.wim"
        if (Test-Path $exportTempPath) { Remove-Item $exportTempPath -Force }
        Export-WindowsImage -SourceImagePath $peStaging -SourceIndex 1 -DestinationImagePath $exportTempPath | Out-Null
        Log 'Info' "Exported to $exportTempPath"

        # ---- Phase 10: Hash + final WIM ----
        $sha   = Get-FileSha256 -Path $exportTempPath
        $size  = (Get-Item $exportTempPath).Length
        $finalWim  = Join-Path $config.outputDir "winpe-autopilot-$arch-$sha.wim"
        $finalIso  = Join-Path $config.outputDir "winpe-autopilot-$arch-$sha.iso"
        $finalJson = Join-Path $config.outputDir "winpe-autopilot-$arch-$sha.json"
        $finalLog  = Join-Path $config.outputDir "winpe-autopilot-$arch-$sha.log"
        if (-not (Test-Path $config.outputDir)) { New-Item -ItemType Directory -Path $config.outputDir -Force | Out-Null }
        Move-Item -Path $exportTempPath -Destination $finalWim -Force

        # ---- Phase 11: Build ISO with efisys_noprompt.bin ----
        $oscdimgDir = Join-Path $config.adkRoot "Assessment and Deployment Kit\Deployment Tools\$arch\Oscdimg"
        $oscdimg    = Join-Path $oscdimgDir 'oscdimg.exe'
        $efiNoPrompt = Join-Path $oscdimgDir 'efisys_noprompt.bin'
        if (-not (Test-Path $oscdimg))    { throw "oscdimg not found: $oscdimg" }
        if (-not (Test-Path $efiNoPrompt)){ throw "efisys_noprompt.bin not found: $efiNoPrompt" }

        # Assemble media tree: copy ADK media template (boot files), drop our WIM at \Sources\boot.wim
        $adkMedia = Join-Path $config.adkRoot "Assessment and Deployment Kit\Windows Preinstallation Environment\$arch\Media"
        if (-not (Test-Path $adkMedia)) { throw "ADK Media template not found: $adkMedia" }
        Copy-Item -Path (Join-Path $adkMedia '*') -Destination $mediaDir -Recurse -Force
        $sourcesDir = Join-Path $mediaDir 'Sources'
        if (-not (Test-Path $sourcesDir)) { New-Item -ItemType Directory -Path $sourcesDir -Force | Out-Null }
        Copy-Item -Path $finalWim -Destination (Join-Path $sourcesDir 'boot.wim') -Force

        # ARM64 is UEFI-only; -bootdata:1#pEF... = single (UEFI) boot sector with efisys_noprompt
        $oscdimgArgs = @(
            '-m','-o','-u2','-udfver102',
            "-bootdata:1#pEF,e,b$efiNoPrompt",
            $mediaDir,
            $finalIso
        )
        & $oscdimg @oscdimgArgs
        if ($LASTEXITCODE -ne 0) { throw "oscdimg failed with exit code $LASTEXITCODE" }
        Log 'Info' "ISO built: $finalIso"

        # ---- Phase 12: Sidecar + log ----
        $sidecar = @{
            kind             = 'pe-wim'
            sha256           = $sha
            size             = $size
            architecture     = $arch
            adkRoot          = $config.adkRoot
            orchestratorUrl  = $config.orchestratorUrl
            sourceVirtioIso  = @{
                path   = $config.virtioIsoPath
                sha256 = (Get-FileSha256 -Path $config.virtioIsoPath)
            }
            driversInjected   = $config.drivers
            packagesAdded     = $packagesToAdd
            dotnetVersion     = $dotnetVersion
            pwsh7ZipSha       = (Get-FileSha256 -Path $config.pwsh7Zip)
            payloadDirSnapshot= (Get-ChildItem -Path $config.payloadDir -Recurse -File | ForEach-Object { $_.FullName.Substring($config.payloadDir.Length).TrimStart('\','/') })
            buildHost         = $env:COMPUTERNAME
            buildTimestamp    = (Get-Date).ToUniversalTime().ToString('o')
            builderScript     = 'Build-PeWim.ps1'
            isoPath           = $finalIso
            isoSha256         = (Get-FileSha256 -Path $finalIso)
            isoSize           = (Get-Item $finalIso).Length
        }
        Write-ArtifactSidecar -Path $finalJson -Properties $sidecar
        Move-Item -Path $logTempPath -Destination $finalLog -Force
        Log 'Info' "Wrote sidecar $finalJson"

        Write-Host "BUILD OK"
        Write-Host "WIM:     $finalWim"
        Write-Host "ISO:     $finalIso"
        Write-Host "Sidecar: $finalJson"
        Write-Host "Log:     $finalLog"
        Write-Host "sha256:  $sha"
        Write-Host "size:    $size"
    } finally {
        try { Dismount-WindowsImage -Path $peMount -Discard -ErrorAction SilentlyContinue | Out-Null } catch {}
        if (Test-Path $peStaging) { Remove-Item $peStaging -Force -ErrorAction SilentlyContinue }
        if (Test-Path $peMount)   { Remove-Item $peMount   -Recurse -Force -ErrorAction SilentlyContinue }
        if (Test-Path $mediaDir)  { Remove-Item $mediaDir  -Recurse -Force -ErrorAction SilentlyContinue }
    }
} finally {
    $lock.Release.Invoke()
}
```

- [ ] **Step 2: Verify the script parses**

```bash
pwsh -NoProfile -Command "& { . ./build/Build-PeWim.ps1 } 2>&1 | head -5"
```

Expected: parameter-binding error about missing `-ConfigJson`. Confirms parse + module import work.

- [ ] **Step 3: Commit**

```bash
git add build/Build-PeWim.ps1
git commit -m "feat(build): add Build-PeWim.ps1 (assemble PE WIM + ISO with efisys_noprompt)"
```

---

## Task 13: Pre-flight check script

**Files:**
- Create: `build/PrePEFlight-Check.ps1`

Runbook companion: a script the operator runs once on the build host after one-time setup, to verify everything's in place before the first real build. Reports pass/fail per check.

- [ ] **Step 1: Create the script**

Create `build/PrePEFlight-Check.ps1`:

```powershell
#Requires -Version 7
<#
.SYNOPSIS
    Verify build host setup before running Build-*.ps1 for the first time.
#>

[CmdletBinding()]
param(
    [string] $AdkRoot = 'C:\Program Files (x86)\Windows Kits\10',
    [string] $BuildRoot = 'C:\BuildRoot',
    [string] $WorkDriveLetter
)

$ErrorActionPreference = 'Continue'
$failures = @()

function Check([string]$Name, [scriptblock]$Test, [string]$FixHint) {
    try {
        $result = & $Test
        if ($result) {
            Write-Host ("[ OK ]  {0}" -f $Name) -ForegroundColor Green
        } else {
            Write-Host ("[FAIL]  {0}" -f $Name) -ForegroundColor Red
            Write-Host ("        Hint: {0}" -f $FixHint) -ForegroundColor Yellow
            $script:failures += $Name
        }
    } catch {
        Write-Host ("[FAIL]  {0} (error: {1})" -f $Name, $_.Exception.Message) -ForegroundColor Red
        Write-Host ("        Hint: {0}" -f $FixHint) -ForegroundColor Yellow
        $script:failures += $Name
    }
}

Write-Host ('=' * 70)
Write-Host 'PE Flight Check'
Write-Host ('=' * 70)

Check 'Running PowerShell 7+' { $PSVersionTable.PSVersion.Major -ge 7 } 'Install pwsh 7 (winget install Microsoft.PowerShell)'
Check 'Running as Administrator' {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    ([System.Security.Principal.WindowsPrincipal]$id).IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
} 'Run pwsh as Administrator'

Check "ADK installed at $AdkRoot" { Test-Path $AdkRoot } "Install Windows ADK to $AdkRoot"
Check 'ADK has WinPE add-on (arm64 winpe.wim)' {
    Test-Path (Join-Path $AdkRoot 'Assessment and Deployment Kit\Windows Preinstallation Environment\arm64\en-us\winpe.wim')
} 'Install the WinPE add-on for the ADK'
Check 'ADK has oscdimg (arm64)' {
    Test-Path (Join-Path $AdkRoot 'Assessment and Deployment Kit\Deployment Tools\arm64\Oscdimg\oscdimg.exe')
} 'Install ADK Deployment Tools'
Check 'ADK has efisys_noprompt.bin (arm64)' {
    Test-Path (Join-Path $AdkRoot 'Assessment and Deployment Kit\Deployment Tools\arm64\Oscdimg\efisys_noprompt.bin')
} 'Reinstall ADK Deployment Tools — efisys_noprompt.bin should ship by default'

Check "BuildRoot exists ($BuildRoot)" { Test-Path $BuildRoot } "Create $BuildRoot and the inputs/outputs/work/src subdirs"
Check "BuildRoot\inputs\windows has at least one ISO" {
    @(Get-ChildItem -Path (Join-Path $BuildRoot 'inputs\windows') -Filter '*.iso' -ErrorAction SilentlyContinue).Count -gt 0
} "Stage a Windows ISO at $BuildRoot\inputs\windows"
Check "BuildRoot\inputs\virtio has at least one ISO" {
    @(Get-ChildItem -Path (Join-Path $BuildRoot 'inputs\virtio') -Filter '*.iso' -ErrorAction SilentlyContinue).Count -gt 0
} "Stage virtio-win.iso at $BuildRoot\inputs\virtio"
Check "BuildRoot\inputs\runtime has pwsh 7 zip" {
    @(Get-ChildItem -Path (Join-Path $BuildRoot 'inputs\runtime') -Filter 'PowerShell-*-win-*.zip' -ErrorAction SilentlyContinue).Count -gt 0
} "Download pwsh 7 zip and place in $BuildRoot\inputs\runtime"
Check "BuildRoot\inputs\runtime has .NET 8 runtime zip" {
    @(Get-ChildItem -Path (Join-Path $BuildRoot 'inputs\runtime') -Filter 'dotnet-runtime-*-win-*.zip' -ErrorAction SilentlyContinue).Count -gt 0
} "Download .NET 8 runtime zip and place in $BuildRoot\inputs\runtime"

Check 'OpenSSH server installed' {
    (Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*').State -eq 'Installed'
} 'Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0'
Check 'OpenSSH server running' {
    (Get-Service -Name sshd -ErrorAction SilentlyContinue).Status -eq 'Running'
} 'Start-Service sshd; Set-Service sshd -StartupType Automatic'
Check 'OpenSSH default shell is pwsh 7' {
    (Get-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -ErrorAction SilentlyContinue).DefaultShell -eq 'C:\Program Files\PowerShell\7\pwsh.exe'
} "New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value 'C:\Program Files\PowerShell\7\pwsh.exe' -PropertyType String -Force"

if ($WorkDriveLetter) {
    Check "Work drive ${WorkDriveLetter}: is Dev Drive (ReFS, trusted)" {
        $vol = Get-Volume -DriveLetter $WorkDriveLetter -ErrorAction SilentlyContinue
        $vol -and $vol.FileSystem -eq 'ReFS' -and (fsutil devdrv query "${WorkDriveLetter}:" 2>$null | Select-String -Quiet 'Trusted: Yes')
    } 'Format a Dev Drive (Settings → System → Storage → Disks & volumes → Create dev drive). fsutil devdrv setfiltered to mark trusted.'
}

Write-Host ('=' * 70)
if ($failures.Count -eq 0) {
    Write-Host 'ALL CHECKS PASSED' -ForegroundColor Green
    exit 0
} else {
    Write-Host ("{0} CHECK(S) FAILED" -f $failures.Count) -ForegroundColor Red
    exit 1
}
```

- [ ] **Step 2: Verify it parses**

```bash
pwsh -NoProfile -Command "& { . ./build/PrePEFlight-Check.ps1 -AdkRoot /tmp/nope -BuildRoot /tmp/nope }" 2>&1 | head -20
```

Expected: parses; runs the checks; reports failures (it's running on macOS not Windows). Useful smoke test that the syntax is valid.

- [ ] **Step 3: Commit**

```bash
git add build/PrePEFlight-Check.ps1
git commit -m "feat(build): add PrePEFlight-Check for build host verification"
```

---

## Task 14: Dev-Mac wrapper for PE WIM build

**Files:**
- Create: `tools/build-pe-wim.sh`
- Create: `build/build-pe-wim.config.example.json`

Bash wrapper. Reads local config, rsyncs payload, ssh-invokes build script with config on stdin, scps artifacts back, runs `register-artifact`.

- [ ] **Step 1: Create the example config**

Create `build/build-pe-wim.config.example.json`:

```json
{
  "buildHost":         "buildhost",
  "buildHostUser":     "Administrator",
  "buildRootRemote":   "C:/BuildRoot",

  "adkRoot":           "C:/Program Files (x86)/Windows Kits/10",
  "architecture":      "arm64",
  "virtioIsoPath":     "C:/BuildRoot/inputs/virtio/virtio-win-0.1.266.iso",
  "drivers":           ["vioserial", "viostor", "vioscsi", "NetKVM", "balloon", "vioinput"],
  "pwsh7Zip":          "C:/BuildRoot/inputs/runtime/PowerShell-7.4.6-win-arm64.zip",
  "dotnetRuntimeZip":  "C:/BuildRoot/inputs/runtime/dotnet-runtime-8.0.10-win-arm64.zip",
  "payloadDir":        "C:/BuildRoot/src/build/pe-payload",
  "orchestratorUrl":   "http://autopilot.local:5000",
  "outputDir":         "C:/BuildRoot/outputs"
}
```

- [ ] **Step 2: Create the wrapper script**

Create `tools/build-pe-wim.sh`:

```bash
#!/usr/bin/env bash
# Build the PE WIM on the remote build host and register the artifact locally.
#
# Usage:  tools/build-pe-wim.sh [<config.json>]
#         (default: build/build-pe-wim.config.json)
#
# Requires: ssh, rsync, scp (macOS defaults), python3, jq.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-$REPO_ROOT/build/build-pe-wim.config.json}"

if [[ ! -f "$CONFIG" ]]; then
    echo "config not found: $CONFIG" >&2
    echo "(copy build/build-pe-wim.config.example.json to build/build-pe-wim.config.json and edit)" >&2
    exit 1
fi
for tool in ssh rsync scp jq python3; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

BUILD_HOST=$(jq -r '.buildHost' "$CONFIG")
BUILD_USER=$(jq -r '.buildHostUser' "$CONFIG")
BUILD_ROOT=$(jq -r '.buildRootRemote' "$CONFIG")
PAYLOAD_DIR_REMOTE=$(jq -r '.payloadDir' "$CONFIG")
OUTPUT_DIR=$(jq -r '.outputDir' "$CONFIG")
ARCH=$(jq -r '.architecture' "$CONFIG")

# --- Convert the config to one Build-PeWim.ps1 will accept (drop dev-Mac-only fields) ---
BUILD_CONFIG_JSON=$(jq 'del(.buildHost, .buildHostUser, .buildRootRemote)' "$CONFIG")

# --- 1. rsync the PE payload tree to the build host ---
echo ">> rsync PE payload → ${BUILD_USER}@${BUILD_HOST}:${PAYLOAD_DIR_REMOTE}"
# rsync -e ssh requires forward-slash paths on the SSH side; OpenSSH on Windows handles them.
rsync -av --delete --exclude '.gitkeep' \
    "$REPO_ROOT/build/pe-payload/" \
    "${BUILD_USER}@${BUILD_HOST}:${PAYLOAD_DIR_REMOTE}/"

# --- 2. ssh + run Build-PeWim.ps1 with config on stdin ---
echo ">> ssh build host: pwsh Build-PeWim.ps1"
SCRIPT_REMOTE="${BUILD_ROOT}/src/build/Build-PeWim.ps1"
BUILD_OUTPUT=$(echo "$BUILD_CONFIG_JSON" | ssh "${BUILD_USER}@${BUILD_HOST}" "pwsh -NoProfile -File '${SCRIPT_REMOTE}' -ConfigJson -")

echo "$BUILD_OUTPUT"

# Parse output: lines "WIM: ...", "ISO: ...", "Sidecar: ...", "Log: ..." appear on success.
WIM_REMOTE=$(echo "$BUILD_OUTPUT"     | awk -F'[[:space:]]+' '/^WIM:/     {print $2}')
ISO_REMOTE=$(echo "$BUILD_OUTPUT"     | awk -F'[[:space:]]+' '/^ISO:/     {print $2}')
SIDECAR_REMOTE=$(echo "$BUILD_OUTPUT" | awk -F'[[:space:]]+' '/^Sidecar:/ {print $2}')
LOG_REMOTE=$(echo "$BUILD_OUTPUT"     | awk -F'[[:space:]]+' '/^Log:/     {print $2}')

if [[ -z "$WIM_REMOTE" || -z "$SIDECAR_REMOTE" ]]; then
    echo "Build failed or output unparsable." >&2
    exit 2
fi

# --- 3. scp artifacts back ---
STAGING="$REPO_ROOT/var/artifacts/staging"
mkdir -p "$STAGING"
echo ">> scp artifacts → $STAGING"
scp "${BUILD_USER}@${BUILD_HOST}:${WIM_REMOTE}"     "$STAGING/"
scp "${BUILD_USER}@${BUILD_HOST}:${ISO_REMOTE}"     "$STAGING/"
scp "${BUILD_USER}@${BUILD_HOST}:${SIDECAR_REMOTE}" "$STAGING/"
scp "${BUILD_USER}@${BUILD_HOST}:${LOG_REMOTE}"     "$STAGING/"

WIM_LOCAL="$STAGING/$(basename "$WIM_REMOTE")"
ISO_LOCAL="$STAGING/$(basename "$ISO_REMOTE")"
SIDECAR_LOCAL="$STAGING/$(basename "$SIDECAR_REMOTE")"

# --- 4. register the WIM ---
echo ">> register WIM"
( cd "$REPO_ROOT/autopilot-proxmox" && python3 -m web.artifact_register \
    --path "$WIM_LOCAL" --sidecar "$SIDECAR_LOCAL" --extension wim )

# --- 5. ISO is registered separately (different sha, different sidecar field) ---
# For v1 we skip ISO registration in the index — the orchestrator's manifest API doesn't
# need to serve the ISO (UTM attaches it directly). Plan 2 may add an ISO register step.
echo ">> ISO staged at $ISO_LOCAL (not registered to artifact-store in v1; UTM attaches directly)"

echo "DONE"
```

- [ ] **Step 3: Make it executable**

```bash
chmod +x tools/build-pe-wim.sh
```

- [ ] **Step 4: Smoke-test (dry-run, no real build host)**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot
./tools/build-pe-wim.sh /tmp/nonexistent.json 2>&1 | head -3
```

Expected: error "config not found" — confirms the script runs and validates inputs.

- [ ] **Step 5: Commit**

```bash
git add tools/build-pe-wim.sh build/build-pe-wim.config.example.json
git commit -m "feat(build): add tools/build-pe-wim.sh dev-Mac wrapper"
```

---

## Task 15: Dev-Mac wrapper for install.wim build

**Files:**
- Create: `tools/build-install-wim.sh`
- Create: `build/build-install-wim.config.example.json`

Same shape as `build-pe-wim.sh` but no payload rsync (install.wim has no per-build inputs from the dev Mac).

- [ ] **Step 1: Create the example config**

Create `build/build-install-wim.config.example.json`:

```json
{
  "buildHost":       "buildhost",
  "buildHostUser":   "Administrator",
  "buildRootRemote": "C:/BuildRoot",

  "windowsIsoPath":  "C:/BuildRoot/inputs/windows/Win11_24H2_ARM64.iso",
  "virtioIsoPath":   "C:/BuildRoot/inputs/virtio/virtio-win-0.1.266.iso",
  "edition":         "Windows 11 Enterprise",
  "architecture":    "arm64",
  "drivers":         ["vioserial", "viostor", "vioscsi", "NetKVM", "balloon", "vioinput"],
  "outputDir":       "C:/BuildRoot/outputs"
}
```

- [ ] **Step 2: Create the wrapper script**

Create `tools/build-install-wim.sh`:

```bash
#!/usr/bin/env bash
# Build install.wim on the remote build host and register the artifact locally.
#
# Usage:  tools/build-install-wim.sh [<config.json>]
#         (default: build/build-install-wim.config.json)

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-$REPO_ROOT/build/build-install-wim.config.json}"

if [[ ! -f "$CONFIG" ]]; then
    echo "config not found: $CONFIG" >&2
    echo "(copy build/build-install-wim.config.example.json to build/build-install-wim.config.json and edit)" >&2
    exit 1
fi
for tool in ssh scp jq python3; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

BUILD_HOST=$(jq -r '.buildHost' "$CONFIG")
BUILD_USER=$(jq -r '.buildHostUser' "$CONFIG")
BUILD_ROOT=$(jq -r '.buildRootRemote' "$CONFIG")

BUILD_CONFIG_JSON=$(jq 'del(.buildHost, .buildHostUser, .buildRootRemote)' "$CONFIG")

echo ">> ssh build host: pwsh Build-InstallWim.ps1"
SCRIPT_REMOTE="${BUILD_ROOT}/src/build/Build-InstallWim.ps1"
BUILD_OUTPUT=$(echo "$BUILD_CONFIG_JSON" | ssh "${BUILD_USER}@${BUILD_HOST}" "pwsh -NoProfile -File '${SCRIPT_REMOTE}' -ConfigJson -")

echo "$BUILD_OUTPUT"

WIM_REMOTE=$(echo "$BUILD_OUTPUT"     | awk -F'[[:space:]]+' '/^WIM:/     {print $2}')
SIDECAR_REMOTE=$(echo "$BUILD_OUTPUT" | awk -F'[[:space:]]+' '/^Sidecar:/ {print $2}')
LOG_REMOTE=$(echo "$BUILD_OUTPUT"     | awk -F'[[:space:]]+' '/^Log:/     {print $2}')

if [[ -z "$WIM_REMOTE" || -z "$SIDECAR_REMOTE" ]]; then
    echo "Build failed or output unparsable." >&2
    exit 2
fi

STAGING="$REPO_ROOT/var/artifacts/staging"
mkdir -p "$STAGING"
echo ">> scp artifacts → $STAGING"
scp "${BUILD_USER}@${BUILD_HOST}:${WIM_REMOTE}"     "$STAGING/"
scp "${BUILD_USER}@${BUILD_HOST}:${SIDECAR_REMOTE}" "$STAGING/"
scp "${BUILD_USER}@${BUILD_HOST}:${LOG_REMOTE}"     "$STAGING/"

WIM_LOCAL="$STAGING/$(basename "$WIM_REMOTE")"
SIDECAR_LOCAL="$STAGING/$(basename "$SIDECAR_REMOTE")"

echo ">> register WIM"
( cd "$REPO_ROOT/autopilot-proxmox" && python3 -m web.artifact_register \
    --path "$WIM_LOCAL" --sidecar "$SIDECAR_LOCAL" --extension wim )

echo "DONE"
```

- [ ] **Step 3: Make it executable**

```bash
chmod +x tools/build-install-wim.sh
```

- [ ] **Step 4: Smoke-test**

```bash
./tools/build-install-wim.sh /tmp/nonexistent.json 2>&1 | head -3
```

Expected: "config not found" error.

- [ ] **Step 5: Commit**

```bash
git add tools/build-install-wim.sh build/build-install-wim.config.example.json
git commit -m "feat(build): add tools/build-install-wim.sh dev-Mac wrapper"
```

---

## Task 16: Build host setup runbook

**Files:**
- Create: `build/README.md`

The skill says no documentation files unless requested, but a build host has too many one-time setup steps for a comment in a script. The user's spec explicitly references this runbook in "Build host setup" — it's a hard prerequisite.

- [ ] **Step 1: Create the runbook**

Create `build/README.md`:

```markdown
# Build host setup runbook

One-time setup for the Windows-on-Proxmox VM that builds `install.wim` and the PE WIM.
Run **`build/PrePEFlight-Check.ps1`** at the end to verify.

## 1. Base OS

- Windows 11 (any recent edition with ADK support).
- 8+ GB RAM, 100+ GB disk (the staging WIM mount + outputs add up).
- Local Administrator account.

## 2. PowerShell 7

```powershell
winget install --id Microsoft.PowerShell --source winget
```

Verify: `pwsh --version` shows 7.x.

## 3. Windows ADK + WinPE add-on

Download from <https://learn.microsoft.com/en-us/windows-hardware/get-started/adk-install>.
Install ADK first, then the WinPE add-on. Default location:
`C:\Program Files (x86)\Windows Kits\10\`.

## 4. OpenSSH server

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic

# Default shell = pwsh 7 (critical: without this, SSH lands in cmd.exe)
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
    -Value 'C:\Program Files\PowerShell\7\pwsh.exe' -PropertyType String -Force
```

### Authorize your dev-Mac key

Copy `~/.ssh/id_ed25519.pub` from the Mac. On the build host as Administrator:

```powershell
# Place in BOTH paths (Windows quirk: Administrators-group keys live under ProgramData)
$key = '<paste-public-key-here>'
Add-Content -Path "$env:USERPROFILE\.ssh\authorized_keys" -Value $key
Add-Content -Path 'C:\ProgramData\ssh\administrators_authorized_keys' -Value $key

# Lock down ACLs on the second file (sshd refuses lax permissions)
icacls 'C:\ProgramData\ssh\administrators_authorized_keys' /inheritance:r
icacls 'C:\ProgramData\ssh\administrators_authorized_keys' /grant 'Administrators:F' 'SYSTEM:F'
```

Disable password auth in `C:\ProgramData\ssh\sshd_config`:

```
PasswordAuthentication no
PubkeyAuthentication yes
```

Restart sshd: `Restart-Service sshd`.

## 5. Dev Drive (recommended)

Create a Dev Drive (ReFS) for the WIM mount/scratch area. Settings → System → Storage → Disks & volumes → Create dev drive (or `New-VHD` + ReFS-format manually). Mount as a drive letter (e.g. `D:`).

Verify trusted state:

```powershell
fsutil devdrv query D:
# Expect: "Trusted: Yes"

# If not trusted:
fsutil devdrv setfiltered /unfiltered D:
```

Then point `BuildRoot\work\` at it (via symlink or by setting `BuildRoot=D:\BuildRoot` in the build configs).

## 6. Directory layout

```powershell
$Root = 'C:\BuildRoot'   # or D:\BuildRoot if Dev Drive
New-Item -ItemType Directory -Path $Root\inputs\windows  -Force
New-Item -ItemType Directory -Path $Root\inputs\virtio   -Force
New-Item -ItemType Directory -Path $Root\inputs\runtime  -Force
New-Item -ItemType Directory -Path $Root\src             -Force
New-Item -ItemType Directory -Path $Root\outputs         -Force
New-Item -ItemType Directory -Path $Root\work            -Force
```

## 7. Stage source ISOs and runtime zips

- `inputs\windows\` — your Windows 11 install ISO (e.g. `Win11_24H2_ARM64.iso`).
- `inputs\virtio\` — `virtio-win-<version>.iso` from <https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/>.
- `inputs\runtime\` — `PowerShell-<ver>-win-<arch>.zip` from <https://github.com/PowerShell/PowerShell/releases> and `dotnet-runtime-<ver>-win-<arch>.zip` from <https://dotnet.microsoft.com/en-us/download/dotnet/8.0>.

## 8. Clone this repo into `src\`

```powershell
cd C:\BuildRoot\src
git clone https://github.com/<your-fork>/ProxmoxVEAutopilot.git .
```

## 9. Defender exclusions (skip if using Dev Drive)

If `BuildRoot\work\` is **not** on a Dev Drive, exclude it from real-time scanning — DISM mount/dismount of a 4 GB WIM with Defender enabled is 10x slower and prone to lock contention:

```powershell
Add-MpPreference -ExclusionPath 'C:\BuildRoot\work'
```

## 10. Verify

From an elevated pwsh:

```powershell
cd C:\BuildRoot\src
.\build\PrePEFlight-Check.ps1 -AdkRoot 'C:\Program Files (x86)\Windows Kits\10' -BuildRoot 'C:\BuildRoot' -WorkDriveLetter D
```

Expect `ALL CHECKS PASSED`. Address any failure before running the build scripts.
```

- [ ] **Step 2: Commit**

```bash
git add build/README.md
git commit -m "docs(build): add build host setup runbook"
```

---

## Task 17: End-to-end smoke test runbook

**Files:**
- Create: `build/SMOKE-TEST.md`

Manual integration test runbook. The user runs this once from the dev Mac after the build host is configured. Verifies the full pipeline produces a registered, hash-matching artifact.

- [ ] **Step 1: Create the runbook**

Create `build/SMOKE-TEST.md`:

```markdown
# Smoke test: end-to-end build pipeline

Run this once after build host setup to confirm the pipeline produces correct artifacts.

## Prerequisites

- Build host configured per `build/README.md`.
- `build/PrePEFlight-Check.ps1` passes on the build host.
- Repo cloned to `C:\BuildRoot\src\` on the build host.
- Dev Mac can `ssh ${BUILD_USER}@${BUILD_HOST} pwsh -Command 'Write-Host hi'` and see `hi`.

## 1. Build the PE WIM (small, ~10 min)

On the dev Mac:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot
cp build/build-pe-wim.config.example.json build/build-pe-wim.config.json
# Edit build-pe-wim.config.json: set buildHost, buildHostUser, paths.
./tools/build-pe-wim.sh
```

Expected output ends with `DONE` and `registered pe-wim <sha>`.

### Verify

```bash
ls -la var/artifacts/staging/        # contains the WIM, ISO, sidecar, log
ls -la var/artifacts/store/          # contains <sha>.wim
sqlite3 var/artifacts/index.db 'SELECT sha256, kind, size FROM artifacts;'
# Should list one row with kind=pe-wim.
```

## 2. Build install.wim (large, ~30 min)

```bash
cp build/build-install-wim.config.example.json build/build-install-wim.config.json
# Edit. Set windowsIsoPath, virtioIsoPath to the real paths on the build host.
./tools/build-install-wim.sh
```

Expected: `DONE` and `registered install-wim <sha>` after ~30 min.

### Verify

```bash
sqlite3 var/artifacts/index.db 'SELECT sha256, kind, size FROM artifacts ORDER BY registered_at;'
# Two rows: pe-wim and install-wim.
```

## 3. Boot the PE ISO in UTM (smoke test of the placeholder Bootstrap.ps1)

1. Find the PE ISO at `var/artifacts/staging/winpe-autopilot-arm64-<sha>.iso`.
2. Create a new Win11 ARM64 VM in UTM with the PE ISO attached as CD/DVD.
3. Boot. Should boot through to PE without prompts.
4. PE should display the placeholder bootstrap output:

   ```
   ========================================
   Autopilot PE bootstrap (placeholder)
   ========================================
   PowerShell version : 7.4.x
   Edition            : Core
   OS                 : Microsoft Windows ...
   Architecture       : ARM64
   Hostname           : MININT-...
   SMBIOS UUID        : <uuid>
   ...
   Orchestrator URL   : http://autopilot.local:5000
   ```

5. Pwsh prompt is interactive. Type `exit` or `wpeutil reboot` to leave.

If you see this output, **the build pipeline is working end-to-end** and Plan 1 is complete.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `tools/build-pe-wim.sh` fails on rsync with "Permission denied" | `administrators_authorized_keys` ACLs wrong | Re-run icacls steps in `build/README.md` §4 |
| ssh hangs after key auth | OpenSSH default shell still cmd.exe | Re-set `HKLM:\SOFTWARE\OpenSSH\DefaultShell` and `Restart-Service sshd` |
| Build script fails at `Mount-DiskImage` | ISO path on build host wrong, or running unelevated | Check `windowsIsoPath` exists on build host; verify SSH session is elevated (`whoami /groups | findstr S-1-16-12288`) |
| Build script fails at `Add-WindowsPackage` with "specified package is not applicable" | Wrong arch — `WinPE_OCs\<pkg>.cab` from amd64 path used for arm64 build | Double-check `architecture` in the build config |
| PE boots but prints nothing visible | `unattend.xml` not picked up | Check the WIM's `\unattend.xml` exists with the `RunSynchronousCommand`; verify `winpeshl.ini` in `\Windows\System32` |
| PE "Press any key to boot from CD/DVD" prompt appears | ISO built without `efisys_noprompt.bin` | Re-check the oscdimg arguments in `Build-PeWim.ps1` Phase 11 |
```

- [ ] **Step 2: Commit**

```bash
git add build/SMOKE-TEST.md
git commit -m "docs(build): add end-to-end smoke-test runbook"
```

---

## Task 18: Final integration check

**Files:** none new — this is the verification task.

- [ ] **Step 1: Run all unit tests on the dev Mac**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot
pwsh -Command "Invoke-Pester -Path build/Modules/Autopilot.Build/Tests/ -CI"
# Expect: all PowerShell helper tests pass.

cd autopilot-proxmox
pytest tests/test_artifact_sidecar.py tests/test_artifact_store.py tests/test_artifact_register.py -v
# Expect: 8 + 7 + 3 = 18 tests pass.
```

- [ ] **Step 2: Verify the example configs parse**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot
jq . build/build-pe-wim.config.example.json     >/dev/null && echo "pe config OK"
jq . build/build-install-wim.config.example.json >/dev/null && echo "install config OK"
```

Expected: both `OK`.

- [ ] **Step 3: Verify both wrapper scripts handle missing config gracefully**

```bash
./tools/build-pe-wim.sh /tmp/missing.json 2>&1 | grep -q 'config not found' && echo 'pe wrapper OK'
./tools/build-install-wim.sh /tmp/missing.json 2>&1 | grep -q 'config not found' && echo 'install wrapper OK'
```

- [ ] **Step 4: Verify file inventory matches plan**

```bash
ls build/Build-InstallWim.ps1 build/Build-PeWim.ps1 build/PrePEFlight-Check.ps1 \
   build/README.md build/SMOKE-TEST.md \
   build/Modules/Autopilot.Build/Autopilot.Build.psd1 \
   build/Modules/Autopilot.Build/Public/Write-CmTraceLog.ps1 \
   build/Modules/Autopilot.Build/Public/Get-FileSha256.ps1 \
   build/Modules/Autopilot.Build/Public/New-BuildLock.ps1 \
   build/Modules/Autopilot.Build/Public/Write-ArtifactSidecar.ps1 \
   build/pe-payload/Bootstrap.ps1 build/pe-payload/Bootstrap.json \
   build/pe-payload/winpeshl.ini build/pe-payload/unattend.xml.template \
   tools/build-install-wim.sh tools/build-pe-wim.sh \
   autopilot-proxmox/web/artifact_sidecar.py \
   autopilot-proxmox/web/artifact_store.py \
   autopilot-proxmox/web/artifact_register.py
```

Expected: all files exist (no "No such file" errors).

- [ ] **Step 5: Run the smoke test**

Follow `build/SMOKE-TEST.md` end-to-end. This is the definitive Plan 1 exit criterion.

**Plan 1 is complete when the smoke test passes** — meaning a PE WIM and install.wim build, register, and (for the PE WIM) boot in UTM showing the placeholder bootstrap diagnostic output.

---

## Self-review checklist

- [x] Spec coverage: every requirement in `2026-04-25-winpe-osd-pipeline-design.md` Section 5 (Build pipeline) and Section 8 (Artifact storage) maps to a task above. Spec Section 6 (PE runtime) is explicitly deferred to Plan 3 — Plan 1 ships the placeholder Bootstrap. Spec Section 7 (Orchestrator API) is explicitly deferred to Plan 2.
- [x] No placeholders ("TBD", "TODO", "fill in") in any task.
- [x] Type/name consistency: `ArtifactKind`, `Sidecar`, `ArtifactRecord`, `ArtifactStore.register`, `Write-CmTraceLog`, `Get-FileSha256`, `New-BuildLock`, `Write-ArtifactSidecar`, `Build-InstallWim.ps1`, `Build-PeWim.ps1`, `tools/build-install-wim.sh`, `tools/build-pe-wim.sh` — all referenced consistently across tasks.
- [x] Every task has a commit step.
- [x] TDD on the helpers + Python (Tasks 2-9). Build scripts (Tasks 11-12) document the integration-test runbook (SMOKE-TEST.md, Task 17) instead of unit-mocking DISM.
