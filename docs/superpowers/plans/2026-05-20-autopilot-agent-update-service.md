# AutopilotAgent Update Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a full-service AutopilotAgent lifecycle flow where the controller publishes a validated MSI release, agents check for newer versions, React shows upgrade/approval/pairing state, and critical infrastructure agents like DC3 can be approved and paired without falling back to Jinja.

**Architecture:** Add controller-side release metadata around setup-produced `agent-msi` artifacts, expose an authenticated agent update-check endpoint, and extend heartbeats/inventory rows with update status. The Windows agent will poll the service after pairing, download a verified newer MSI, run silent `msiexec`, and report update events. React `/vms` becomes the primary operator surface for pending approvals, approved-but-not-paired devices, critical infrastructure pairing, and upgrade availability.

**Tech Stack:** FastAPI, PostgreSQL via `psycopg`, existing `setup_artifacts` registry, React/Vite/TypeScript, Vitest, pytest, .NET 8 worker service, WiX MSI.

---

## Context And Guardrails

- The live DC3 install failed after bootstrap because `autopilotagent-postinstall.ps1` reached `Set-Service -Name AutopilotAgent` and Windows reported that the service did not exist.
- The current local fallback artifact at `autopilot-agent/artifacts/AutopilotAgent.msi` has previously been a tiny placeholder; path existence must not count as release readiness.
- The bearer bootstrap token pasted in chat must be rotated before another manual DC3 install attempt.
- Do not replace the existing WinPE or CloudOSD deployment flow. These changes make the agent asset path safer and add update metadata around it.
- Treat the React shell as the operator-facing source of truth for new approval and pairing workflows.
- Use `/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python` for pytest in this repo.
- At completion, deploy and validate on the live server; local tests alone are not done for this repository.

## File Structure

- Modify `autopilot-proxmox/web/setup_artifacts.py`: identify latest valid AutopilotAgent MSI releases and reject placeholder artifacts.
- Modify `autopilot-proxmox/web/cloudosd_endpoints.py`: serve only validated MSI assets for `/api/cloudosd/assets/autopilotagent.msi`.
- Modify `autopilot-proxmox/web/osdeploy_endpoints.py`: share the same validated MSI lookup for OSDeploy package metadata.
- Modify `autopilot-proxmox/web/agent_telemetry_pg.py`: persist agent update check state and latest published version metadata when needed.
- Modify `autopilot-proxmox/web/agent_v1_endpoints.py`: add update-check API and include update hints on heartbeat responses.
- Modify `autopilot-proxmox/web/app.py`: enrich `/api/vms/fleet` agent rows with release/update/approval pairing status.
- Modify `autopilot-proxmox/frontend/src/contracts.ts`: add typed update and approval state fields.
- Modify `autopilot-proxmox/frontend/src/viewModels.ts`: derive labels for pending, approved waiting for claim, paired, current, and upgrade available.
- Modify `autopilot-proxmox/frontend/src/pages/VmsPage.tsx`: add upgrade metrics, row badges, critical infrastructure approval controls, and detail panel fields.
- Modify `autopilot-agent/src/AutopilotAgent/AgentApiClient.cs`: add update-check and update event calls.
- Create `autopilot-agent/src/AutopilotAgent/AgentUpdateService.cs`: download, verify, and execute MSI upgrade.
- Modify `autopilot-agent/src/AutopilotAgent/Worker.cs`: call update check after heartbeat and before work processing.
- Modify `autopilot-agent/installer/AutopilotAgent.wxs`: verify service registration remains stable during major upgrades.
- Test `autopilot-proxmox/tests/test_agent_release_service.py`: controller release metadata and invalid MSI rejection.
- Test `autopilot-proxmox/tests/test_agent_v1_endpoints.py`: update-check endpoint, heartbeat hints, approval pairing lifecycle.
- Test `autopilot-proxmox/tests/test_cloudosd_endpoints.py`: asset serving refuses placeholder MSI.
- Test `autopilot-proxmox/frontend/src/viewModels.test.ts`: update and approval labels.
- Test `autopilot-proxmox/frontend/src/App.test.tsx`: React renders upgrade/approval states.
- Test `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`: update-check DTO contract and installer command behavior.

---

### Task 1: Validated Agent Release Metadata

**Files:**
- Modify: `autopilot-proxmox/web/setup_artifacts.py`
- Create: `autopilot-proxmox/tests/test_agent_release_service.py`

- [ ] **Step 1: Write failing tests for valid and invalid MSI selection**

Add `autopilot-proxmox/tests/test_agent_release_service.py`:

