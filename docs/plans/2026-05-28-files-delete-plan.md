# Files (MSI) DELETE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-file and bulk delete to the MSI file shelf via a new `POST /api/files/delete` endpoint and a checkbox-driven Delete button on `FilesPage.tsx`, mirroring the existing Hashes delete pattern exactly.

**Architecture:** Single new FastAPI endpoint in `web/app.py` next to the existing hashes/files endpoints. React page gains selection state and a Delete button. No new modules, no new dependencies, no schema changes. Reference checks against sequences are dropped: MSI references live inside `run_script` PowerShell strings, which are unstructured and would produce false negatives at best. Hard delete with a confirm dialog is the right shape.

**Tech Stack:** FastAPI + Pydantic + Form parsing (Python 3.12), React 19 + Vite + TypeScript, pytest, vitest, Playwright.

**Spec:** [docs/specs/2026-05-28-crud-gap-fill-design.md](../specs/2026-05-28-crud-gap-fill-design.md) Surface 1.

---

## File Structure

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `autopilot-proxmox/web/app.py` (around line 11280-11295, next to `/files/{filename}` route) | Add `delete_file_shelf_items` endpoint |
| Modify | `autopilot-proxmox/frontend/src/pages/FilesPage.tsx` | Add selection state, Delete button, confirm dialog |
| Modify | `autopilot-proxmox/frontend/src/contracts.ts` | If `FileShelfRow` needs a stable id field (use filename) |
| Create | `autopilot-proxmox/tests/test_file_shelf_delete.py` | pytest coverage for the new endpoint |
| Modify | `autopilot-proxmox/frontend/src/generated/` (auto-generated) | OpenAPI client regeneration |

---

## Task 1: Backend endpoint

**Files:**
- Modify: `autopilot-proxmox/web/app.py` (insert immediately after the existing `/api/files/upload` handler, around line 11343)
- Test: `autopilot-proxmox/tests/test_file_shelf_delete.py`

- [ ] **Step 1: Write the failing test**

Create `autopilot-proxmox/tests/test_file_shelf_delete.py`:

```python
from pathlib import Path

from fastapi.testclient import TestClient


def test_delete_files_removes_listed_msi_files(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "tool-one.msi").write_bytes(b"one")
    (tmp_path / "tool-two.msi").write_bytes(b"two")
    (tmp_path / "keep.msi").write_bytes(b"keep")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data=[("files", "tool-one.msi"), ("files", "tool-two.msi")],
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "deleted": 2}
    assert not (tmp_path / "tool-one.msi").exists()
    assert not (tmp_path / "tool-two.msi").exists()
    assert (tmp_path / "keep.msi").exists()


def test_delete_files_rejects_path_traversal(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    sibling = tmp_path.parent / "outside.msi"
    sibling.write_bytes(b"do-not-touch")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data=[("files", "../outside.msi")],
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": 0}
    assert sibling.exists()
    sibling.unlink()


def test_delete_files_skips_non_msi_extensions(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")
    (tmp_path / "tool.msi").write_bytes(b"delete me")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data=[("files", "notes.txt"), ("files", "tool.msi")],
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": 1}
    assert (tmp_path / "notes.txt").exists()
    assert not (tmp_path / "tool.msi").exists()


def test_delete_files_missing_files_count_as_zero(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data=[("files", "ghost.msi")],
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": 0}


def test_delete_files_html_redirects_to_react_files(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "tool.msi").write_bytes(b"x")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data=[("files", "tool.msi")],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/react/files"
    assert not (tmp_path / "tool.msi").exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run from the `autopilot-proxmox` directory:

```bash
cd autopilot-proxmox && python -m pytest tests/test_file_shelf_delete.py -v
```

Expected: All five tests FAIL with `404` because `/api/files/delete` does not exist yet.

- [ ] **Step 3: Write the endpoint**

Open `autopilot-proxmox/web/app.py`. Find the existing `/api/files/upload` handler at line 11322. Immediately after the closing of `upload_file_shelf_items` (after the line that ends with `return _redirect_with_query("/react/files", uploaded=saved)` or equivalent), insert:

```python
@app.post("/api/files/delete")
async def delete_file_shelf_items(request: Request, files: list[str] = Form(...)):
    deleted = 0
    for filename in files:
        try:
            file_path = _safe_path(FILE_SHELF_DIR, filename)
        except ValueError:
            continue
        if file_path.exists() and file_path.suffix.lower() == ".msi":
            file_path.unlink()
            deleted += 1
    if _request_wants_json(request):
        return {"ok": True, "deleted": deleted}
    return RedirectResponse("/react/files", status_code=303)
