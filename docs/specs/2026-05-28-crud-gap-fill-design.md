# CRUD Gap Fill Across Operator Surfaces

Date: 2026-05-28
Branch: claude/mystifying-keller-8c1a01
Status: Design (awaiting user review before plan)

## Goal

Fill the four real CRUD gaps in the React operator UI that today force operators into YAML edits, retired Jinja snapshots, or one-shot bulk endpoints. Define a canonical CRUD shape once and apply it to each surface so the work decomposes into four independent, shippable PRs.

## Scope

In scope (numbered as Surface 1-4 throughout this doc; ship order is in the Ship Plan below):

1. Files (MSI) DELETE.
2. Install Tracking soft delete with reason and audit fields.
3. Answer ISOs React port plus per-row create / read / delete.
4. OEM Profiles full CRUD with two-tier storage (built-in YAML plus custom Postgres).

Out of scope:

- Read-only or observational surfaces (Dashboard, Jobs, Runs, Monitoring, Signals Hub, Cloud Devices).
- Action-shaped surfaces that are not CRUD-shaped by design (Provision, OSDCloud / OSDeploy cockpits, VM start / stop / refresh, UTM VM lifecycle is already complete).
- Already CRUD-complete surfaces (Bubbles, Sequences, Task Sequences, Credentials, Agents, Hashes, OSDCloud / OSDeploy caches).

## Audit Summary

| Surface | Has today | Missing | Verdict |
| --- | --- | --- | --- |
| Files (MSI) | C upload, R list, R download | D delete, reference check | Gap |
| Install Tracking | R, C run, U item-mark | D run, D item, audit fields | Gap |
| Answer ISOs | R list, bulk prune, rebuild-all | C build-one, D single, R detail, React port | Gap |
| OEM Profiles | YAML edit only | All of CRUD via API and UI | Gap |
| Hashes | C upload, R list / download, D bulk | nothing (hashes immutable) | Complete |
| Bubbles, Sequences, Task Sequences, Credentials, Agents, UTM VMs, OSD caches | full CRUD | nothing | Complete |
| Dashboard, Jobs, Runs, Monitoring, Devices, Provision, OSDCloud / OSDeploy cockpits, VMs | n/a | by design | Skip |

## Canonical CRUD Pattern

Every new endpoint in this spec follows the same shape. This is also how the surfaces already complete (Bubbles, Sequences) are built, so we are aligning, not inventing.

### Backend (FastAPI)

REST verbs per resource:

- `GET /api/<resource>` returns a list.
- `POST /api/<resource>` creates one, status `201`.
- `GET /api/<resource>/{id}` returns one or `404`.
- `PUT /api/<resource>/{id}` full replace, or `PATCH` when partial-update semantics are clearer for the resource.
- `DELETE /api/<resource>/{id}` returns `204` on success, `404` on missing, `409` when referenced by another resource.

Rules:

- Every request body and every response uses a Pydantic model so `npm run generate:openapi` produces typed TS clients without manual edits.
- Validation errors return `422` with the field path.
- Errors are explicit. No silent fallback that masks a failure. Reference checks return `409` with the blocking resource id in the body.
- File-backed storage uses atomic write through a temp file plus rename, guarded by an OS file lock. Postgres-backed storage uses a transaction.

### Frontend (React 19, Vite)

- One List page and one Form page per resource. Form is shared between New and Edit routes; the route decides whether to load existing data first.
- Built on existing primitives: `Shell`, `Panel`, `fetchJson`, `postForm` from `apiClient.ts`. No new UI framework.
- Navigation: register in `frontend/src/routes.ts` with `active: true` and the correct group. Remove the corresponding `RetiredJinjaPages` entry if one exists.
- Delete UX: per-row uses a confirm dialog; bulk uses checkbox selection (match `HashesPage.tsx`). Delete buttons that require a reason open a small modal with a required text field.

### Tests

- pytest endpoint tests per route, covering `200`, `404`, `409`, `422`.
- vitest for page render, form submit, and delete confirm.
- Playwright happy-path E2E per new page (one per surface).
- OpenAPI client regeneration is part of CI: `npm run generate:openapi` must produce a clean diff.

