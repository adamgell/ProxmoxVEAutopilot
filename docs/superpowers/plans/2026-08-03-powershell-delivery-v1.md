# Typed Setup-CM Module Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Privately deliver a hash-pinned Setup-CM module to LABZ1-DC02 through Autopilot Agent, atomically repair the vault archive/manifest pair, and retry the typed MECM client work so it records the healthy state already demonstrated in the lab.

**Architecture:** The controller registers a `setup-cm-module` ZIP in its private artifact store and queues `publish_setup_cm_module` solely for `agent-labz1-dc02`. The Agent downloads it with its bearer token, validates hash and runtime ZIP entries, then replaces only the two fixed vault files. The existing Agent work channel is the PowerShell delivery plane; this release does not provide a generic Remote PowerShell shell.

**Tech Stack:** FastAPI, Pydantic, Pytest, PostgreSQL, .NET 8, PowerShell 7, Pester 6, GitHub Actions.

## Global Constraints

- Target only `agent-labz1-dc02`; reject any caller-provided host, path, URL, script, command, or parameter.
- Artifact kind is exactly `setup-cm-module`; never serve it through `/files`.
- Fixed destination files are `C:\\SetupCm\\Modules\\setup-cm.zip` and `C:\\SetupCm\\Modules\\setup-cm.manifest.json`.
- Metadata contains only an exact SHA-256 and source commit; no secrets, keys, credentials, installer media, or tenant configuration.
- Run PowerShell locally under the Agent. A constrained remote PowerShell relay is future work.
- Release only after tests, review, and tagged CI. Preserve failed evidence and use a new work item for each retry.

## File Structure

- `autopilot-proxmox/web/setup_artifacts.py`: immutable package registration and lookup.
- `autopilot-proxmox/web/setup_cm_endpoints.py`: operator-session-protected multipart upload into the private registry.
- `autopilot-proxmox/web/agent_telemetry_pg.py`: active-work authorization query.
- `autopilot-proxmox/web/agent_v1_endpoints.py`: Agent-token private package stream.
- `autopilot-proxmox/web/setup_cm_endpoints.py`: fixed DC02 queue endpoint.
- `autopilot-proxmox/tests/test_setup_artifacts.py`: metadata tests.
- `autopilot-proxmox/tests/test_agent_v1_endpoints.py`: authorization tests.
- `autopilot-proxmox/tests/test_setup_cm_endpoints.py`: queue tests.
- `autopilot-agent/src/AutopilotAgent/AgentApiClient.cs`: authenticated file download.
- `autopilot-agent/src/AutopilotAgent/SetupCmModulePublishWorkService.cs`: typed validation and atomic replace.
- `autopilot-agent/src/AutopilotAgent/Worker.cs`: new-kind routing.
- `autopilot-agent/src/AutopilotAgent/Program.cs`: publisher-service registration.
- `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`: Agent contracts.

### Task 1: Add the private module-artifact contract

**Files:**
- Modify: `autopilot-proxmox/web/setup_artifacts.py`
- Test: `autopilot-proxmox/tests/test_setup_artifacts.py`

**Interfaces:**
- Produces `get_artifact(artifact_id: str, *, kind: str | None = None) -> dict | None`.
- Produces `validate_setup_cm_module_metadata(metadata: dict[str, Any], actual_sha256: str) -> dict[str, str]`.

- [ ] **Step 1: Write the failing test**

```python
def test_module_metadata_requires_matching_hash_and_git_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", tmp_path)
    archive = tmp_path / "setup-cm.zip"
    archive.write_bytes(b"module")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="source_commit"):
        setup_artifacts.register_existing_artifact(kind="setup-cm-module", path=archive, metadata={"sha256": digest})
    row = setup_artifacts.register_existing_artifact(kind="setup-cm-module", path=archive, metadata={"sha256": digest, "source_commit": "a" * 40})
    assert setup_artifacts.get_artifact(row["artifact_id"], kind="setup-cm-module")["sha256"] == digest
```

- [ ] **Step 2: Confirm red state**

Run: `pytest autopilot-proxmox/tests/test_setup_artifacts.py -k module_metadata -q`

Expected: FAIL because private metadata validation and lookup do not exist.

- [ ] **Step 3: Implement the narrow contract**