```

This mirrors the existing `/api/hashes/delete` handler (line 11253) exactly, swapping `HASH_DIR`→`FILE_SHELF_DIR` and `.csv`→`.msi`, and redirecting to `/react/files` instead of `/react/hashes`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_file_shelf_delete.py -v
```

Expected: All five tests PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_file_shelf_delete.py
git commit -m "$(cat <<'EOF'
Add /api/files/delete endpoint for MSI shelf

Mirrors the existing /api/hashes/delete shape: accepts a Form list of
filenames, hard-deletes any matching .msi within FILE_SHELF_DIR, rejects
path traversal via _safe_path. JSON clients get {ok, deleted}; HTML
form posts get a 303 redirect back to /react/files.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Frontend selection + Delete button

**Files:**
- Modify: `autopilot-proxmox/frontend/src/pages/FilesPage.tsx`

- [ ] **Step 1: Write the failing vitest**

Open `autopilot-proxmox/frontend/src/pages/FilesPage.test.tsx`. If the file does not exist, create it with the same shape as `HashesPage.test.tsx` (search for that file first; if it does not exist either, use this minimal harness):

```tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { FilesPage } from "./FilesPage";
import * as apiClient from "../apiClient";

const bootstrap = {
  navigation: [],
  flows: [],
  paths: { "files": "/react/files" },
  version: "test",
} as never;

describe("FilesPage delete", () => {
  it("posts selected filenames to /api/files/delete and reloads the list", async () => {
    const fetchJson = vi.spyOn(apiClient, "fetchJson");
    fetchJson.mockResolvedValueOnce({
      files: [
        { filename: "alpha.msi", size: 100, mtime: "2026-05-28T00:00:00Z" },
        { filename: "beta.msi", size: 200, mtime: "2026-05-28T00:00:00Z" },
      ],
    } as never);
    fetchJson.mockResolvedValueOnce({ files: [] } as never);

    const postForm = vi.spyOn(apiClient, "postForm");
    postForm.mockResolvedValueOnce({ deleted: 2 } as never);

    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<FilesPage bootstrap={bootstrap} />);

    await waitFor(() => expect(screen.getByText("alpha.msi")).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText("Select alpha.msi"));
    fireEvent.click(screen.getByLabelText("Select beta.msi"));
    fireEvent.click(screen.getByRole("button", { name: /^Delete$/ }));

    await waitFor(() => {
      expect(postForm).toHaveBeenCalledWith(
        "/api/files/delete",
        expect.any(FormData),
      );
    });
    const sentForm = postForm.mock.calls[0][1] as FormData;
    expect(sentForm.getAll("files")).toEqual(["alpha.msi", "beta.msi"]);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd autopilot-proxmox/frontend && npm run test -- FilesPage
```

Expected: FAIL because the Delete button and checkboxes do not exist yet.

- [ ] **Step 3: Update `FilesPage.tsx`**

Replace the file at `autopilot-proxmox/frontend/src/pages/FilesPage.tsx` with this version. Changes from current: add a `selected` Set, a delete handler, an `<input type="checkbox">` column header and per-row cell, and a Delete button next to Upload.

