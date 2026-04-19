# Unified Hashes Page

**Date:** 2026-04-09
**Status:** Approved

## Summary

Replace the three hash-related pages (`/hashes`, `/upload`, `/hashes/upload`) with a single unified `/hashes` page. One nav link ("Hashes") covers import, browse, per-file group tags, Intune status, selective upload, download, and delete.

## Page Layout

### Import Section

File input at the top of the page for importing CSV files from the user's computer. Accepts `.csv` files from `Get-WindowsAutopilotInfo`. Posts to `POST /api/hashes/upload` (existing endpoint). Shows success/error banner after import.

### Hash Files Table

| Column | Description |
|--------|-------------|
| Checkbox | Select for bulk actions |
| VM Name | Derived from filename (stem minus `_hwid`) |
| Serial | First column of second CSV row |
| OEM | Mapped from serial prefix (PF=Lenovo, SVC=Dell, CZC=HP, MSF=Microsoft, LAB=Generic) |
| Group Tag | Editable `<input type="text">` per row — value sent with upload |
| Intune Status | Live badge: "Registered" (green) if serial found in Autopilot devices, "Not Uploaded" (gray) otherwise |
| Size | File size |
| Modified | Last modified timestamp |
| Actions | Download link |

### Action Bar (below table)

- **Upload Selected to Intune** — uploads checked files with their per-row group tags
- **Delete Selected** — deletes checked files (with confirmation)
- Selection count indicator

## API Changes

### Modified Endpoints

**`GET /hashes`** — unified page. Template context includes:
- `hash_files`: list of hash file dicts (existing `get_hash_files()`)
- `ap_serials`: set of serials from Autopilot devices (from `get_autopilot_devices()`)
- `uploaded`: success message (query param)
- `error`: error message (query param)

**`POST /api/jobs/upload`** — modified to accept selected files and per-file group tags instead of uploading all files. Receives form data:
- `files`: list of filenames to upload
- `group_tags`: list of group tags (parallel array, one per file)

Generates a bash script that calls the upload playbook once per file with the file's group tag. Each upload runs as a separate job for parallelism.

### Existing Endpoints (unchanged)

- `POST /api/hashes/upload` — import CSV files from user's computer
- `POST /api/hashes/delete` — delete selected hash files
- `GET /api/hashes/{filename}` — download hash file

### Removed Routes

- `GET /upload` — replaced by unified `/hashes`
- `GET /hashes/upload` — replaced by unified `/hashes`

## Frontend Changes

### New template: `hashes.html` (rewrite)

Complete rewrite of `hashes.html` to include:
1. Import file input area at top
2. Hash files table with inline group tag inputs and Intune status badges
3. Bulk action bar at bottom
4. Success/error banners

Uses `postAction()` JS helper (already exists in vms.html) for the upload action, plus a custom function to collect selected filenames and their group tags.

### Removed templates

- `upload.html` — no longer needed
- `upload_hashes.html` — no longer needed

### `base.html` nav update

Replace three links:
```
Upload to Intune | Hash Files | Import Hashes
```
With single link:
```
Hashes
```

### CSS additions in `base.html`

```css
.badge { display:inline-block; padding:1px 6px; border-radius:3px; font-size:10px; font-weight:bold; }
.badge-green { background:#d4edda; color:#155724; }
.badge-gray { background:#e2e3e5; color:#383d41; }
```

## Backend Changes (app.py)

### `hashes_page()` handler

Merge logic from all three page handlers. Load hash files, fetch Autopilot device serials for status badges, pass `uploaded`/`error` query params.

```python
@app.get("/hashes", response_class=HTMLResponse)
async def hashes_page(request: Request, uploaded: str = "", error: str = ""):
    hash_files = get_hash_files()
    devices, _ = get_autopilot_devices()
    ap_serials = {d["serial"] for d in devices}
    for f in hash_files:
        f["in_intune"] = f["serial"] in ap_serials
    return templates.TemplateResponse("hashes.html", {
        "request": request,
        "hash_files": hash_files,
        "uploaded": uploaded,
        "error": error,
    })
```

### Upload endpoint modification

Change `POST /api/jobs/upload` to accept selected files and per-file group tags. For each selected file, run the upload playbook with that file and its group tag as a separate job.

### Remove old route handlers

- Remove `upload_page()` handler
- Remove `upload_hashes_page()` handler

## Files to Modify

- `web/app.py` — rewrite `hashes_page`, modify upload endpoint, remove old handlers
- `web/templates/hashes.html` — complete rewrite
- `web/templates/base.html` — update nav, add badge CSS
- `web/templates/upload.html` — delete
- `web/templates/upload_hashes.html` — delete

## Out of Scope

- Drag-and-drop file upload (file input is sufficient)
- Inline editing of CSV content
- Batch group tag (set same tag for all selected) — per-file covers this use case
