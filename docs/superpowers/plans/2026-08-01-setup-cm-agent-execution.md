# Setup-CM Autopilot Agent Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the hash-pinned Setup-CM deployment stages through AutopilotAgent rather than guest-agent or ad-hoc remote execution.

**Architecture:** The controller queues four typed work items. A new Agent service validates the bounded work request, copies and hash-verifies a Setup-CM module archive, and starts PowerShell 7 against the archive's fixed `Invoke-SetupCm.ps1` entry point. The service returns bounded process output and the fixed evidence root; the controller never accepts arbitrary PowerShell or secrets.

**Tech Stack:** .NET 8 Worker Service, PowerShell 7, FastAPI/Pydantic, PostgreSQL agent work queue, pytest, Agent contract tests.

## Global Constraints

- Work kinds are exactly `setup_cm_acquire`, `setup_cm_sql`, `setup_cm_mecm`, and `setup_cm_health`.
- Use `pwsh.exe`, never Windows PowerShell, for Setup-CM work.
- The request contains a fixed `config_path`, `evidence_root`, `module_archive_path`, and 64-character SHA-256 only; it contains no product keys, credentials, media bytes, tokens, or script text.
- Agent execution accepts only `C:\ProgramData\SetupCm\` config/evidence paths and `\\LABZ1-DC02\SetupCm\` or `C:\ProgramData\SetupCm\` module archive paths.
- The Agent reports at most 256 KiB per output stream and a 3-hour timeout.
- Preserve existing Setup-CM Test/Apply/Verify behavior and its evidence; do not copy deployment logic into C#.

---

### Task 1: Typed Setup-CM request validation and Agent capability

**Files:**
- Create: `autopilot-agent/src/AutopilotAgent/SetupCmWorkService.cs`
- Modify: `autopilot-agent/src/AutopilotAgent/Worker.cs`
- Modify: `autopilot-agent/src/AutopilotAgent/Program.cs`
- Modify: `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`

**Interfaces:**
- Consumes: `AgentWorkItem.Request` as `IReadOnlyDictionary<string, JsonElement>`.
- Produces: `SetupCmWorkService.SupportedKinds`, `SetupCmWorkService.ProcessAsync`, and Agent heartbeat capabilities for the four typed kinds.

- [ ] **Step 1: Write the failing contract tests**

```csharp
Assert.True(SetupCmWorkService.SupportedKinds.Contains("setup_cm_sql"));
Assert.Throws<InvalidOperationException>(() =>
    SetupCmWorkService.ValidateRequest("setup_cm_sql", Request(
        archivePath: @"C:\Windows\Temp\module.zip")));
Assert.Throws<InvalidOperationException>(() =>
    SetupCmWorkService.ValidateRequest("setup_cm_sql", Request(
        archiveSha256: "not-a-hash")));
```

- [ ] **Step 2: Run the contract tests to verify RED**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: FAIL because `SetupCmWorkService` does not exist.

- [ ] **Step 3: Implement minimal typed validation and process launch**

```csharp
public static readonly IReadOnlySet<string> SupportedKinds = new HashSet<string>(StringComparer.Ordinal)
{ "setup_cm_acquire", "setup_cm_sql", "setup_cm_mecm", "setup_cm_health" };

var psi = new ProcessStartInfo("pwsh.exe") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
psi.ArgumentList.Add("-NoProfile");
psi.ArgumentList.Add("-File");
psi.ArgumentList.Add(entryPoint);
psi.ArgumentList.Add("-ConfigPath");
psi.ArgumentList.Add(configPath);
psi.ArgumentList.Add("-Mode");
psi.ArgumentList.Add("Unattended");
psi.ArgumentList.Add("-Stage");
psi.ArgumentList.Add(stage);
```

Require a 64-character hexadecimal archive SHA-256, copy to the agent work root, compare `SHA256.HashData`, expand the archive, and require `Invoke-SetupCm.ps1` plus `src/SetupCm/SetupCm.psd1` before launch. Reject traversal, UNC paths other than the LABZ1 vault, unknown work kinds, and paths outside the explicit roots.

- [ ] **Step 4: Wire the service into the worker**

Inject `SetupCmWorkService`; append its `SupportedKinds` to `SupportedWorkKinds`; dispatch matching work through `ProcessAsync`; call `CompleteWorkAsync` with `stage`, `archive_sha256`, `evidence_root`, `stdout`, and `stderr`; keep error text secret-free.

- [ ] **Step 5: Run GREEN verification**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: PASS and only the four named kinds are advertised.

- [ ] **Step 6: Commit**

```bash
git add autopilot-agent/src/AutopilotAgent/SetupCmWorkService.cs autopilot-agent/src/AutopilotAgent/Worker.cs autopilot-agent/src/AutopilotAgent/Program.cs autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs
git commit -m "feat: run setup-cm stages through agent"
```

### Task 2: Controller queue endpoint and API contract

**Files:**
- Create: `autopilot-proxmox/web/setup_cm_endpoints.py`
- Modify: `autopilot-proxmox/web/app.py`
- Modify: `autopilot-proxmox/tests/test_agent_v1_endpoints.py`

**Interfaces:**
- Consumes: `POST /api/setup-cm/v1/agents/{agent_id}/work` with a typed JSON body.
- Produces: one `agent_work_items` row whose kind is allowlisted and request JSON has no secret fields.

- [ ] **Step 1: Write failing endpoint tests**

```python
response = agent_client.post(
    "/api/setup-cm/v1/agents/agent-cm01/work",
    json={"stage": "sql", "config_path": r"C:\\ProgramData\\SetupCm\\labz1.local.yaml",
          "evidence_root": r"C:\\ProgramData\\SetupCm\\artifacts",
          "module_archive_path": r"\\\\LABZ1-DC02\\SetupCm\\Modules\\setup-cm.zip",
          "module_archive_sha256": "a" * 64},
)
assert response.status_code == 202
assert response.json()["kind"] == "setup_cm_sql"
```

Also assert `stage="shell"`, a 63-character hash, `C:\\Windows\\Temp` archive path, and an unknown Agent each fail with 422 or 404 and never create work.

- [ ] **Step 2: Run the targeted test to verify RED**

Run: `pytest -q autopilot-proxmox/tests/test_agent_v1_endpoints.py -k setup_cm`

Expected: FAIL with route not found.

- [ ] **Step 3: Implement the route**

Define `SetupCmWorkBody` with `stage: Literal["acquire", "sql", "mecm", "health"]`, `config_path`, `evidence_root`, `module_archive_path`, and `module_archive_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")`. Validate paths with a shared Windows-root helper. Verify the Agent exists, call `agent_telemetry_pg.create_work_item`, and return `202` with the public work record. Do not accept a request/result/password/product-key field.

