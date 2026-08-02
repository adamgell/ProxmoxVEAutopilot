# Setup-CM Content-Location Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a typed, idempotent Agent task that repairs only the LABZ1 client boundary/group/local-DP content-location path.

**Architecture:** The FastAPI controller owns an immutable request contract and queues a new work kind. `SetupCmDiagnosticsWorkService` validates the same contract, runs an embedded PowerShell resource, and requires bounded JSON readback. The resource uses Configuration Manager cmdlets only to create/reuse the fixed subnet boundary, named group, and local distribution point association.

**Tech Stack:** FastAPI/Pydantic, PostgreSQL Agent work queue, .NET 8 Autopilot Agent, PowerShell 7+, MECM ConfigurationManager module, pytest, Agent contract test executable.

## Global Constraints

- Only `LAB`, `192.168.16.0/24`, `LABZ1 Client Network`, and `LABZ1-CM01.test.gell.one` are accepted.
- Requests reject extra fields and must not accept scripts, paths, arbitrary site codes, subnets, groups, or DPs.
- Re-runs reuse matching MECM objects; conflicts return evidence and do not delete or overwrite objects.
- Output stays below the existing 256 KiB ceiling and has explicit readback fields.
- No installer, credential, key, token, generated LAB configuration, or media enters Git.

---

### Task 1: Controller remediation contract and queue

**Files:**
- Modify: `autopilot-proxmox/web/setup_cm_endpoints.py:88-192`
- Modify: `autopilot-proxmox/tests/test_agent_v1_endpoints.py:1947-1980`

**Interfaces:**
- Consumes: `agent_id` and the immutable LABZ1 remediation body.
- Produces: `setup_cm_content_location_remediation` work with the accepted body as `request_json`.

- [ ] **Step 1: Write the failing endpoint test**

```python
body = {
    "site_code": "LAB",
    "client_subnet": "192.168.16.0/24",
    "boundary_group_name": "LABZ1 Client Network",
    "distribution_point_fqdn": "LABZ1-CM01.test.gell.one",
}
accepted = agent_client.post(
    "/api/setup-cm/v1/agents/agent-cm01-content-location/content-location-remediation",
    json=body,
)
assert accepted.status_code == 202
assert accepted.json()["kind"] == "setup_cm_content_location_remediation"
assert accepted.json()["request"] == body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py -k content_location_remediation -q`

Expected: FAIL because the route and body model do not exist.

- [ ] **Step 3: Implement strict Pydantic body and route**

```python
class SetupCmContentLocationRemediationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_code: Literal["LAB"]
    client_subnet: Literal["192.168.16.0/24"]
    boundary_group_name: Literal["LABZ1 Client Network"]
    distribution_point_fqdn: Literal["LABZ1-CM01.test.gell.one"]
```

Create work with `kind="setup_cm_content_location_remediation"`; preserve the unknown-Agent 404 behavior.

- [ ] **Step 4: Run endpoint test to verify it passes**

Run: `pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py -k 'content_location_diagnostics or content_location_remediation' -q`

Expected: PASS, including rejections for a foreign subnet, foreign DP, and an extra `script` field.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/setup_cm_endpoints.py autopilot-proxmox/tests/test_agent_v1_endpoints.py
git commit -m "feat: queue content location remediation"
```

### Task 2: Agent request contract and embedded resource

**Files:**
- Modify: `autopilot-agent/src/AutopilotAgent/SetupCmDiagnosticsWorkService.cs:17-304`
- Create: `autopilot-agent/src/AutopilotAgent/SetupCmContentLocationRemediation.ps1`
- Modify: `autopilot-agent/src/AutopilotAgent/AutopilotAgent.csproj:19-23`
- Modify: `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`

**Interfaces:**
- Consumes: work kind `setup_cm_content_location_remediation` with the four fixed contract values.
- Produces: JSON fields `site_code`, `client_subnet`, `boundary_group_name`, `distribution_point_fqdn`, `boundary`, `boundary_group`, `distribution_points`, `changed`, and `errors`.

- [ ] **Step 1: Write failing Agent contract tests**

```csharp
var request = SetupCmDiagnosticsWorkService.ValidateContentLocationRemediationRequest(
    Request(("site_code", "LAB"), ("client_subnet", "192.168.16.0/24"),
        ("boundary_group_name", "LABZ1 Client Network"),
        ("distribution_point_fqdn", "LABZ1-CM01.test.gell.one")));
