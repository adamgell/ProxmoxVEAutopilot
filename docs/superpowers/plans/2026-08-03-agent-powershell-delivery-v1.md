# Agent PowerShell Delivery v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the fixed read-only `endpoint_facts` PowerShell 7 runbook to a selected installed Autopilot Agent and return structured results.

**Architecture:** The controller creates a normal `remote_powershell` work item for a registered Agent after validating the sole v1 command ID. The Agent advertises the new kind, validates the request exactly, executes an embedded runbook through `pwsh.exe` with a 60-second process-tree-safe timeout, and returns a bounded JSON result through the existing work API.

**Tech Stack:** C#/.NET 8 Autopilot Agent, PowerShell 7, FastAPI/Pydantic, PostgreSQL work queue, existing .NET contract runner, pytest.

## Global Constraints

- v1 accepts only `command_id: endpoint_facts`; arbitrary script text is prohibited.
- Run locally through Autopilot Agent, not WinRM or QGA.
- Use `pwsh.exe -NoProfile -NonInteractive` and the embedded runbook only.
- Enforce a 60-second timeout and a 64 KiB stdout/stderr ceiling.
- Never log or return credentials, keys, tokens, installer media, or private configuration.
- Live proof targets `agent-ring0ivy24-01` and remains read-only.

---

### Task 1: Define and validate the Agent runbook contract

**Files:**
- Modify: `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`
- Create: `autopilot-agent/src/AutopilotAgent/RemotePowerShellWorkService.cs`
- Create: `autopilot-agent/src/AutopilotAgent/RemotePowerShellEndpointFacts.ps1`
- Modify: `autopilot-agent/src/AutopilotAgent/AutopilotAgent.csproj`

**Interfaces:**
- Consumes: `AgentWorkItem.Request` JSON dictionary.
- Produces: `RemotePowerShellWorkService.ValidateRequest(Dictionary<string, JsonElement>)` and the `remote_powershell` Agent capability.

- [ ] **Step 1: Write the failing contract test**

```csharp
Assert(RemotePowerShellWorkService.SupportedKind == "remote_powershell", "remote PowerShell kind is not registered");
var request = RemotePowerShellWorkService.ValidateRequest(
    new Dictionary<string, JsonElement> { ["command_id"] = JsonSerializer.SerializeToElement("endpoint_facts") });
Assert(request.CommandId == "endpoint_facts", "endpoint facts command did not round-trip");
AssertThrows<InvalidOperationException>(
    () => RemotePowerShellWorkService.ValidateRequest(
        new Dictionary<string, JsonElement> { ["script"] = JsonSerializer.SerializeToElement("Get-ChildItem") }),
    "remote PowerShell accepted arbitrary script input");
```

- [ ] **Step 2: Run the test to verify RED**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: FAIL because `remote_powershell` and its validation API do not exist.

- [ ] **Step 3: Add the minimal Agent implementation**

Add the exact request validator, embedded runbook resource, 60-second bounded process execution, JSON result parser, and `remote_powershell` dispatch branch. The PowerShell runbook emits only the specified endpoint-facts JSON object.

- [ ] **Step 4: Run the contract test to verify GREEN**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: PASS.

- [ ] **Step 5: Build the Agent**

Run: `dotnet build autopilot-agent/src/AutopilotAgent/AutopilotAgent.csproj --no-restore`

Expected: build succeeds without new warnings or errors.

### Task 2: Provide a constrained controller queue endpoint

**Files:**
- Create: `autopilot-proxmox/web/remote_powershell_endpoints.py`
- Modify: `autopilot-proxmox/web/app.py`
- Create: `autopilot-proxmox/tests/test_remote_powershell_endpoints.py`

**Interfaces:**
- Consumes: `POST /api/remote-powershell/v1/agents/{agent_id}/endpoint-facts`.
- Produces: a standard public `remote_powershell` work-item payload with request `{"command_id":"endpoint_facts"}`.

- [ ] **Step 1: Write the failing FastAPI tests**

```python
def test_queue_endpoint_facts_creates_typed_work_for_registered_agent(client, registered_agent):
    response = client.post(f"/api/remote-powershell/v1/agents/{registered_agent}/endpoint-facts")
    assert response.status_code == 202
    assert response.json()["kind"] == "remote_powershell"
    assert response.json()["request"] == {"command_id": "endpoint_facts"}

def test_queue_endpoint_facts_rejects_unknown_agent(client):
    response = client.post("/api/remote-powershell/v1/agents/missing/endpoint-facts")
    assert response.status_code == 404
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run: `python -m pytest autopilot-proxmox/tests/test_remote_powershell_endpoints.py -q`

Expected: FAIL because the route does not exist.

- [ ] **Step 3: Add the endpoint and router registration**

Use the existing `_conn`, `agent_telemetry_pg.get_device`, `agent_telemetry_pg.create_work_item`, and `_public_work_item` pattern. Do not accept a free-form body.

- [ ] **Step 4: Run the focused tests to verify GREEN**

Run: `python -m pytest autopilot-proxmox/tests/test_remote_powershell_endpoints.py -q`

Expected: PASS.

- [ ] **Step 5: Run controller regression tests**

Run: `python -m pytest autopilot-proxmox/tests -q`

Expected: PASS.

### Task 3: Release and prove the read-only LABZ1 delivery

**Files:**
- No source files beyond Tasks 1-2.

**Interfaces:**
- Consumes: published Agent version and the queue endpoint.
- Produces: a complete `remote_powershell` work item for `agent-ring0ivy24-01` with structured endpoint facts.

- [ ] **Step 1: Commit the verified changes**

```bash
git add autopilot-agent autopilot-proxmox docs/superpowers
git commit -m "feat: add bounded remote PowerShell endpoint facts"
```

- [ ] **Step 2: Publish directly through the existing build-host release flow**

Use the tagged source bundle, `fetch_source_bundle`, and `build_agent_msi` work items. Do not wait for GitHub Actions.

- [ ] **Step 3: Verify the client Agent self-updates**

Query the latest `agent_ring0ivy24-01` heartbeat and confirm it reports the published version.

- [ ] **Step 4: Queue one endpoint-facts work item**

Call the controller endpoint for `agent-ring0ivy24-01`; wait for terminal state and read the structured result from `agent_work_items`.

- [ ] **Step 5: Report only live proof**

Report status, Agent version, returned computer/PowerShell facts, and work-item state. Do not claim streaming, cancellation, arbitrary scripts, or mutation controls until their separate slices are built and proven.