```python
def get_artifact(artifact_id: str, *, kind: str | None = None) -> dict | None:
    return next((row for row in list_artifacts(kind=kind) if row.get("artifact_id") == artifact_id), None)

def validate_setup_cm_module_metadata(metadata: dict[str, Any], actual_sha256: str) -> dict[str, str]:
    source_commit = str(metadata.get("source_commit") or "").strip().lower()
    expected = str(metadata.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("setup-cm-module source_commit must be a 40-character Git SHA")
    if expected != actual_sha256.lower():
        raise ValueError("setup-cm-module metadata sha256 does not match the uploaded file")
    return {"source_commit": source_commit, "sha256": actual_sha256.lower()}
```

Call the validator only for kind `setup-cm-module`; save the computed SHA in the registry row.

- [ ] **Step 4: Verify and commit**

Run: `pytest autopilot-proxmox/tests/test_setup_artifacts.py -q`

Expected: PASS.

Commit: `git add autopilot-proxmox/web/setup_artifacts.py autopilot-proxmox/tests/test_setup_artifacts.py && git commit -m "feat: add private Setup-CM module artifact contract"`

### Task 2: Upload the approved package through the authenticated controller

**Files:**
- Modify: `autopilot-proxmox/web/setup_cm_endpoints.py`
- Test: `autopilot-proxmox/tests/test_setup_cm_endpoints.py`

**Interfaces:**
- Consumes the application's existing session middleware, which protects non-exempt `/api/` routes.
- Produces `POST /api/setup-cm/v1/module-artifacts` accepting a single multipart field named `file` and form fields `sha256` and `source_commit`.
- Produces a registered `setup-cm-module` row; it does not return a public download URL.

- [ ] **Step 1: Write the failing upload tests**

```python
def test_module_artifact_upload_requires_matching_hash(client):
    response = client.post(
        "/api/setup-cm/v1/module-artifacts",
        files={"file": ("setup-cm.zip", b"module", "application/zip")},
        data={"sha256": "a" * 64, "source_commit": "b" * 40},
    )
    assert response.status_code == 422

def test_module_artifact_upload_registers_private_artifact(client):
    body = b"module"
    response = client.post(
        "/api/setup-cm/v1/module-artifacts",
        files={"file": ("setup-cm.zip", body, "application/zip")},
        data={"sha256": hashlib.sha256(body).hexdigest(), "source_commit": "b" * 40},
    )
    assert response.status_code == 201
    assert response.json()["kind"] == "setup-cm-module"
    assert "download_url" not in response.json()
```

- [ ] **Step 2: Confirm red state**

Run: `pytest autopilot-proxmox/tests/test_setup_cm_endpoints.py -k module_artifact_upload -q`

Expected: FAIL because no private upload endpoint exists.

- [ ] **Step 3: Implement a bounded multipart upload**

```python
@router.post("/module-artifacts", status_code=201)
async def upload_setup_cm_module_artifact(
    file: UploadFile = File(...),
    sha256: str = Form(...),
    source_commit: str = Form(...),
):
    if Path(file.filename or "").suffix.lower() != ".zip":
        raise HTTPException(status_code=422, detail="file must be a ZIP")
    target = setup_artifacts.safe_artifact_path("setup-cm-module", "setup-cm.zip")
    # Stream at most 64 MiB to target, then register with computed metadata validation.
```

The route relies on global operator-session middleware and does not appear in the Agent exemption list. Delete the partial target on read/validation failure. Do not accept a target filename or kind.

- [ ] **Step 4: Verify and commit**

Run: `pytest autopilot-proxmox/tests/test_setup_cm_endpoints.py -k module_artifact_upload -q`

Expected: PASS.

Commit: `git add autopilot-proxmox/web/setup_cm_endpoints.py autopilot-proxmox/tests/test_setup_cm_endpoints.py && git commit -m "feat: upload private Setup-CM module artifacts"`

### Task 3: Permit package delivery only to an Agent with active typed work

**Files:**
- Modify: `autopilot-proxmox/web/agent_telemetry_pg.py`
- Modify: `autopilot-proxmox/web/agent_v1_endpoints.py`
- Test: `autopilot-proxmox/tests/test_agent_v1_endpoints.py`