```python
from __future__ import annotations

from pathlib import Path


def _write_fake_msi(path: Path, *, size: int = 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + (b"\0" * (size - 2)))
    return path


def test_latest_agent_release_ignores_tiny_placeholder(tmp_path, monkeypatch):
    from web import setup_artifacts

    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(setup_artifacts, "REGISTRY_PATH", artifact_root / "artifact_registry.json")

    placeholder = artifact_root / "agent-msi" / "AutopilotAgent.msi"
    placeholder.parent.mkdir(parents=True, exist_ok=True)
    placeholder.write_text("placeholder", encoding="utf-8")
    setup_artifacts.register_existing_artifact(kind="agent-msi", path=placeholder)

    assert setup_artifacts.latest_agent_release() is None


def test_latest_agent_release_returns_newest_x64_release(tmp_path, monkeypatch):
    from web import setup_artifacts

    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(setup_artifacts, "REGISTRY_PATH", artifact_root / "artifact_registry.json")

    old_msi = _write_fake_msi(artifact_root / "agent-msi" / "AutopilotAgent-0.1.2-win-x64.msi")
    new_msi = _write_fake_msi(artifact_root / "agent-msi" / "AutopilotAgent-0.1.3-win-x64.msi")
    arm_msi = _write_fake_msi(artifact_root / "agent-msi" / "AutopilotAgent-0.1.3-win-arm64.msi")
    setup_artifacts.register_existing_artifact(kind="agent-msi", path=old_msi, metadata={"version": "0.1.2", "rid": "win-x64"})
    setup_artifacts.register_existing_artifact(kind="agent-msi", path=arm_msi, metadata={"version": "0.1.3", "rid": "win-arm64"})
    setup_artifacts.register_existing_artifact(kind="agent-msi", path=new_msi, metadata={"version": "0.1.3", "rid": "win-x64"})

    release = setup_artifacts.latest_agent_release(runtime_identifier="win-x64")

    assert release is not None
    assert release["version"] == "0.1.3"
    assert release["runtime_identifier"] == "win-x64"
    assert release["path"] == str(new_msi)
    assert release["sha256"]
    assert release["size_bytes"] >= 4096
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_agent_release_service.py -q
```

Expected: fails because `latest_agent_release` is not defined.

- [ ] **Step 3: Implement release selection**

Add focused helpers to `autopilot-proxmox/web/setup_artifacts.py`:

```python
def _agent_release_rid(row: dict) -> str:
    metadata = row.get("metadata") or {}
    value = str(metadata.get("rid") or metadata.get("runtime_identifier") or "").strip()
    if value:
        return value
    filename = str(row.get("filename") or "").lower()
    if "arm64" in filename:
        return "win-arm64"
    return "win-x64"


def _agent_release_version(row: dict) -> str:
    metadata = row.get("metadata") or {}
    value = str(metadata.get("version") or metadata.get("agent_version") or "").strip()
    if value:
        return value
    filename = str(row.get("filename") or "")
    prefix = "AutopilotAgent-"
    if filename.startswith(prefix):
        remainder = filename[len(prefix):]
        for suffix in ("-win-x64.msi", "-win-arm64.msi", ".msi"):
            if remainder.endswith(suffix):
                return remainder[: -len(suffix)]
    return ""


def _looks_like_msi(path: Path, size_bytes: int) -> bool:
    if size_bytes < 1024:
        return False
    try:
        header = path.read_bytes()[:2]
    except OSError:
        return False
    return header == b"MZ"


def latest_agent_release(*, runtime_identifier: str = "win-x64") -> dict | None:
    candidates: list[dict] = []
    for row in list_artifacts(kind="agent-msi"):
        path = Path(row.get("path") or "")
        if not path.is_file():
            continue
        size_bytes = int(row.get("size_bytes") or path.stat().st_size)
        if not _looks_like_msi(path, size_bytes):
            continue
        rid = _agent_release_rid(row)
        if rid != runtime_identifier:
            continue
        release = dict(row)
        release["path"] = str(path)
        release["runtime_identifier"] = rid
        release["version"] = _agent_release_version(row)
        release["size_bytes"] = size_bytes
        release["sha256"] = row.get("sha256") or _sha256(path)
        candidates.append(release)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.get("created_at") or "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_agent_release_service.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/setup_artifacts.py autopilot-proxmox/tests/test_agent_release_service.py
git commit -m "Add validated AutopilotAgent release metadata"
```

---

### Task 2: Refuse Placeholder MSI Assets

**Files:**
- Modify: `autopilot-proxmox/web/cloudosd_endpoints.py`
- Modify: `autopilot-proxmox/web/osdeploy_endpoints.py`
- Modify: `autopilot-proxmox/tests/test_cloudosd_endpoints.py`
- Modify: `autopilot-proxmox/tests/test_osd_v2_endpoints.py`

- [ ] **Step 1: Write failing CloudOSD asset test**

In `autopilot-proxmox/tests/test_cloudosd_endpoints.py`, add:

```python
def test_cloudosd_agent_msi_asset_rejects_placeholder_from_app_output(
    cloudosd_client,
    monkeypatch,
):
    from web import cloudosd_endpoints

    msi_path = (
        Path(cloudosd_endpoints._APP_ROOT)
        / "output"
        / "cloudosd"
        / "AutopilotAgent.msi"
    )
    monkeypatch.delenv("AUTOPILOT_AGENT_MSI_PATH", raising=False)
    msi_path.parent.mkdir(parents=True, exist_ok=True)
    msi_path.write_text("placeholder", encoding="utf-8")
    try:
        response = cloudosd_client.get("/api/cloudosd/assets/autopilotagent.msi")
        assert response.status_code == 404
        assert "valid AutopilotAgent MSI" in response.json()["detail"]
    finally:
        msi_path.unlink(missing_ok=True)
```

- [ ] **Step 2: Write failing OSDeploy lookup test**

In `autopilot-proxmox/tests/test_osd_v2_endpoints.py`, add:

```python
def test_osdeploy_agent_msi_refuses_placeholder_from_host_repo(tmp_path, monkeypatch):
    from web import osdeploy_endpoints

    host_repo = tmp_path / "repo"
    msi_path = host_repo / "autopilot-agent" / "artifacts" / "AutopilotAgent.msi"
    msi_path.parent.mkdir(parents=True, exist_ok=True)
    msi_path.write_text("placeholder", encoding="utf-8")
    monkeypatch.delenv("AUTOPILOT_AGENT_MSI_PATH", raising=False)
    monkeypatch.setenv("HOST_REPO_MOUNT", str(host_repo))

    try:
        osdeploy_endpoints._asset_path("autopilotagent.msi")
    except Exception as exc:
        assert "valid AutopilotAgent MSI" in str(exc)
    else:
        raise AssertionError("placeholder MSI was accepted")
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_cloudosd_endpoints.py::test_cloudosd_agent_msi_asset_rejects_placeholder_from_app_output tests/test_osd_v2_endpoints.py::test_osdeploy_agent_msi_refuses_placeholder_from_host_repo -q
```

Expected: fails because current lookup accepts existing files without MSI validation.

- [ ] **Step 4: Implement shared validation**

In both endpoint modules, prefer `setup_artifacts.latest_agent_release(runtime_identifier="win-x64")`. Only use fallback paths when they pass:

```python
def _valid_agent_msi(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.stat().st_size < 1024:
        return False
    try:
        return path.read_bytes()[:2] == b"MZ"
    except OSError:
        return False
```

For `autopilotagent.msi`, first return the validated setup release:

```python
try:
    from web import setup_artifacts

    release = setup_artifacts.latest_agent_release(runtime_identifier="win-x64")
    if release:
        return Path(release["path"])
except Exception:
    pass
```

Then require `_valid_agent_msi(candidate)` for configured and fallback paths. If all candidates fail, raise:

```python
raise HTTPException(status_code=404, detail="No valid AutopilotAgent MSI is published.")
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_cloudosd_endpoints.py::test_cloudosd_agent_msi_asset_rejects_placeholder_from_app_output tests/test_osd_v2_endpoints.py::test_osdeploy_agent_msi_refuses_placeholder_from_host_repo -q
```

