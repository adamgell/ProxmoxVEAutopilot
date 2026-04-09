# VM Actions with Icon Buttons on /vms Page

**Date:** 2026-04-08
**Status:** Approved

## Summary

Add Start, Shutdown, Force Stop, Reset, and Delete VM actions to the `/vms` page as inline SVG icon buttons grouped by purpose (power | tools | destructive). Actions call the Proxmox API directly — no Ansible playbooks or job system needed.

## Actions

| Action | Proxmox API Call | Icon | Color | Confirmation | Shown When |
|--------|-----------------|------|-------|-------------|------------|
| Start | `POST /nodes/{node}/qemu/{vmid}/status/start` | Play triangle (filled) | #006600 green | No | Stopped |
| Shutdown | `POST /nodes/{node}/qemu/{vmid}/status/shutdown` | Power symbol (stroke) | #cc8800 amber | Yes — "Shutdown VM {vmid}?" | Running |
| Force Stop | `POST /nodes/{node}/qemu/{vmid}/status/stop` | Filled square | #cc0000 red | Yes — "Force stop VM {vmid}? This is like pulling the power plug." | Running |
| Reset | `POST /nodes/{node}/qemu/{vmid}/status/reset` | Refresh arrow (stroke) | #336699 blue | No | Running |
| Capture Hash | `POST /api/jobs/capture` (existing) | Hash # symbol (stroke) | #336699 blue | No | Running |
| Console | `GET /api/vms/{vmid}/console` (existing) | Monitor (stroke) | #336699 blue | No | Running |
| Delete | `DELETE /nodes/{node}/qemu/{vmid}` | X (stroke) | #cc0000 red | Yes — "Delete VM {vmid}? This cannot be undone." | Always |

## API Endpoints

### New endpoints in app.py

All new endpoints accept a VMID path parameter, call the Proxmox API directly, and redirect to `/vms`.

```
POST /api/vms/{vmid}/start
POST /api/vms/{vmid}/shutdown
POST /api/vms/{vmid}/stop
POST /api/vms/{vmid}/reset
POST /api/vms/{vmid}/delete
```

### Backend helpers

Add `_proxmox_api_post(path)` — same as existing `_proxmox_api()` but uses `requests.post`. The existing helper is GET-only.

Add `_proxmox_api_delete(path)` — same pattern but uses `requests.delete`.

### Error handling

On Proxmox API error, redirect to `/vms?error={message}` and display as a red banner at the top of the page (same pattern as `ap_error` already used on the page).

### Delete behavior

The delete endpoint stops the VM first if it's running (`POST .../status/stop`), waits briefly for it to stop, then calls `DELETE .../qemu/{vmid}`. If the VM cannot be stopped, the error is shown.

## Frontend Changes

### vms.html — Actions column

Replace the current text-button Actions cell with grouped inline SVG icon buttons:

**Running VMs:**
```
[Shutdown] [Force Stop] [Reset]  |  [Capture Hash] [Console]  |  [Delete]
```

**Stopped VMs:**
```
[Start]  |  [Delete]
```

Each icon is a `<button>` inside its own `<form method="POST">` pointing at the corresponding endpoint. Destructive actions (Shutdown, Force Stop, Delete) have `onsubmit="return confirm('...')"`.

### Icon specification

All icons are inline SVGs at 13x13px. No external dependencies (no CDN, no icon library).

- **Start:** `<polygon points="5,3 19,12 5,21"/>` filled #006600
- **Shutdown:** Circle + vertical line, stroke #cc8800, stroke-width 2.5
- **Force Stop:** `<rect x="4" y="4" width="16" height="16" rx="2"/>` filled #cc0000
- **Reset:** Refresh arrow path, stroke #336699, stroke-width 2.5
- **Capture Hash:** Four lines forming # symbol, stroke #336699, stroke-width 2.5
- **Console:** Rectangle + stand lines, stroke #336699, stroke-width 2
- **Delete:** Two diagonal lines forming X, stroke #cc0000, stroke-width 2.5

### base.html — New CSS

```css
.action-btn {
  background: none;
  border: 1px solid #ccc;
  border-radius: 3px;
  padding: 3px 5px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  vertical-align: middle;
}
.action-btn:hover {
  background: #dde8f0;
  border-color: #336699;
}
.action-btn.danger:hover {
  background: #f8d7da;
  border-color: #cc0000;
}
```

### Error banner

Add to the top of vms.html content block:

```html
{% if error %}
<p style="background:#f8d7da;border:1px solid #f5c6cb;padding:8px;color:#721c24;">{{ error }}</p>
{% endif %}
```

Pass `error` query param in the template context from `vms_page()`.

## Files to modify

- `autopilot-proxmox/web/app.py` — add `_proxmox_api_post`, `_proxmox_api_delete`, 5 new endpoints, update `vms_page` to pass error param
- `autopilot-proxmox/web/templates/vms.html` — rewrite Actions column with SVG icon buttons, add error banner
- `autopilot-proxmox/web/templates/base.html` — add `.action-btn` CSS classes

## Out of scope

- Bulk start/stop for selected VMs (could be added later)
- Websocket-based status refresh after action (page reload is sufficient)
- Action logging beyond Proxmox's built-in task log
