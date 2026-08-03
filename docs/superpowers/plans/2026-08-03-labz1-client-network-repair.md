# LABZ1 Client Network Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the one approved LABZ1 test client's static VNet address and domain DNS through a narrow typed Autopilot Agent work item.

**Architecture:** The FastAPI endpoint queues a body-free work item only for `agent-ring0ivy24-01`. An embedded PowerShell 7 script run by the Autopilot Agent locates the exact known adapter MAC, applies the fixed LABZ1 network contract, and returns bounded post-change connectivity evidence.

**Tech Stack:** FastAPI/Pydantic, pytest, .NET 10 single-file Windows Agent, embedded PowerShell 7, Agent contract-test executable.

## Global Constraints

- Allow only `agent-ring0ivy24-01`; accept no IP address, gateway, DNS, interface name, script, or command from an HTTP caller.
- Mutate only adapter MAC `BC-24-11-9C-43-E6` with `192.168.16.103/24`, gateway `192.168.16.1`, and DNS `192.168.16.12`.
- Do not expose or persist secrets, credentials, tokens, installer media, or arbitrary command output.
- Queue no MECM client install automatically; require repair readback before the existing typed client-install operation is retried.

---

### Task 1: Controller contract and fixed queue

**Files:**
- Modify: `autopilot-proxmox/web/setup_cm_endpoints.py`
- Test: `autopilot-proxmox/tests/test_agent_v1_endpoints.py`

**Interfaces:**
- Produces `POST /api/setup-cm/v1/agents/{agent_id}/client-network-repair`.
- Queues `kind="setup_cm_client_network_repair"` with `{}` only for `agent-ring0ivy24-01`.

- [ ] **Step 1: Write the failing endpoint tests.**

```python
allowed = agent_client.post(
    "/api/setup-cm/v1/agents/agent-ring0ivy24-01/client-network-repair",
    json={},
)
assert allowed.status_code == 202
assert allowed.json()["kind"] == "setup_cm_client_network_repair"
assert allowed.json()["request"] == {}

denied = agent_client.post(
    "/api/setup-cm/v1/agents/another-agent/client-network-repair",
    json={},
)
assert denied.status_code == 422
```

- [ ] **Step 2: Run the focused tests and verify they fail because the route does not exist.**

Run: `python3 -m pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py -k client_network_repair -q`

- [ ] **Step 3: Add a body-free queue endpoint with the fixed target guard.**

- [ ] **Step 4: Re-run the focused tests and verify they pass.**

### Task 2: Agent work contract and fixed PowerShell repair

**Files:**
- Create: `autopilot-agent/src/AutopilotAgent/SetupCmClientNetworkRepair.ps1`
- Modify: `autopilot-agent/src/AutopilotAgent/SetupCmDiagnosticsWorkService.cs`
- Modify: `autopilot-agent/src/AutopilotAgent/AutopilotAgent.csproj`
- Modify: `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`

**Interfaces:**
- `SetupCmDiagnosticsWorkService.SupportedKinds` includes `setup_cm_client_network_repair`.
- `ProcessAsync` routes that kind to the embedded fixed script and validates its bounded result.

- [ ] **Step 1: Write failing Agent contract tests.**

```csharp
Assert(SetupCmDiagnosticsWorkService.SupportedKinds.Contains(
    "setup_cm_client_network_repair"),
    "client-network repair kind is not registered");
Assert(File.ReadAllText(networkRepairScriptPath).Contains("192.168.16.103"),
    "client-network repair lacks the fixed LABZ1 address");
Assert(File.ReadAllText(networkRepairScriptPath).Contains("BC-24-11-9C-43-E6"),
    "client-network repair lacks the fixed target adapter MAC");
```

- [ ] **Step 2: Run the contract executable and verify it fails because the new kind/script is absent.**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj --no-restore`

- [ ] **Step 3: Add the embedded, fixed script and typed dispatch.**

The script must fail closed unless exactly one up adapter has the approved MAC. It must remove only that adapter's prior IPv4 default route/address before setting the fixed network values; it must then read back the exact address, gateway, DNS, domain-controller lookup, and TCP 53/445 status.

- [ ] **Step 4: Re-run the contract executable and verify it passes.**

### Task 3: Release and live proof

**Files:**
- Modify: `VERSION`
- Modify: `autopilot-agent/Directory.Build.props`

- [ ] **Step 1: Run the focused controller and Agent contract suites.**
- [ ] **Step 2: Run the full controller suite and Windows-targeted Agent build.**
- [ ] **Step 3: Review the staged diff with CodeRabbit; address only substantiated findings.**
- [ ] **Step 4: Commit, tag, deploy the exact release, and build/publish the matching Agent MSI through the Windows build host.**
- [ ] **Step 5: Verify `agent-ring0ivy24-01` self-updates before queueing the one body-free network repair.**
- [ ] **Step 6: Verify the repair's strict readback, then queue one fresh SHA-verified typed client install and prove CcmExec, LAB assignment, management point, and server registration.**
