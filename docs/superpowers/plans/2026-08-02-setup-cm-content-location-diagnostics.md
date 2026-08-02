# Setup-CM Content-Location Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a read-only, typed CM01 Agent diagnostic that identifies the exact boundary, DP, or `LAB00003` distribution condition preventing a LABZ1 client installation.

**Architecture:** The controller accepts a closed LAB-specific request and queues a dedicated work kind. The CM01 Agent validates the same request, runs a fixed embedded PowerShell 7 script, validates its bounded JSON result, and records evidence without applying a MECM change.

**Tech Stack:** FastAPI/Pydantic, Python pytest, C#/.NET Autopilot Agent contract tests, PowerShell 7, Configuration Manager PowerShell module.

## Global Constraints

- Use the Autopilot Agent for all Windows work; QGA is diagnostic-only.
- Accept only `site_code=LAB`, a NetBIOS computer name, and an IPv4 address.
- The diagnostic is read-only: no New/Set/Remove MECM cmdlets, SQL writes, or arbitrary scripts.
- Return bounded JSON without credentials, keys, tokens, installer media, or arbitrary command output.
- Keep the controller and Agent request contracts identical.

---

### Task 1: Controller queue contract

**Files:**
- Modify: `autopilot-proxmox/web/setup_cm_endpoints.py:72-168`
- Test: `autopilot-proxmox/tests/test_agent_v1_endpoints.py:1921-1950`

**Interfaces:**
- Consumes: `SetupCmContentLocationDiagnosticsBody(site_code, target_computer_name, client_ipv4)`.
- Produces: `POST /api/setup-cm/v1/agents/{agent_id}/content-location-diagnostics` and work kind `setup_cm_content_location_diagnostics`.

- [ ] **Step 1: Write the failing controller test**

```python
body = {"site_code": "LAB", "target_computer_name": "RING0IVY24-01", "client_ipv4": "192.168.16.103"}
accepted = agent_client.post("/api/setup-cm/v1/agents/agent-cm01-content/content-location-diagnostics", json=body)
assert accepted.status_code == 202
assert accepted.json()["kind"] == "setup_cm_content_location_diagnostics"
assert agent_client.post(..., json={**body, "client_ipv4": "not-an-ip"}).status_code == 422
assert agent_client.post(..., json={**body, "script": "Get-ChildItem"}).status_code == 422
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py -k content_location -q`

Expected: FAIL because the route is not registered.

- [ ] **Step 3: Add the minimal closed Pydantic body and route**

```python
class SetupCmContentLocationDiagnosticsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_code: Literal["LAB"]
    target_computer_name: str = Field(pattern=r"^[A-Za-z0-9-]{1,63}$")
    client_ipv4: str

    @field_validator("client_ipv4")
    @classmethod
    def validate_client_ipv4(cls, value: str) -> str:
        parsed = ipaddress.IPv4Address(value)
        return str(parsed)
```

Queue only the fixed work kind after resolving the registered Agent.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py -k content_location -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/setup_cm_endpoints.py autopilot-proxmox/tests/test_agent_v1_endpoints.py
git commit -m "feat: queue content location diagnostics"
```

### Task 2: Agent request and result contract

**Files:**
- Modify: `autopilot-agent/src/AutopilotAgent/SetupCmDiagnosticsWorkService.cs:9-183`
- Modify: `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs:517-565`

**Interfaces:**
- Consumes: work kind `setup_cm_content_location_diagnostics` with the controller body.
- Produces: fixed script resource `AutopilotAgent.SetupCmContentLocationDiagnostics.ps1` and a result requiring `site_code`, `target_computer_name`, `client_ipv4`, `client_subnet`, `matching_boundaries`, `boundary_groups`, `distribution_points`, `client_package`, and `errors`.

- [ ] **Step 1: Write failing Agent contract assertions**

```csharp
Assert(SetupCmDiagnosticsWorkService.SupportedKinds.Contains("setup_cm_content_location_diagnostics"), "content location diagnostic kind is not registered");
var request = SetupCmDiagnosticsWorkService.ValidateContentLocationRequest(valid);
Assert(request.ClientIpv4 == "192.168.16.103", "client IPv4 must round-trip");
AssertThrows<InvalidOperationException>(() => SetupCmDiagnosticsWorkService.ValidateContentLocationRequest(new Dictionary<string, JsonElement>(valid) { ["script"] = JsonSerializer.SerializeToElement("bad") }), "content location diagnostics accepted an arbitrary script field");
```

- [ ] **Step 2: Run the contract executable and verify RED**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: FAIL because the supported kind and validator do not exist.

- [ ] **Step 3: Add minimal isolated content-location request/result handling**

```csharp
public sealed record SetupCmContentLocationDiagnosticsRequest(string SiteCode, string TargetComputerName, string ClientIpv4);
// Validate exactly three fields; require an IPv4 address; select the fixed script
// only for this kind; validate the nine required JSON properties before completion.
```

- [ ] **Step 4: Run the contract executable and verify GREEN**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-agent/src/AutopilotAgent/SetupCmDiagnosticsWorkService.cs autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs
git commit -m "feat: validate content location diagnostics"
```