## Surface 1: Files (MSI) DELETE

### Backend

Add `POST /api/files/delete` mirroring the existing `/api/hashes/delete` shape exactly: `Form(files: list[str])` request body, response `{ok, deleted}` for JSON clients, `303` redirect to `/react/files` for HTML form posts. Uses the existing `_safe_path(FILE_SHELF_DIR, filename)` helper to reject path traversal silently (matching how the hashes endpoint handles bad input). Hard delete on disk; only files with the `.msi` suffix are deleted.

No reference check against sequences or ts_engine: MSI references inside sequences live in unstructured `run_script` PowerShell strings, so a structured reference check would produce false negatives. Operator confirmation in the UI is the safety boundary.

### Frontend

Update `FilesPage.tsx`:

- Add a checkbox column, a selection set in component state, and a Delete button mirroring `HashesPage.tsx` lines 62-76.
- Confirm dialog before deletion (`window.confirm`) showing the count.

### Tests

- `tests/test_file_shelf_delete.py`: success, traversal rejected, missing file counts as zero, non-`.msi` extensions skipped, HTML form post returns 303.
- `frontend/src/pages/FilesPage.test.tsx`: selection state + delete posts the expected FormData.

## Surface 2: Install Tracking soft delete

### Storage

Migration in `web/install_tracking_pg.py`:

- Add to both `install_tracking_runs` and `install_tracking_items`:
  - `deleted_at TIMESTAMPTZ NULL`
  - `deleted_by TEXT NULL`
  - `delete_reason TEXT NULL`
- Add a partial index on `(deleted_at IS NULL)` for the default list path so the soft-delete filter does not regress query performance.

The existing list and detail queries get a `WHERE deleted_at IS NULL` clause unless `include_deleted=true` is passed.

### Endpoints

- `DELETE /api/install-tracking/runs/{run_id}` (body: `{reason: string, max 500 chars}`). Sets `deleted_at = now()`, `deleted_by = <session user>`, `delete_reason = reason`. Cascades the same soft-delete to all items in that run.
- `DELETE /api/install-tracking/items/{item_id}` (body: `{reason: string}`). Soft-deletes a single item.
- `GET /api/install-tracking/runs?include_deleted=true` returns deleted rows alongside live rows, with the deletion metadata fields populated.

A reason is required. Empty or whitespace-only returns `422`.

### Frontend

Update the install tracking page (currently `RetiredJinjaPages` for the legacy surface, or its successor if already React) to:

- Show a Delete button per row.
- Open a small modal that takes a required reason string.
- Show a "Show deleted" toggle in the list header. When on, deleted rows render with a strikethrough class and a tooltip showing `deleted_by`, `deleted_at`, and `delete_reason`.

### Tests

- `tests/test_install_tracking_delete.py`: success run delete with cascade, item delete, missing reason returns `422`, deleted row hidden by default, deleted row visible with `include_deleted=true`.
- Vitest for modal + toggle.
- Playwright happy-path delete.

## Surface 3: Answer ISOs React port plus CRUD

Current state: routed under `/react/answer-isos` but served from `RetiredJinjaPages.tsx` (the old Jinja snapshot rendered inside the React shell). This surface needs a real React page.

### Backend

Existing endpoints to keep:

- `GET /api/answer-isos` (list).
- `POST /api/answer-isos/prune` (bulk delete by criteria).
- `POST /api/answer-iso/rebuild` (bulk rebuild from current OEM profiles). Path stays singular for compatibility; new endpoints use the plural form.

New endpoints:

- `POST /api/answer-isos` (body: `{vm_name: string, oem_profile_key: string}`, response: `201` with the new filename and metadata).
- `GET /api/answer-isos/{filename}` (metadata: filename, size_bytes, mtime_iso, oem_profile_key, vm_name, sha256 if cheap).
- `DELETE /api/answer-isos/{filename}` (`204` on success, `404` on missing).

Validation:

- `vm_name` matches the existing VM name regex.
- `oem_profile_key` must resolve through the merged OEM profile loader. If unknown, return `422`.

### Frontend

New `frontend/src/pages/AnswerIsosPage.tsx`:

- Top section: an inline form to build one ISO (VM name + OEM profile dropdown sourced from `GET /api/oem-profiles`). This is why Surface 4 ships before Surface 3 in the ship plan below.
- Table: filename, size, mtime, OEM profile, VM name, per-row Delete.
- Footer: Prune button (existing bulk endpoint) and Rebuild All button.

Remove `/react/answer-isos` from `RetiredJinjaPages.tsx` and from `retiredPages.test.tsx`.

### Tests

- `tests/test_answer_isos_endpoints.py`: build-one success, unknown OEM profile rejected, delete success, delete missing returns `404`, traversal rejected.
- Vitest for the new page.
- Playwright: build one, list shows it, delete it, list empty.

## Surface 4: OEM Profiles full CRUD

This is the largest surface. It introduces a new Postgres-backed storage module so operator-created profiles do not require editing the YAML file in the repo.

### Storage model

Two-tier:

- Built-in profiles stay in `autopilot-proxmox/files/oem_profiles.yml`. They are read-only through the API. Repo remains the source of truth for defaults.
- Custom profiles live in a new Postgres table `oem_profiles_custom` (module: `web/oem_profiles_pg.py`).
- Merged list: built-ins first, then customs. If a custom key collides with a built-in, the custom value wins. The API exposes the source as a field on every row.

Table shape:

```sql
CREATE TABLE oem_profiles_custom (
  key TEXT PRIMARY KEY,
  manufacturer TEXT NOT NULL,
  product TEXT NOT NULL,
  family TEXT NOT NULL,
  sku TEXT NOT NULL,
  chassis_type SMALLINT NOT NULL,
  serial_prefix TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by TEXT NULL,
  updated_by TEXT NULL
);
```

`serial_prefix` is optional and only applied when present, matching the existing YAML convention.

### Endpoints

New module `web/oem_profiles_endpoints.py`:

- `GET /api/oem-profiles` returns merged list. Each row carries `source: "builtin" | "custom" | "override"`.
- `POST /api/oem-profiles` creates a custom profile. If the key collides with a built-in, require `?override=true` and tag the row `override`.
- `GET /api/oem-profiles/{key}` returns the merged view.
- `PUT /api/oem-profiles/{key}` replaces a custom profile. Returns `403` if the key only exists as a built-in.
- `DELETE /api/oem-profiles/{key}` removes the custom row. Returns `403` for built-ins. If the row was an `override`, the built-in becomes visible again on the next list call.

Validation:

- `key`: kebab-case, 1-64 chars, `[a-z0-9-]+`.
- `chassis_type`: integer in `{3, 8, 9, 10, 14, 15, 30, 31, 32, 35}`. This matches the SMBIOS Type 3 chassis-type set the project already supports.
- `manufacturer`, `product`, `family`, `sku`: required non-empty strings, trimmed.

### Callers to migrate

Today there are two loaders in `web/app.py`: `load_oem_profiles()` (defined around line 4448) and `_load_oem_profiles_dict()` (around line 14756). Both read `files/oem_profiles.yml` directly. Known call sites:

- `web/app.py` at lines 510, 5396, 5715, 5823, 9033, 13091, 14864 (mix of API responses and provisioning paths).
- `web/cloudosd_endpoints.py` at line 2898.

The work for this surface includes:

1. Replace both existing loaders with a single merged loader (e.g., `web/oem_profiles_loader.py::load_merged_oem_profiles()`). The merged loader reads the YAML once and caches it, then merges the custom table on every call.
2. Migrate every call site listed above to the merged loader. Grep for `oem_profiles.yml` and `load_oem_profiles` at the start of work to confirm the list is still accurate before merging.
3. Delete the old loaders once all call sites are migrated.

### Frontend

New pages:

- `frontend/src/pages/OemProfilesPage.tsx`: list with a Source column, per-row Edit, per-row Delete (disabled for built-ins). New Custom Profile button.
- `frontend/src/pages/OemProfileFormPage.tsx`: shared by `/react/oem-profiles/new` and `/react/oem-profiles/:key/edit`. The form renders chassis_type as a labelled select using the canonical chassis enum.

Navigation: add three entries under the "Settings" group in `frontend/src/routes.ts` (`/react/oem-profiles`, `/react/oem-profiles/new`, `/react/oem-profiles/:key/edit`).

### Tests

- `tests/test_oem_profiles_endpoints.py`: list merges correctly, create, edit, delete, override flow, built-in deletion rejected, validation cases (bad chassis, empty key, bad regex).
- `tests/test_oem_profiles_loader.py`: merged loader exercised through `load_oem_profiles()`.
- Vitest for list + form.
- Playwright: create custom, list shows it, edit, delete.

## Cross-Cutting Concerns

### OpenAPI client regeneration

Every PR that touches `web/*.py` request or response shapes runs:

```bash
cd autopilot-proxmox/frontend && npm run generate:openapi
```

and commits the resulting `src/generated/` diff. The existing CI flow already runs `tsc --noEmit`, which will fail loudly if the regenerated client drifts from page usage.

### Reference checks

None for Surface 1 (see Surface 1 backend note). If a future surface needs structured reference tracking, it gets its own helper at that point.

### Audit trail conventions

For Install Tracking, the audit fields live on the row itself (`deleted_at`, `deleted_by`, `delete_reason`). For Bubbles, there is already a dedicated `audit-events` endpoint. We are not unifying these here; each resource uses what already exists or what fits its shape. A unified audit table is a separate spec.

### Permission model

All new endpoints require an authenticated operator session. No new role splits in this spec. If the OEM Profiles UI later needs to restrict edits to admins, that is a follow-up; right now any operator who can edit Settings can edit OEM Profiles.

### Navigation final state

After all four PRs ship, `routes.ts` adds OEM Profiles to Settings, removes `/react/answer-isos` from `RetiredJinjaPages`, and otherwise leaves nav unchanged.

## Ship Plan

Four PRs in order, each independently mergeable:

1. **Files DELETE.** Smallest, validates the canonical-pattern claim.
2. **Install Tracking soft delete.** Adds the audit-field pattern; isolated to one storage module.
3. **OEM Profiles full CRUD.** Larger because of the loader migration, but ships before Answer ISOs because the Answer ISO build form depends on the merged profile list endpoint.
4. **Answer ISOs React port.** Replaces the retired-Jinja snapshot; consumes the OEM profile API delivered by PR 3.

Each PR delivers: backend endpoints + Pydantic models + storage layer changes if any + React page changes + tests + regenerated OpenAPI client + updated docs entries.

## Risks and Mitigations

- **OEM Profile loader callers drift.** Mitigation: search call sites at the top of Surface 4 work; the loader is referenced from a single helper today, so the blast radius is bounded.
- **MSI reference check misses a caller.** Mitigation: scan all of `web/*.py` for MSI-filename references at start of Surface 1; if the check is incomplete, fall back to allow-with-warning rather than silently deleting.
- **Soft-delete query plans.** Mitigation: the partial index on `deleted_at IS NULL` keeps the default list cheap; verify with `EXPLAIN ANALYZE` on a seeded dev DB before merge.
- **Answer ISO port regressions.** Mitigation: the existing Jinja snapshot stays available via `/legacy/...` style if needed; the React removal is reversible until the snapshot template is deleted, which we defer to a follow-up.

## Success Criteria

- Each surface has create / read / update / delete (or the subset that makes sense, documented) accessible from the React UI without requiring YAML edits, retired-Jinja fallbacks, or one-shot bulk endpoints.
- Generated OpenAPI client matches the new endpoints exactly; no manual TS edits.
- Existing tests still pass; each surface ships with new pytest + vitest + Playwright coverage.
- Operator can manage OEM profiles, answer ISOs, files, and install-tracking records end-to-end from the browser.