**Interfaces:**
- Consumes `get_artifact(artifact_id, kind="setup-cm-module")` and authenticated `device["agent_id"]`.
- Produces `has_active_work_request(conn, *, agent_id, kind, request_key, request_value) -> bool`.
- Produces `GET /api/agent/v1/setup-cm-module-artifacts/{artifact_id}`.

- [ ] **Step 1: Write the failing authorization tests**

```python
def test_module_download_rejects_agent_without_matching_work(client, registered_agent, artifact):
    response = client.get(f"/api/agent/v1/setup-cm-module-artifacts/{artifact['artifact_id']}", headers=agent_headers(registered_agent))
    assert response.status_code == 403

def test_module_download_streams_matching_work_artifact(client, registered_agent, queued_publish_work):
    artifact_id = queued_publish_work["request"]["artifact_id"]
    response = client.get(f"/api/agent/v1/setup-cm-module-artifacts/{artifact_id}", headers=agent_headers(registered_agent))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
```

- [ ] **Step 2: Confirm red state**

Run: `pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py -k module_download -q`

Expected: FAIL because no authenticated private stream exists.

- [ ] **Step 3: Implement work-bound file streaming**

```python
@router.get("/setup-cm-module-artifacts/{artifact_id}")
def download_setup_cm_module_artifact(artifact_id: str, device: dict = Depends(_require_agent)):
    with _conn() as conn:
        allowed = agent_telemetry_pg.has_active_work_request(
            conn, agent_id=device["agent_id"], kind="publish_setup_cm_module",
            request_key="artifact_id", request_value=artifact_id,
        )
    if not allowed:
        raise HTTPException(status_code=403, detail="artifact is not authorized for this agent")
    artifact = setup_artifacts.get_artifact(artifact_id, kind="setup-cm-module")
    if not artifact or not Path(artifact["path"]).is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(Path(artifact["path"]), media_type="application/zip", filename=artifact["filename"])
```

The database query authorizes only `queued`, `leased`, and `running` work. A completed or failed work item grants no later downloads.

- [ ] **Step 4: Verify and commit**

Run: `pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py -k module_download -q`

Expected: PASS.

Commit: `git add autopilot-proxmox/web/agent_telemetry_pg.py autopilot-proxmox/web/agent_v1_endpoints.py autopilot-proxmox/tests/test_agent_v1_endpoints.py && git commit -m "feat: authorize private Setup-CM module downloads"`

### Task 4: Queue publication for the DC02 Agent only

**Files:**
- Modify: `autopilot-proxmox/web/setup_cm_endpoints.py`
- Test: `autopilot-proxmox/tests/test_setup_cm_endpoints.py`

**Interfaces:**
- Consumes a `setup-cm-module` artifact by immutable ID.
- Produces `POST /api/setup-cm/v1/agents/agent-labz1-dc02/module-publications` with kind `publish_setup_cm_module` and fields `artifact_id`, `archive_sha256`, and `source_commit`.

- [ ] **Step 1: Write the failing endpoint tests**

```python
def test_module_publication_rejects_non_dc02_target(client, valid_body):
    response = client.post("/api/setup-cm/v1/agents/agent-other/module-publications", json=valid_body)
    assert response.status_code == 422

def test_module_publication_derives_hash_and_commit_from_registry(client, valid_artifact):
    response = client.post("/api/setup-cm/v1/agents/agent-labz1-dc02/module-publications", json={"artifact_id": valid_artifact["artifact_id"]})
    assert response.status_code == 202
    assert response.json()["kind"] == "publish_setup_cm_module"
    assert response.json()["request"] == {"artifact_id": valid_artifact["artifact_id"], "archive_sha256": valid_artifact["sha256"], "source_commit": valid_artifact["metadata"]["source_commit"]}
```

- [ ] **Step 2: Confirm red state**

Run: `pytest autopilot-proxmox/tests/test_setup_cm_endpoints.py -k module_publication -q`

Expected: FAIL because the DC02-only endpoint does not exist.

- [ ] **Step 3: Implement the fixed queue body and endpoint**