Assert(request.ClientSubnet == "192.168.16.0/24", "remediation subnet changed");
```

Also assert invalid `10.0.0.0/24` is rejected, `SupportedKinds` contains the remediation kind, and the project embeds the remediation resource.

- [ ] **Step 2: Run Agent contracts to verify they fail**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: FAIL or compile failure because the remediation validator and resource are absent.

- [ ] **Step 3: Implement typed dispatch**

Add a dedicated request record, strict validator, `ProcessContentLocationRemediationAsync`, PowerShell argument overload, and JSON result parser. Use the existing five-minute timeout and 256 KiB output ceiling. Add the work kind to `SupportedKinds`.

- [ ] **Step 4: Implement idempotent PowerShell resource**

The resource loads the Configuration Manager module, looks up the fixed `IPSubnet` boundary, creates it only if absent, looks up/creates the fixed group, validates existing values before adding memberships, associates only the fixed local DP, and emits JSON readback. The sole permitted `Set-*` call is `Set-CMBoundaryGroup -Name $BoundaryGroupName -AddSiteSystemServerName $DistributionPointFqdn`; it must not call `Remove-*`, `Clear-*`, or any other `Set-*` form.

- [ ] **Step 5: Run Agent contracts to verify they pass**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: PASS with the contract, resource, and parser assertions.

- [ ] **Step 6: Commit**

```bash
git add autopilot-agent/src/AutopilotAgent/SetupCmDiagnosticsWorkService.cs autopilot-agent/src/AutopilotAgent/SetupCmContentLocationRemediation.ps1 autopilot-agent/src/AutopilotAgent/AutopilotAgent.csproj autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs
git commit -m "feat: remediate LABZ1 content location"
```

### Task 3: Cross-layer verification, review, and LAB proof

**Files:**
- Modify: none unless verification identifies a defect.

**Interfaces:**
- Consumes: the controller route and Agent work kind from Tasks 1-2.
- Produces: CI-published Agent release and live, readback-backed LABZ1 boundary/DP evidence.

- [ ] **Step 1: Run focused cross-layer tests**

Run: `pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py -k 'setup_cm or content_location' -q && dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: PASS.

- [ ] **Step 2: Parse and inspect the PowerShell resource**

Run: `pwsh -NoProfile -Command "[void][scriptblock]::Create((Get-Content autopilot-agent/src/AutopilotAgent/SetupCmContentLocationRemediation.ps1 -Raw)); Select-String -Path autopilot-agent/src/AutopilotAgent/SetupCmContentLocationRemediation.ps1 -Pattern 'Remove-|Set-'"`

Expected: parser succeeds. The resource may contain the one exact additive `Set-CMBoundaryGroup -AddSiteSystemServerName` call, but contains no `Remove-*`, `Clear-*`, or other `Set-*` forms.

- [ ] **Step 3: Run full controller suite**

Run: `pytest autopilot-proxmox/tests -q`

Expected: PASS with only known environment warnings.

- [ ] **Step 4: Run CodeRabbit review**

Run: `coderabbit review --agent -t uncommitted --base-commit 045b07c`

Expected: no critical or warning findings. Address any finding with a new failing regression test before changing production code.

- [ ] **Step 5: Release and prove LAB result**

Commit the release version, push/tag it, wait for tag CI, deploy it, build/publish the matching MSI, and let CM01 self-update. Queue the typed remediation, then the existing content-location diagnostic. Required live result: the fixed subnet is in `matching_boundaries`, `LABZ1 Client Network` is in `boundary_groups`, and `LABZ1-CM01.test.gell.one` is in `distribution_points`.