### Task 3: Fixed read-only CM01 diagnostic script

**Files:**
- Create: `autopilot-agent/src/AutopilotAgent/SetupCmContentLocationDiagnostics.ps1`
- Modify: `autopilot-agent/src/AutopilotAgent/AutopilotAgent.csproj:21`

**Interfaces:**
- Consumes: validated `-SiteCode`, `-TargetComputerName`, and `-ClientIpv4` parameters.
- Produces: the Task 2 result contract using only Configuration Manager read cmdlets and a maximum of 100 rows per collection.

- [ ] **Step 1: Write a failing PowerShell parser/resource check**

```powershell
$script = 'autopilot-agent/src/AutopilotAgent/SetupCmContentLocationDiagnostics.ps1'
$tokens = $null; $errors = $null
[System.Management.Automation.Language.Parser]::ParseFile($script, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count) { throw $errors }
if (-not (Select-String -LiteralPath 'autopilot-agent/src/AutopilotAgent/AutopilotAgent.csproj' -Pattern 'SetupCmContentLocationDiagnostics.ps1')) { throw 'Embedded resource missing' }
```

- [ ] **Step 2: Run the check and verify RED**

Run: `pwsh -NoProfile -Command "& { <parser/resource check above> }"`

Expected: FAIL because the fixed script and embedded resource are absent.

- [ ] **Step 3: Implement the smallest read-only script**

The script imports the local Configuration Manager module, derives the supplied
IPv4 /24 subnet, reads matching `SMS_Boundary` rows, boundary-group membership,
referenced DPs, and `LAB00003` distribution status. Each source is caught
independently into `errors`; each collection has a 100-item cap and explicit
safe fields. It emits the fixed result with `ConvertTo-Json -Compress -Depth 6`.

The script must not contain `New-`, `Set-`, or `Remove-` MECM cmdlets.

- [ ] **Step 4: Run parser/resource check and Windows build**

Run: `pwsh -NoProfile -Command "& { <parser/resource check above> }" && dotnet build autopilot-agent/src/AutopilotAgent/AutopilotAgent.csproj -c Release`

Expected: parser succeeds and the Agent builds.

- [ ] **Step 5: Commit**

```bash
git add autopilot-agent/src/AutopilotAgent/SetupCmContentLocationDiagnostics.ps1 autopilot-agent/src/AutopilotAgent/AutopilotAgent.csproj
git commit -m "feat: collect content location evidence"
```

### Task 4: Release and live diagnostic

**Files:**
- Modify: version files required by the repository release process.

**Interfaces:**
- Consumes: green Tasks 1-3.
- Produces: a published Agent MSI matching a deployed controller version and one completed CM01 diagnostic work item.

- [ ] **Step 1: Run targeted and full release gates**

Run: `pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py -k 'setup_cm or content_location' -q && dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj && pytest autopilot-proxmox/tests -q`

Expected: all selected tests pass before versioning.

- [ ] **Step 2: Run CodeRabbit review and independently verify findings**

Run the repository CodeRabbit review command against the branch diff. Apply only an independently verified finding, then rerun the affected tests.

- [ ] **Step 3: Version, commit, push, tag, and deploy**

Run the repository release flow, deploy the controller, and verify `/api/healthz` before publishing and installing the matching Agent MSI on CM01.

- [ ] **Step 4: Queue the read-only diagnostic and collect evidence**

Queue `setup_cm_content_location_diagnostics` for `agent-labz1-cm01` with `LAB`, `RING0IVY24-01`, and `192.168.16.103`. Preserve the completed work-item JSON as the remediation decision record.

- [ ] **Step 5: Commit release metadata**

```bash
git add <version-files>
git commit -m "release: vYYYY.MM.DD"
git push origin codex/setup-cm-agent-execution
```