```python
class SetupCmModulePublicationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str = Field(pattern=r"^[0-9a-f-]{36}$")

@router.post("/agents/{agent_id}/module-publications", status_code=202)
def queue_setup_cm_module_publication(agent_id: str, body: SetupCmModulePublicationBody):
    if agent_id != "agent-labz1-dc02":
        raise HTTPException(status_code=422, detail="module publication target must be agent-labz1-dc02")
    artifact = setup_artifacts.get_artifact(body.artifact_id, kind="setup-cm-module")
    if not artifact:
        raise HTTPException(status_code=404, detail="setup-cm-module artifact not found")
    request = {"artifact_id": artifact["artifact_id"], "archive_sha256": artifact["sha256"], "source_commit": artifact["metadata"]["source_commit"]}
```

Get the registered device and enqueue this exact request. Do not accept caller-supplied hashes, URLs, paths, commits, commands, or targets.

- [ ] **Step 4: Verify and commit**

Run: `pytest autopilot-proxmox/tests/test_setup_cm_endpoints.py -k module_publication -q`

Expected: PASS.

Commit: `git add autopilot-proxmox/web/setup_cm_endpoints.py autopilot-proxmox/tests/test_setup_cm_endpoints.py && git commit -m "feat: queue fixed Setup-CM module publication"`

### Task 5: Validate and atomically publish on the Agent

**Files:**
- Modify: `autopilot-agent/src/AutopilotAgent/AgentApiClient.cs`
- Create: `autopilot-agent/src/AutopilotAgent/SetupCmModulePublishWorkService.cs`
- Modify: `autopilot-agent/src/AutopilotAgent/Worker.cs`
- Modify: `autopilot-agent/src/AutopilotAgent/Program.cs`
- Modify: `autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`
- Modify: `autopilot-agent/tests/AutopilotAgent.ContractTests/Program.cs`

**Interfaces:**
- Consumes work request `{ artifact_id: string, archive_sha256: string, source_commit: string }`.
- Produces `SetupCmModulePublishWorkService.SupportedKind`, `ValidateRequest`, `ValidateArchive`, `ProcessAsync`, and `AgentApiClient.DownloadSetupCmModuleArtifactAsync`.

- [ ] **Step 1: Write failing C# contracts**

```csharp
Assert(SetupCmModulePublishWorkService.SupportedKind == "publish_setup_cm_module", "module publication kind is missing");
var request = SetupCmModulePublishWorkService.ValidateRequest(new Dictionary<string, JsonElement>
{
    ["artifact_id"] = JsonSerializer.SerializeToElement("00000000-0000-0000-0000-000000000001"),
    ["archive_sha256"] = JsonSerializer.SerializeToElement(new string('a', 64)),
    ["source_commit"] = JsonSerializer.SerializeToElement(new string('b', 40)),
});
AssertThrows<InvalidOperationException>(() => SetupCmModulePublishWorkService.ValidateRequest(new Dictionary<string, JsonElement>
{
    ["artifact_id"] = JsonSerializer.SerializeToElement(request.ArtifactId),
    ["archive_sha256"] = JsonSerializer.SerializeToElement(request.ArchiveSha256),
    ["source_commit"] = JsonSerializer.SerializeToElement(request.SourceCommit),
    ["destination_path"] = JsonSerializer.SerializeToElement(@"C:\\Windows\\Temp\\x.zip"),
}), "module publication accepted an arbitrary destination");
```

Use temporary ZIPs: a passing ZIP has the matching manifest operation/hash/commit and runtime paths; failing ZIPs omit `scripts/Invoke-SetupCmClient.ps1`, mismatch the manifest hash, or contain `../outside.txt`.

- [ ] **Step 2: Confirm red state**

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: FAIL because the publisher service is absent.

- [ ] **Step 3: Implement typed request, safe download, and ZIP validation**

```csharp
public sealed record SetupCmModulePublishRequest(string ArtifactId, string ArchiveSha256, string SourceCommit);
public static SetupCmModulePublishRequest ValidateRequest(IReadOnlyDictionary<string, JsonElement> values)
{
    RequireOnlyFields(values, "artifact_id", "archive_sha256", "source_commit");
    // Require UUID, 64-hex archive hash, and 40-hex Git commit.
}
```

The authenticated Agent download writes only to a generated file under `C:\\ProgramData\\SetupCm\\downloads`. Reject payloads over 64 MiB; rooted/traversal/duplicate ZIP entries; missing `setup-cm.manifest.json`, `scripts/Invoke-SetupCm.ps1`, `scripts/Invoke-SetupCmClient.ps1`, `src/SetupCm/SetupCm.psd1`, or `src/SetupCm/SetupCm.psm1`; and manifest operation/hash/commit mismatch.