```tsx
import { useCallback, useMemo, useRef, useState } from "react";

import { fetchJson, postForm } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, FileShelfRow, FilesResponse } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { bytesLabel, lowerText, textValue } from "../utilityModels";

export function FilesPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const fileInput = useRef<HTMLInputElement | null>(null);
  const [rows, setRows] = useState<readonly FileShelfRow[]>([]);
  const [filter, setFilter] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<readonly string[]>([]);

  const load = useCallback(async () => {
    try {
      const payload = await fetchJson<FilesResponse>("/api/files");
      setRows(payload.files);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load files");
    }
  }, []);

  usePolling(load);

  const query = filter.trim().toLowerCase();
  const filtered = useMemo(
    () => rows.filter((row) => lowerText(row.filename).includes(query)),
    [query, rows],
  );
  const totalBytes = rows.reduce(
    (sum, row) => sum + (typeof row.size === "number" ? row.size : 0),
    0,
  );

  const toggleSelected = (filename: string) => {
    setSelected((current) =>
      current.includes(filename)
        ? current.filter((name) => name !== filename)
        : [...current, filename],
    );
  };

  const uploadFiles = async () => {
    const files = fileInput.current?.files;
    if (!files?.length) {
      return;
    }
    const form = new FormData();
    Array.from(files).forEach((file) => {
      form.append("files", file);
    });
    try {
      const result = await postForm<{ readonly uploaded?: number }>(
        "/api/files/upload",
        form,
      );
      setMessage(`Uploaded ${String(result.uploaded ?? files.length)} MSI file(s)`);
      if (fileInput.current) {
        fileInput.current.value = "";
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "File upload failed");
    }
  };

  const deleteSelected = async () => {
    if (!selected.length) {
      return;
    }
    if (
      !window.confirm(`Delete ${String(selected.length)} MSI file(s)? This cannot be undone.`)
    ) {
      return;
    }
    const form = new FormData();
    selected.forEach((name) => {
      form.append("files", name);
    });
    try {
      const result = await postForm<{ readonly deleted?: number }>(
        "/api/files/delete",
        form,
      );
      setMessage(`Deleted ${String(result.deleted ?? selected.length)} MSI file(s)`);
      setSelected([]);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "File delete failed");
    }
  };

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Files"
      section="Fleet"
      path="/react/files"
    >
      {message ? <p className="notice" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="File shelf metrics">
        <Metric label="Files" value={String(rows.length)} />
        <Metric label="Storage" value={bytesLabel(totalBytes)} />
        <Metric label="Visible" value={String(filtered.length)} />
        <Metric label="Selected" value={String(selected.length)} />
      </section>
      <section className="filter-row" aria-label="File shelf controls">
        <div className="filter-row__top">
          <label className="filter">
            <span>Search files</span>
            <input
              value={filter}
              onChange={(event) => { setFilter(event.target.value); }}
              placeholder="MSI filename"
            />
          </label>
          <span className="result-count">
            {String(filtered.length)} of {String(rows.length)}
          </span>
        </div>
        <div className="utility-upload-row">
          <input
            ref={fileInput}
            type="file"
            accept=".msi,application/octet-stream"
            multiple
            aria-label="Upload MSI files"
          />
          <button
            className="utility-button"
            type="button"
            onClick={() => { void uploadFiles(); }}
          >
            Upload MSI
          </button>
          <button
            className="utility-button utility-button--danger"
            type="button"
            onClick={() => { void deleteSelected(); }}
            disabled={!selected.length}
          >
            Delete
          </button>
        </div>
      </section>
      <Panel title="MSI files">
        <div className="table-wrap">
          <table className="jobs-table utility-table">
            <thead>
              <tr>
                <th scope="col" aria-label="Select" />
                <th scope="col">File</th>
                <th scope="col">Size</th>
                <th scope="col">Modified</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.filename}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Select ${row.filename}`}
                      checked={selected.includes(row.filename)}
                      onChange={() => { toggleSelected(row.filename); }}
                    />
                  </td>
                  <td>
                    <a href={`/files/${encodeURIComponent(row.filename)}`}>
                      {row.filename}
                    </a>
                  </td>
                  <td>{bytesLabel(row.size)}</td>
                  <td>{textValue(row.mtime)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!filtered.length ? <p className="empty">No MSI files.</p> : null}
      </Panel>
    </PageFrame>
  );
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd autopilot-proxmox/frontend && npm run test -- FilesPage
```

Expected: PASS.

- [ ] **Step 5: Run the typechecker**

```bash
cd autopilot-proxmox/frontend && npm run typecheck
```

Expected: 0 errors.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/frontend/src/pages/FilesPage.tsx autopilot-proxmox/frontend/src/pages/FilesPage.test.tsx
git commit -m "$(cat <<'EOF'
Add selection + delete to FilesPage

Adds a checkbox column and a Delete button that posts the selected MSI
filenames to /api/files/delete. Confirm dialog before deletion. Mirrors
the HashesPage selection idiom.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Regenerate OpenAPI client

**Files:**
- Modify: `autopilot-proxmox/frontend/src/generated/` (any files that change)

- [ ] **Step 1: Regenerate the client**

```bash
cd autopilot-proxmox/frontend && npm run generate:openapi
```

Expected: Updates `src/generated/openapi.json` and `src/generated/client/*` with entries for `delete_file_shelf_items_api_files_delete_post`.

- [ ] **Step 2: Run the typechecker to confirm no drift**

```bash
cd autopilot-proxmox/frontend && npm run typecheck
```

Expected: 0 errors.

- [ ] **Step 3: Commit the regenerated client**

```bash
git add autopilot-proxmox/frontend/src/generated
git commit -m "$(cat <<'EOF'
Regenerate OpenAPI client for /api/files/delete

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Full test sweep

- [ ] **Step 1: Run the full backend test suite**

```bash
cd autopilot-proxmox && python -m pytest tests/test_file_shelf.py tests/test_file_shelf_delete.py -v
```

Expected: All tests PASS.

- [ ] **Step 2: Run the full frontend test suite**

```bash
cd autopilot-proxmox/frontend && npm run test
```

Expected: 0 failures.

- [ ] **Step 3: Run the frontend lint**

```bash
cd autopilot-proxmox/frontend && npm run lint
```

Expected: 0 errors. (If existing warnings are present in unrelated files, leave them.)

- [ ] **Step 4: If any step fails, fix the underlying issue and re-run before declaring complete.** Do not use `--no-verify` or skip steps.

---

## Self-Review Checklist

Run these checks against the plan before handing it off:

- Every endpoint behavior in the spec for Surface 1 has a test in Task 1.
- Every UI behavior (selection, Delete button, confirm dialog) has a test in Task 2.
- The endpoint redirect target (`/react/files`) matches the React route and the existing convention used by `/api/hashes/delete`.
- `FILE_SHELF_DIR`, `_safe_path`, and `_request_wants_json` are existing helpers in `web/app.py`; no new helpers needed.
- The plan does not introduce a reference check against sequences/ts_engine (the spec was updated to drop this — see Spec Edit below).

## Spec Edit Required

Before merging this plan, update `docs/specs/2026-05-28-crud-gap-fill-design.md` Surface 1 section:

- Remove the reference-check requirement.
- Remove the `web/reference_checks.py` module from the Cross-Cutting Concerns section.
- Replace with: "Hard delete with a frontend confirm dialog. MSI references inside sequences live in unstructured `run_script` PowerShell strings, so a structured reference check would produce false negatives; we accept operator confirmation as the safety boundary."

This spec edit is part of the same PR.

## Success Criteria

- `POST /api/files/delete` exists and behaves identically to `POST /api/hashes/delete` (sans extension difference).
- React `/react/files` page lets an operator select one or more MSI files and delete them with a confirm dialog.
- All five pytest tests pass; the FilesPage vitest passes; typecheck and lint are clean.
- OpenAPI client is regenerated and committed.
