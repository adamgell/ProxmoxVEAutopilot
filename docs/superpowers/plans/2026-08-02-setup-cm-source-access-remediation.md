# Setup-CM Source Access Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a narrowly typed Autopilot Agent remediation that grants the proven MECM client-source NTFS read access to one validated domain machine and reports post-change evidence.

**Architecture:** The controller queues only `{site_code,target_computer_name}` under a new work kind. The embedded Agent script owns a fixed local MECM Client path, resolves the domain machine SID, applies only an inherited `ReadAndExecute` allow ACE if missing, and returns read-back evidence.

**Tech Stack:** FastAPI/Pydantic, C# .NET 10 single-file Windows service, embedded PowerShell 7 script, pytest, Agent contract test executable.

## Global Constraints

- Keep `LAB` and `LABZ1-CM01.test.gell.one` bounds; do not accept paths, arbitrary accounts, rights, scripts, or commands.
- Preserve the share ACL and all pre-existing NTFS ACL entries.
- Add only a `ReadAndExecute` allow ACE for the resolved machine SID on the fixed MECM Client directory and its descendants.
- Do not commit installers, credentials, product keys, tokens, or private configuration.

---

### Task 1: Controller contract and queue

**Files:**
- Modify: `autopilot-proxmox/web/setup_cm_endpoints.py`
- Test: `autopilot-proxmox/tests/test_agent_v1_endpoints.py`

**Interfaces:**
- Produces `POST /api/setup-cm/v1/agents/{agent_id}/source-access`.
- Queues `kind="setup_cm_source_access"` with exactly `site_code` and `target_computer_name`.

- [ ] **Step 1: Write the failing endpoint test**

```python
response = agent_client.post(
    "/api/setup-cm/v1/agents/agent-1/source-access",
    json={"site_code": "LAB", "target_computer_name": "RING0IVY24-01"},
)
assert response.status_code == 202
assert response.json()["kind"] == "setup_cm_source_access"
assert response.json()["request"] == {"site_code": "LAB", "target_computer_name": "RING0IVY24-01"}
```

- [ ] **Step 2: Run the focused test and verify it fails with 404.**

- [ ] **Step 3: Add a strict Pydantic body and queue function using the existing diagnostic field constraints.**

- [ ] **Step 4: Run the focused test and verify it passes.**

### Task 2: Agent work contract and fixed remediation script

**Files:**
- Modify: `autopilot-agent/src/AutopilotAgent/SetupCmDiagnosticsWorkService.cs`
- Modify: `autopilot-agent/src/AutopilotAgent/SetupCmSourceDiagnostics.ps1`
- Modify: `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`

**Interfaces:**
- `SetupCmDiagnosticsWorkService.SupportedKinds` includes `setup_cm_source_access`.
- `ValidateRequest` accepts exactly `site_code` and `target_computer_name` for remediation.
- The embedded script dispatches only the named diagnostic/remediation mode.

- [ ] **Step 1: Write a failing contract test that requires the new kind and rejects a request containing `path`.**

- [ ] **Step 2: Run the contract executable and verify it fails because the kind is unsupported.**

- [ ] **Step 3: Add minimal typed dispatch and script mode. The remediation mode derives the fixed path, resolves `<domain>\\<target>$`, applies only an inherited `ReadAndExecute` allow ACE when absent, and emits matching post-change ACE evidence.**

- [ ] **Step 4: Run the contract executable and verify it passes.**

### Task 3: Verify and release

**Files:**
- Modify: `VERSION`
- Modify: `autopilot-agent/Directory.Build.props`

- [ ] **Step 1: Run the focused controller and Agent contract suites.**
- [ ] **Step 2: Run the full controller suite and Windows-targeted Agent build.**
- [ ] **Step 3: Review the staged diff and run CodeRabbit review.**
- [ ] **Step 4: Commit, tag, publish, deploy the exact release, and build/publish the matching MSI from the Windows build host.**
- [ ] **Step 5: Queue remediation, verify its returned post-change ACE, then retry the existing typed client-install workflow.**