- [ ] **Step 4: Replace only fixed vault files**

```csharp
var moduleRoot = @"C:\\SetupCm\\Modules";
var archiveTarget = Path.Combine(moduleRoot, "setup-cm.zip");
var manifestTarget = Path.Combine(moduleRoot, "setup-cm.manifest.json");
var archiveTemp = archiveTarget + ".next";
var manifestTemp = manifestTarget + ".next";
// Write both validated .next files, close them, then replace the two fixed targets.
```

Use `finally` to clean the download and `.next` files. Evidence records only artifact ID, SHA, source commit, and fixed filenames.

- [ ] **Step 5: Route, verify, and commit**

```csharp
if (string.Equals(work.Kind, SetupCmModulePublishWorkService.SupportedKind, StringComparison.Ordinal))
{
    await setupCmModulePublishWorkService.ProcessAsync(work, cancellationToken);
    return;
}
```

Run: `dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: PASS.

Commit: `git add autopilot-agent/src/AutopilotAgent/AgentApiClient.cs autopilot-agent/src/AutopilotAgent/SetupCmModulePublishWorkService.cs autopilot-agent/src/AutopilotAgent/AgentWorker.cs autopilot-agent/tests/AutopilotAgent.ContractTests && git commit -m "feat: publish Setup-CM modules through Autopilot Agent"`

### Task 6: Release, repair the vault, and prove a typed client result

**Files:**
- Modify: `autopilot-agent/Directory.Build.props`
- Use: `/Users/Adam.Gell/repo/setup-cm/.worktrees/codex-mecm-vc-redist`

**Interfaces:**
- Consumes passing tests, a tagged Agent release, clean Setup-CM source, and typed publication endpoint.
- Produces a matching DC02 vault archive/manifest and a new completed client-install work item for `agent-ring0ivy24-01`.

- [ ] **Step 1: Verify local tests before release**

Run: `pytest autopilot-proxmox/tests/test_setup_artifacts.py autopilot-proxmox/tests/test_agent_v1_endpoints.py autopilot-proxmox/tests/test_setup_cm_endpoints.py -q && dotnet run --project autopilot-agent/tests/AutopilotAgent.ContractTests/AutopilotAgent.ContractTests.csproj`

Expected: PASS.

- [ ] **Step 2: Review, bump, tag, and deploy after success**

Run CodeRabbit review; resolve only verified actionable findings. Bump one calendar version, commit, create the matching tag after checking the remote convention, wait for tagged CI, deploy that exact tag, request fresh MSI build/publication, and verify `latest_agent_release` chooses the exact new version.

- [ ] **Step 3: Self-update DC02 and deliver the package**

Wait until `agent-labz1-dc02` reports the new Agent version and capability. Package clean Setup-CM source using `tools/setup-cm/Publish-SetupCmModule.ps1` under PowerShell 7; verify its commit and hash; register it with the private artifact endpoint; queue only `artifact_id` to the DC02 module-publications endpoint.

- [ ] **Step 4: Read back vault and client proof**

Wait for publication `complete`. With read-only bounded guest checks, verify the DC02 ZIP's SHA equals its manifest SHA and the manifest commit equals uploaded metadata. Queue one new client-install work with that SHA for `agent-ring0ivy24-01`; wait for `complete`; then verify CcmExec running, `AssignedSiteCode=LAB`, `LocationServices.EventLastUsedMP=LABZ1-CM01.test.gell.one`, and server `SMS_R_System` active/non-obsolete registration with `MP_ClientRegistration` and `Heartbeat Discovery`.

## Plan self-review

**Spec coverage:** Tasks 1–4 build private immutable storage, authenticated operator upload, Agent authorization, and strict DC02 queueing. Task 5 builds Agent validation and atomic publishing. Task 6 preserves the release gate and finishes with vault/client/server proof. The generic remote shell remains excluded.

**Placeholder scan:** No TBD/TODO terms, arbitrary target fields, public artifact URLs, or generic scripts are described.

**Type consistency:** The operator uploads `sha256` and `source_commit`; the controller queues only `artifact_id`, `archive_sha256`, and `source_commit`; the Agent validates those exact names and downloads solely by `artifact_id`. Agent authorization uses current real work states `pending` and `claimed`, not invented status names.