- [ ] **Step 4: Register the router and verify GREEN**

Run: `pytest -q autopilot-proxmox/tests/test_agent_v1_endpoints.py -k setup_cm`

Expected: PASS with no work item for rejected inputs.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/setup_cm_endpoints.py autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_agent_v1_endpoints.py
git commit -m "feat: queue typed setup-cm agent work"
```

### Task 3: Package, publish, upgrade, and prove CM01 acquisition

**Files:**
- Modify: `scripts/push_agent_msi.sh`
- Create: `tools/setup-cm/Publish-SetupCmModule.ps1`
- Modify: `docs/LABZ1_DEPLOYMENT.md`
- Test: `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`

**Interfaces:**
- Consumes: a clean `setup-cm` source tree.
- Produces: a private `setup-cm.zip` plus SHA-256 in `\\LABZ1-DC02\SetupCm\Modules`, and an Agent MSI update that advertises `setup_cm_acquire`.

- [ ] **Step 1: Write the failing archive-manifest test**

```csharp
Assert.Throws<InvalidOperationException>(() =>
    SetupCmWorkService.ValidateExtractedModule(Path.Combine(temp, "empty")));
```

- [ ] **Step 2: Run RED**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: FAIL until extraction validation is implemented in Task 1.

- [ ] **Step 3: Implement reproducible private module publishing**

`Publish-SetupCmModule.ps1` uses `Compress-Archive` over `Invoke-SetupCm.ps1`, `src`, and required module metadata into a temporary zip, calculates SHA-256, copies the zip to the private Modules share, and writes a sibling JSON manifest containing filename, source commit, hash, and UTC timestamp. It refuses uncommitted source changes and never includes `lab.local.yaml`, cache, artifacts, or secrets.

- [ ] **Step 4: Upgrade CM01 and queue Acquire**

Build the Agent MSI on the configured build host, publish it using the existing controller artifact flow, allow the existing Agent update service to verify the MSI hash, and start the CM01 Agent. Queue `setup_cm_acquire` through the typed endpoint using the private manifest hash. Verify the resulting controller work item, CM01 evidence JSON, copied cache hashes, and Agent heartbeat capability.

- [ ] **Step 5: Run full tests**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj && pytest -q autopilot-proxmox/tests/test_agent_v1_endpoints.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/push_agent_msi.sh tools/setup-cm/Publish-SetupCmModule.ps1 docs/LABZ1_DEPLOYMENT.md autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs
git commit -m "feat: publish setup-cm agent bundles"
```

### Task 4: Code review and live evidence gate

**Files:**
- Modify: `docs/LABZ1_DEPLOYMENT.md`

- [ ] **Step 1: Run the configured CodeRabbit review**

Run: `coderabbit review --plain`

Expected: no unaddressed high-confidence finding. Treat unavailable/rate-limited review as an evidence limitation and perform a manual diff review.

- [ ] **Step 2: Validate the live acquisition evidence**

Confirm the CM01 Agent work item is `complete`, both cached ISO hashes equal the private media manifest, each embedded setup signature is valid Microsoft, and no installer/pid/password appears in the work request, result, Agent log, or Git status.

- [ ] **Step 3: Commit the finalized runbook**

```bash
git add docs/LABZ1_DEPLOYMENT.md
git commit -m "docs: record setup-cm agent validation"
```