Expected: both tests pass.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/cloudosd_endpoints.py autopilot-proxmox/web/osdeploy_endpoints.py autopilot-proxmox/tests/test_cloudosd_endpoints.py autopilot-proxmox/tests/test_osd_v2_endpoints.py
git commit -m "Reject invalid AutopilotAgent MSI assets"
```

---

### Task 3: Agent Update Check API

**Files:**
- Modify: `autopilot-proxmox/web/agent_v1_endpoints.py`
- Modify: `autopilot-proxmox/web/agent_telemetry_pg.py`
- Modify: `autopilot-proxmox/tests/test_agent_v1_endpoints.py`

- [ ] **Step 1: Write failing update-check endpoint test**

Add to `autopilot-proxmox/tests/test_agent_v1_endpoints.py`:

```python
def test_agent_update_check_reports_newer_published_msi(agent_client, pg_conn, tmp_path, monkeypatch):
    from web import setup_artifacts

    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(setup_artifacts, "REGISTRY_PATH", artifact_root / "artifact_registry.json")
    msi = artifact_root / "agent-msi" / "AutopilotAgent-0.1.3-win-x64.msi"
    msi.parent.mkdir(parents=True, exist_ok=True)
    msi.write_bytes(b"MZ" + (b"\0" * 4094))
    setup_artifacts.register_existing_artifact(
        kind="agent-msi",
        path=msi,
        metadata={"version": "0.1.3", "rid": "win-x64"},
    )
    token = _approved_agent_with_heartbeat(
        agent_client,
        agent_id="agent-update-old",
        token="agent-update-token",
        vmid=118,
        computer_name="GELL-UPDATE118",
        agent_version="0.1.2",
    )

    response = agent_client.post(
        "/api/agent/v1/update-check",
        headers=_bearer(token),
        json={
            "agent_id": "agent-update-old",
            "installed_version": "0.1.2",
            "runtime_identifier": "win-x64",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "upgrade_available"
    assert body["published_version"] == "0.1.3"
    assert body["download_url"].endswith("/api/cloudosd/assets/autopilotagent.msi")
    assert body["sha256"]
    assert body["size_bytes"] >= 4096
```

- [ ] **Step 2: Write failing no-upgrade test**

Add:

```python
def test_agent_update_check_reports_current_when_versions_match(agent_client, pg_conn, tmp_path, monkeypatch):
    from web import setup_artifacts

    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(setup_artifacts, "REGISTRY_PATH", artifact_root / "artifact_registry.json")
    msi = artifact_root / "agent-msi" / "AutopilotAgent-0.1.3-win-x64.msi"
    msi.parent.mkdir(parents=True, exist_ok=True)
    msi.write_bytes(b"MZ" + (b"\0" * 4094))
    setup_artifacts.register_existing_artifact(
        kind="agent-msi",
        path=msi,
        metadata={"version": "0.1.3", "rid": "win-x64"},
    )
    token = _approved_agent_with_heartbeat(
        agent_client,
        agent_id="agent-update-current",
        token="agent-update-current-token",
        vmid=119,
        computer_name="GELL-UPDATE119",
        agent_version="0.1.3",
    )

    response = agent_client.post(
        "/api/agent/v1/update-check",
        headers=_bearer(token),
        json={
            "agent_id": "agent-update-current",
            "installed_version": "0.1.3",
            "runtime_identifier": "win-x64",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "current"
    assert response.json()["download_url"] is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_agent_v1_endpoints.py::test_agent_update_check_reports_newer_published_msi tests/test_agent_v1_endpoints.py::test_agent_update_check_reports_current_when_versions_match -q
```

Expected: `404 Not Found`.

- [ ] **Step 4: Implement request model and version compare**

In `autopilot-proxmox/web/agent_v1_endpoints.py`, add:

```python
class UpdateCheckBody(BaseModel):
    agent_id: str = Field(min_length=1)
    installed_version: Optional[str] = None
    runtime_identifier: str = "win-x64"
```

Add helpers:

```python
def _version_parts(value: str | None) -> tuple[int, ...]:
    parts: list[int] = []
    for raw in (value or "").replace("-", ".").split("."):
        if raw.isdigit():
            parts.append(int(raw))
        else:
            break
    return tuple(parts)


def _newer_version(published: str | None, installed: str | None) -> bool:
    published_parts = _version_parts(published)
    installed_parts = _version_parts(installed)
    if published_parts and installed_parts:
        return published_parts > installed_parts
    return bool(published and installed and published != installed)
```

- [ ] **Step 5: Implement endpoint**

Add:

```python
@router.post("/update-check")
def update_check(body: UpdateCheckBody, device: dict = Depends(_require_agent)):
    if body.agent_id != device["agent_id"]:
        raise HTTPException(status_code=403, detail="token/agent mismatch")
    release = setup_artifacts.latest_agent_release(
        runtime_identifier=body.runtime_identifier,
    )
    installed = body.installed_version or device.get("agent_version")
    if not release:
        return {
            "schema_version": 1,
            "status": "blocked",
            "reason": "no_valid_agent_msi_published",
            "installed_version": installed,
            "published_version": None,
            "runtime_identifier": body.runtime_identifier,
            "download_url": None,
            "sha256": None,
            "size_bytes": None,
        }
    published = release.get("version") or ""
    available = _newer_version(published, installed)
    return {
        "schema_version": 1,
        "status": "upgrade_available" if available else "current",
        "reason": "" if available else "installed_version_matches_published",
        "installed_version": installed,
        "published_version": published,
        "runtime_identifier": body.runtime_identifier,
        "download_url": "/api/cloudosd/assets/autopilotagent.msi" if available else None,
        "sha256": release.get("sha256") if available else None,
        "size_bytes": release.get("size_bytes") if available else None,
    }
```

- [ ] **Step 6: Run tests to verify they pass**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_agent_v1_endpoints.py::test_agent_update_check_reports_newer_published_msi tests/test_agent_v1_endpoints.py::test_agent_update_check_reports_current_when_versions_match -q
```

Expected: both pass.

- [ ] **Step 7: Commit**

```bash
git add autopilot-proxmox/web/agent_v1_endpoints.py autopilot-proxmox/tests/test_agent_v1_endpoints.py
git commit -m "Add AutopilotAgent update check API"
```

---

### Task 4: Approval Pairing State For React

**Files:**
- Modify: `autopilot-proxmox/web/app.py`
- Modify: `autopilot-proxmox/tests/test_agent_v1_endpoints.py`

- [ ] **Step 1: Write failing inventory test for approved-but-not-paired**

Extend `test_vms_agent_inventory_shows_pending_approved_and_active_states` in `autopilot-proxmox/tests/test_agent_v1_endpoints.py`:

```python
assert rows["agent-ui-approved"]["pairing_status"] == "waiting_for_claim"
assert rows["agent-ui-approved"]["needs_pairing"] is True
assert rows["agent-ui-active"]["pairing_status"] == "paired"
assert rows["agent-ui-active"]["needs_pairing"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_agent_v1_endpoints.py::test_vms_agent_inventory_shows_pending_approved_and_active_states -q
```

Expected: fails because `pairing_status` and `needs_pairing` are missing.

- [ ] **Step 3: Implement pairing status enrichment**

In `autopilot-proxmox/web/app.py`, add helper near `_agent_inventory_rows`:

```python
def _agent_pairing_status(*, approval_status: str, last_heartbeat_at: str, claimed_at: str = "") -> str:
    if last_heartbeat_at:
        return "paired"
    if approval_status == "pending":
        return "waiting_for_approval"
    if approval_status == "approved":
        return "waiting_for_claim" if not claimed_at else "waiting_for_heartbeat"
    if approval_status == "claimed":
        return "waiting_for_heartbeat"
    return "unknown"
```

When building each active or pending row, include:

```python
"pairing_status": _agent_pairing_status(
    approval_status=approval_status,
    last_heartbeat_at=last_heartbeat_at,
    claimed_at=_iso_or_blank(row.get("claimed_at")),
),
"needs_pairing": pairing_status != "paired",
```

Use local variables so `pairing_status` is computed once.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_agent_v1_endpoints.py::test_vms_agent_inventory_shows_pending_approved_and_active_states -q
```

Expected: test passes.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_agent_v1_endpoints.py
git commit -m "Expose AutopilotAgent pairing state"
```

---

### Task 5: React Upgrade And Approval Surface

**Files:**
- Modify: `autopilot-proxmox/frontend/src/contracts.ts`
- Modify: `autopilot-proxmox/frontend/src/viewModels.ts`
- Modify: `autopilot-proxmox/frontend/src/viewModels.test.ts`
- Modify: `autopilot-proxmox/frontend/src/pages/VmsPage.tsx`
- Modify: `autopilot-proxmox/frontend/src/App.test.tsx`

- [ ] **Step 1: Write failing view-model tests**

In `autopilot-proxmox/frontend/src/viewModels.test.ts`, add:

```typescript
test("fleetAgentLabel surfaces approved agents waiting for pairing", () => {
  const row = buildFleetMachineRows({
    vms: [],
    missing_vms: [],
    autopilot_devices: [],
    agents: [{
      agent_id: "agent-dc3",
      approval_status: "approved",
      pairing_status: "waiting_for_claim",
      needs_pairing: true,
      computer_name: "DC3"
    }]
  })[0];

  expect(fleetAgentLabel(row)).toBe("Approved");
});

test("fleetAgentLabel surfaces upgrade availability before plain version", () => {
  const row = buildFleetMachineRows({
    vms: [{
      vmid: 110,
      name: "DC3",
      status: "running"
    }],
    missing_vms: [],
    autopilot_devices: [],
    agents: [{
      agent_id: "agent-vm-110",
      approval_status: "active",
      pairing_status: "paired",
      vmid: 110,
      agent_version: "0.1.2",
      published_agent_version: "0.1.3",
      update_status: "upgrade_available",
      upgrade_available: true,
      last_heartbeat_at: new Date().toISOString()
    }]
  })[0];

  expect(fleetAgentLabel(row)).toBe("Upgrade available");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/frontend
npm test -- src/viewModels.test.ts -t "fleetAgentLabel surfaces"
```

Expected: fails because fields and label logic are missing.

- [ ] **Step 3: Add contract fields**

In `autopilot-proxmox/frontend/src/contracts.ts`, extend `AgentFleetRow`:

```typescript
  readonly pairing_status?: "waiting_for_approval" | "waiting_for_claim" | "waiting_for_heartbeat" | "paired" | "unknown";
  readonly needs_pairing?: boolean;
  readonly update_status?: "current" | "upgrade_available" | "blocked" | "unknown";
  readonly upgrade_available?: boolean;
  readonly published_agent_version?: string;
  readonly update_reason?: string;
  readonly agent_msi_sha256?: string;
  readonly agent_msi_size_bytes?: number;
```

- [ ] **Step 4: Update labels and counts**

In `autopilot-proxmox/frontend/src/viewModels.ts`, update `summarizeFleet`:

```typescript
upgradeAgents: fleet.agents.filter((agent) => agent.upgrade_available === true).length,
pendingApprovals: fleet.agents.filter((agent) => agent.approval_status === "pending").length,
pairingAgents: fleet.agents.filter((agent) => agent.needs_pairing === true).length,
```

Update `fleetAgentLabel`:

```typescript
  if (row.agent.upgrade_available) {
    return "Upgrade available";
  }
  if (row.agent.approval_status === "pending") {
    return "Pending";
  }
  if (row.agent.approval_status === "approved" || row.agent.needs_pairing) {
    return "Approved";
  }
```

- [ ] **Step 5: Run view-model tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/frontend
npm test -- src/viewModels.test.ts -t "fleetAgentLabel surfaces"
```

Expected: both tests pass.

- [ ] **Step 6: Write failing React render test**

In `autopilot-proxmox/frontend/src/App.test.tsx`, add fixture data for DC3:

```typescript
agents: [{
  agent_id: "agent-vm-110",
  approval_status: "approved",
  pairing_status: "waiting_for_claim",
  needs_pairing: true,
  vmid: 110,
  computer_name: "DC3",
  agent_version: "0.1.2",
  published_agent_version: "0.1.3",
  update_status: "upgrade_available",
  upgrade_available: true
}]
```

Assert:

```typescript
expect(await screen.findByText("Upgrade available")).toBeInTheDocument();
expect(screen.getByText("Approved")).toBeInTheDocument();
expect(screen.getByText("Agents needing upgrade")).toBeInTheDocument();
```

- [ ] **Step 7: Implement React page updates**

In `autopilot-proxmox/frontend/src/pages/VmsPage.tsx`, add metrics:

```tsx
<Metric label="Agents needing upgrade" value={String(counts.upgradeAgents)} tone={counts.upgradeAgents ? "bad" : "good"} />
<Metric label="Approvals" value={String(counts.pendingApprovals)} tone={counts.pendingApprovals ? "bad" : "good"} />
<Metric label="Pairing" value={String(counts.pairingAgents)} tone={counts.pairingAgents ? "bad" : "good"} />
```

In the detail agent panel, add:

```tsx
["Published", fallbackText(row.agent?.published_agent_version)],
["Update", fallbackText(row.agent?.update_status)],
["Pairing", fallbackText(row.agent?.pairing_status)]
```

In `Critical Infrastructure`, add status badges for `node.agent?.upgrade_available` and `node.agent?.needs_pairing`, and add an approve button when `approval_status === "pending"` and `approval_id` exists.

- [ ] **Step 8: Run frontend tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/frontend
npm test -- src/viewModels.test.ts src/App.test.tsx
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit**

```bash
git add autopilot-proxmox/frontend/src/contracts.ts autopilot-proxmox/frontend/src/viewModels.ts autopilot-proxmox/frontend/src/viewModels.test.ts autopilot-proxmox/frontend/src/pages/VmsPage.tsx autopilot-proxmox/frontend/src/App.test.tsx
git commit -m "Show agent upgrades and pairing in React fleet"
```

---

### Task 6: Windows Agent Update Client

**Files:**
- Modify: `autopilot-agent/src/AutopilotAgent/AgentApiClient.cs`
- Create: `autopilot-agent/src/AutopilotAgent/AgentUpdateService.cs`
- Modify: `autopilot-agent/src/AutopilotAgent/Worker.cs`
- Modify: `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`

- [ ] **Step 1: Write failing contract tests**

In `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`, add checks equivalent to:

```csharp
var updateJson = """
{
  "schema_version": 1,
  "status": "upgrade_available",
  "published_version": "0.1.3",
  "runtime_identifier": "win-x64",
  "download_url": "/api/cloudosd/assets/autopilotagent.msi",
  "sha256": "abc123",
  "size_bytes": 4096
}
""";
var update = JsonSerializer.Deserialize<AgentUpdateCheckResponse>(
    updateJson,
    JsonOptions.Default);
Require(update is not null, "update check response deserializes");
Require(update.Status == "upgrade_available", "update status preserved");
Require(update.DownloadUrl == "/api/cloudosd/assets/autopilotagent.msi", "download url preserved");
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-agent
dotnet test AutopilotAgent.sln
```

Expected: build fails because `AgentUpdateCheckResponse` does not exist.

- [ ] **Step 3: Add DTO and API client method**

In `AgentApiClient.cs`, add:

```csharp
public async Task<AgentUpdateCheckResponse> CheckForUpdateAsync(
    AgentConfig config,
    string runtimeIdentifier,
    CancellationToken cancellationToken)
{
    var request = new HttpRequestMessage(
        HttpMethod.Post,
        $"{config.ServerUrl.TrimEnd('/')}/api/agent/v1/update-check")
    {
        Content = JsonContent.Create(new
        {
            agent_id = config.AgentId,
            installed_version = ThisAssembly.Version,
            runtime_identifier = runtimeIdentifier,
        }),
    };
    request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", config.AgentToken);
    var response = await httpClient.SendAsync(request, cancellationToken);
    response.EnsureSuccessStatusCode();
    return await response.Content.ReadFromJsonAsync<AgentUpdateCheckResponse>(
        cancellationToken: cancellationToken)
        ?? throw new InvalidOperationException("Update check response was empty.");
}
```

Add record:

```csharp
public sealed record AgentUpdateCheckResponse(
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("published_version")] string? PublishedVersion,
    [property: JsonPropertyName("runtime_identifier")] string RuntimeIdentifier,
    [property: JsonPropertyName("download_url")] string? DownloadUrl,
    [property: JsonPropertyName("sha256")] string? Sha256,
    [property: JsonPropertyName("size_bytes")] long? SizeBytes);
```

- [ ] **Step 4: Create update service**

Create `AgentUpdateService.cs`:

```csharp
using System.Diagnostics;
using System.Security.Cryptography;

namespace AutopilotAgent;

public sealed class AgentUpdateService(AgentApiClient apiClient, HttpClient httpClient, AgentFileLog log)
{
    public async Task CheckAndApplyOnceAsync(AgentConfig config, CancellationToken cancellationToken)
    {
        var runtimeIdentifier = OperatingSystem.IsWindows()
            && System.Runtime.InteropServices.RuntimeInformation.ProcessArchitecture == System.Runtime.InteropServices.Architecture.Arm64
            ? "win-arm64"
            : "win-x64";
        var update = await apiClient.CheckForUpdateAsync(config, runtimeIdentifier, cancellationToken);
        if (!string.Equals(update.Status, "upgrade_available", StringComparison.OrdinalIgnoreCase))
        {
            return;
        }
        if (string.IsNullOrWhiteSpace(update.DownloadUrl) || string.IsNullOrWhiteSpace(update.Sha256))
        {
            log.Warning("Agent update was advertised without a download URL or SHA-256.");
            return;
        }
        var uri = update.DownloadUrl.StartsWith("http", StringComparison.OrdinalIgnoreCase)
            ? update.DownloadUrl
            : $"{config.ServerUrl.TrimEnd('/')}{update.DownloadUrl}";
        var target = Path.Combine(Path.GetTempPath(), "AutopilotAgent-update.msi");
        await using (var stream = await httpClient.GetStreamAsync(uri, cancellationToken))
        await using (var file = File.Create(target))
        {
            await stream.CopyToAsync(file, cancellationToken);
        }
        var actual = await Sha256Async(target, cancellationToken);
        if (!string.Equals(actual, update.Sha256, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("Downloaded AutopilotAgent MSI failed SHA-256 validation.");
        }
        var process = Process.Start(new ProcessStartInfo
        {
            FileName = "msiexec.exe",
            Arguments = $"/i \"{target}\" /qn /norestart",
            UseShellExecute = false,
        }) ?? throw new InvalidOperationException("msiexec.exe did not start.");
        await process.WaitForExitAsync(cancellationToken);
        if (process.ExitCode != 0 && process.ExitCode != 3010)
        {
            throw new InvalidOperationException($"AutopilotAgent MSI update failed with exit code {process.ExitCode}.");
        }
        log.Info($"AutopilotAgent MSI update completed with exit code {process.ExitCode}.");
    }

    private static async Task<string> Sha256Async(string path, CancellationToken cancellationToken)
    {
        await using var stream = File.OpenRead(path);
        var hash = await SHA256.HashDataAsync(stream, cancellationToken);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }
}
```

- [ ] **Step 5: Wire service into Worker**

Inject `AgentUpdateService agentUpdateService` in `Worker`, then after heartbeat:

```csharp
await agentUpdateService.CheckAndApplyOnceAsync(config, stoppingToken);
```

Catch and log update failures without skipping heartbeats permanently:

```csharp
catch (Exception ex)
{
    log.Error(ex, "Agent update check failed.");
}
```

- [ ] **Step 6: Register service in Program**

In `Program.cs`, add:

```csharp
builder.Services.AddSingleton<AgentUpdateService>();
```

- [ ] **Step 7: Run agent tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-agent
dotnet test AutopilotAgent.sln
```

Expected: tests pass.

- [ ] **Step 8: Commit**

```bash
git add autopilot-agent/src/AutopilotAgent/AgentApiClient.cs autopilot-agent/src/AutopilotAgent/AgentUpdateService.cs autopilot-agent/src/AutopilotAgent/Worker.cs autopilot-agent/src/AutopilotAgent/Program.cs autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs
git commit -m "Add AutopilotAgent self-update client"
```

---

### Task 7: MSI Service Registration Regression

**Files:**
- Modify: `autopilot-agent/installer/AutopilotAgent.wxs`
- Modify: `autopilot-agent/scripts/Build-AutopilotAgent.ps1`
- Modify: `autopilot-proxmox/tests/test_autopilot_agent_assets.py`

- [ ] **Step 1: Write failing installer asset test**

In `autopilot-proxmox/tests/test_autopilot_agent_assets.py`, add:

```python
def test_autopilot_agent_wix_installs_windows_service():
    wxs = Path("../autopilot-agent/installer/AutopilotAgent.wxs").resolve()
    text = wxs.read_text(encoding="utf-8")

    assert 'Name="AutopilotAgent"' in text
    assert "<ServiceInstall" in text
    assert "<ServiceControl" in text
    assert 'Start="auto"' in text
    assert 'Account="LocalSystem"' in text
```

- [ ] **Step 2: Run test**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_autopilot_agent_assets.py::test_autopilot_agent_wix_installs_windows_service -q
```

Expected: should pass if WiX file still carries service registration. If it fails, fix the WiX file before proceeding.

- [ ] **Step 3: Add post-build validation guidance**

In `Build-AutopilotAgent.ps1`, after MSI build, emit a validation line:

```powershell
Write-Host "Built AutopilotAgent MSI for $rid at $msiDir. Validate service table before publishing."
```

Do not add Windows-only inspection unless the build host has a stable MSI table reader available.

- [ ] **Step 4: Run asset test**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_autopilot_agent_assets.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-agent/installer/AutopilotAgent.wxs autopilot-agent/scripts/Build-AutopilotAgent.ps1 autopilot-proxmox/tests/test_autopilot_agent_assets.py
git commit -m "Guard AutopilotAgent service MSI registration"
```

---

### Task 8: Focused Verification Bundle

**Files:**
- No production files unless failures require fixes.

- [ ] **Step 1: Run focused backend tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_agent_release_service.py tests/test_agent_v1_endpoints.py tests/test_cloudosd_endpoints.py::test_cloudosd_agent_msi_asset_rejects_placeholder_from_app_output tests/test_osd_v2_endpoints.py::test_osdeploy_agent_msi_refuses_placeholder_from_host_repo tests/test_autopilot_agent_assets.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run frontend tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/frontend
npm test -- src/viewModels.test.ts src/App.test.tsx
```

Expected: all selected tests pass.

- [ ] **Step 3: Run agent tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-agent
dotnet test AutopilotAgent.sln
```

Expected: all tests pass.

- [ ] **Step 4: Build frontend**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/frontend
npm run build
```

Expected: build succeeds with no TypeScript errors.

- [ ] **Step 5: Commit any verification fixes**

If the verification run required fixes:

```bash
git add <changed-files>
git commit -m "Stabilize AutopilotAgent update verification"
```

If there were no fixes, do not create an empty commit.

---

### Task 9: Live Deployment And DC3 Validation

**Files:**
- No repo files unless deployment docs are updated.

- [ ] **Step 1: Rotate exposed bootstrap token**

On the live controller, rotate the fleet bootstrap token because the old proof appeared in chat. Use the existing repo/runtime token flow; do not print the token in logs or chat.

- [ ] **Step 2: Build a real agent MSI through build-host workload**

Queue only the required build and publish workload:

```bash
curl -fsS -X POST 'https://autopilot.gell.one/api/setup/v1/build-host/workloads' \
  -H 'Content-Type: application/json' \
  -d '{"force":true,"kinds":["fetch_source_bundle","build_agent_msi","publish_artifacts"]}'
```

Expected: setup artifact registry contains at least one valid `agent-msi` with size greater than 1024 bytes and SHA-256 metadata.

- [ ] **Step 3: Deploy controller update**

Use the repo’s live deployment path. In this repo, completion means the live checkout under `/opt/ProxmoxVEAutopilot/autopilot-proxmox`, the rebuilt image, and the running container all reflect the commit.

- [ ] **Step 4: Validate release API**

Run against live:

```bash
curl -fsS 'https://autopilot.gell.one/api/cloudosd/assets/autopilotagent.msi' -o /tmp/AutopilotAgent.msi
ls -lh /tmp/AutopilotAgent.msi
file /tmp/AutopilotAgent.msi
```

Expected: file is a real MSI-sized binary, not tiny ASCII text.

- [ ] **Step 5: Re-pair DC3 through React**

Open React `/vms`, find DC3 under `Critical Infrastructure`, approve the pending agent from the React surface, and verify the row moves:

```text
pending -> approved / waiting_for_claim -> paired
```

Expected: DC3 heartbeat arrives, `AutopilotAgent` service exists, and the UI no longer relies on the Jinja approval path.

- [ ] **Step 6: Validate upgrade state**

Install or leave an older agent version on a test VM. Confirm React shows:

```text
Upgrade available
Installed version: older
Published version: latest
```

After the agent update runs, confirm the next heartbeat reports the published version and React clears the upgrade warning.

- [ ] **Step 7: Commit deployment docs only if changed**

If the live run reveals an operator command worth documenting, update `docs/WINDOWS_BUILD_BOX.md` or `docs/FIRST_RUN_E2E.md` and commit:

```bash
git add docs/WINDOWS_BUILD_BOX.md docs/FIRST_RUN_E2E.md
git commit -m "Document AutopilotAgent update validation"
```

---

## Self-Review

- Spec coverage: the plan covers validated MSI publication, agent check-in update detection, React upgrade visibility, React approval and pairing, DC3 critical infrastructure state, agent-side update installation, and live deployment validation.
- Placeholder scan: no placeholder markers or open-ended “add tests” steps remain; each task has concrete files, commands, and expected outcomes.
- Type consistency: backend fields use `update_status`, `upgrade_available`, `published_agent_version`, `pairing_status`, and `needs_pairing`; frontend contracts and view-model tasks use the same names.
- Scope check: this is one connected agent lifecycle feature. It intentionally avoids unrelated CloudOSD, WinPE, task sequence, or bubble architecture changes.
