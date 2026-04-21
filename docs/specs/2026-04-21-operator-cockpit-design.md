# Operator cockpit polish

**Date:** 2026-04-21
**Status:** approved for execution (same-session agent build)
**Scope:** UI only. Backend microservice split is tracked in `docs/specs/2026-04-21-microservice-split-design.md`; referenced here but out of scope.

## Motivation

Today's web UI is 20 individually-built Jinja pages behind a flat 12-tab nav. `base.html` is already well-designed (MS-palette tokens, dark mode, sticky header, typography), but:

- The nav has no grouping — all 12 routes sit side-by-side
- The home page is 3 stats + a link list; not a real dashboard
- Each page improvises its own success/error banners with hardcoded hex colors, breaking dark mode in 12 files (~60 lines of bugs from audit)
- Cross-page patterns (headers, filters, empty states, detail pages) were never defined

Operator time split confirmed by user: 50% provisioning, 20% identity/health, 20% setup, 10% job babysitting. Design decisions favour the 70% daily path.

## Information architecture

Six primary tabs, verb-based grouping, three drop-downs:

```
Dashboard · Provision ▾ · Fleet ▾ · Jobs · Monitoring · Settings ▾
```

| Tab | Contents | Routes |
|---|---|---|
| Dashboard | new status-first home | `/` |
| Provision ▾ | Provision VMs · Build Template · Sequences · Answer ISOs | `/provision`, `/template`, `/sequences`, `/answer-isos` |
| Fleet ▾ | Devices · Hashes · Cloud | `/vms`, `/hashes`, `/cloud` |
| Jobs | job list + detail | `/jobs` |
| Monitoring | monitor view + new service-health strip | `/monitoring` |
| Settings ▾ | Credentials · General | `/credentials`, `/settings` |

**URLs do NOT change** — verb grouping lives only in the nav. Existing bookmarks and deep links still work. Active-tab detection in `base.html` needs a small update so `/template` highlights the "Provision" parent.

Dropdowns: click-to-open, keyboard-accessible, close on outside click. No hover-only menus. Mobile (<700px): collapses to hamburger with the same grouping as a vertical list.

## Dashboard (`/`)

Status-first, five modules stacked:

1. **Service health strip** (full width, compact)
   - Reads `service_health` table (written by web/monitor/builder from the microservice split spec)
   - Chips per service: ok (≤20s heartbeat) / warn (20–50s) / bad (>50s)
   - Right side: running git sha + uptime
   - Gated on table existence: if `service_health` doesn't exist yet, hide the strip entirely (table ships with the microservice-split PR from the other session)
   - Endpoint: `GET /api/services`
   - Polls every 10s

2. **Live now + Fleet health** (two columns)
   - *Live now* — count of running jobs (large monospace number) + queued count chip; inline list of up to 3 running jobs with title, elapsed, progress bar (progress = log-lines vs. historical median for that `job_type`); rows click through to `/jobs/<id>`
   - *Fleet health* — total device count + 2×2 grid of enrollment percentages (AD joined · Autopilot · Intune · MDE); cells deep-link to pre-filtered `/vms`
   - Endpoints: existing jobs data from `jobs.py`; new `GET /api/fleet/summary` reads `device_monitor.db`

3. **Launchpad** (three horizontal CTAs)
   - Provision VMs (primary, MS-blue), Build Template, Capture Hash
   - Each opens the destination page's existing start form — no modals

4. **Recent jobs** (last 5, compact table)
   - id · type+target · status chip · time ago
   - "View all →" link to `/jobs`

5. **Quick shortcuts** (small link row at bottom)
   - Sequences · Answer ISOs · Hashes · Credentials

**Web never probes external APIs** — every dashboard read is a DB read (matches microservice-split rule).

## Shared page patterns

New `templates/_macros.html` holds Jinja macros that pages opt into. New CSS classes land in `base.html` alongside existing tokens.

### Macros

```jinja
{{ page_head(title, subtitle=None, action_href=None, action_label=None) }}
{{ alert(message, level="info") }}                 {# level ∈ ok|warn|bad|info|neutral #}
{{ filter_bar(filters, total=None, shown=None) }}
{{ empty_state(message, action_href=None, action_label=None, hint=None) }}
{{ detail_grid() }}                                {# two-column metadata / active area #}
```

### CSS classes (in `base.html`)

- `.page-head` — top-of-page block; H1 + subtitle on the left, action button on the right, optional filter row below
- `.alert` + `.alert-ok` / `.alert-warn` / `.alert-bad` / `.alert-info` / `.alert-neutral` — uses existing `--ok-bg/fg` etc. tokens so dark mode works for free; dismissible variant with close button
- `.filter-bar` — horizontal row of inputs + select dropdowns + result count, reads query params as source of truth
- `.empty-state` — centered block for 0-row tables
- `.detail-grid` — two-column layout (metadata left, active/log right), stacks on mobile

### Status chips

`.chip` and `.badge` already exist in `base.html:504-518`. Audit pass: every page using inline-colored spans for state swaps to these classes. Fixes dark mode as a side effect.

### SVG icons

Hardcoded `fill` / `stroke` attributes (mainly in `vms.html:102-149`) convert to `currentColor` so they inherit text color and theme-switch automatically.

## Dark-mode harmonization

From the audit, 12 files have bugs (~60 lines). Worst offenders: `devices.html` (8), `vms.html` (7), `job_detail.html` (5), `settings.html` + `monitoring_settings.html` (4 each). Root cause: everyone rolled their own Bootstrap-colored success/error banner (`#d4edda`/`#155724`, `#f8d7da`/`#721c24`) instead of using the `--ok-bg` / `--bad-bg` tokens.

Fix pattern:
1. Replace inline banners with `{{ alert(...) }}` macro calls
2. Bulk-replace `#999`/`#777`/`#666` muted text → `var(--ink-muted)` / `var(--ink-soft)`
3. Replace modal/panel `#fff` → `var(--panel)`, borders `#ccc`/`#ddd` → `var(--line)` / `var(--line-strong)`
4. SVG icons: hardcoded colors → `currentColor`
5. `devices.html` `stateBadge()` JS function: emit class names instead of inline styles

Clean reference pages (already token-correct): `home.html` (before rewrite), `login.html`.

## Rollout — four PRs

| # | PR | Files | Depends on |
|---|---|---|---|
| 1 | Nav restructure | `base.html` (nav markup + dropdown CSS + mobile collapse + active-tab JS) | — |
| 2 | Dashboard | `home.html` rewrite, new `/api/services` + `/api/fleet/summary` in `app.py` | microservice-split `service_health` table (gate with feature check) |
| 3 | Shared macros library | new `templates/_macros.html`, new CSS in `base.html` | — |
| 4 | Dark-mode sweep | 12 template files from audit | PR #3 (uses `alert` macro) |

**Parallelization (same-session agent build):**
- Round 1: **PR 1 + PR 3** — both touch `base.html`; schedule serially to avoid merge conflicts, OR split into two worktrees
- Round 2 (after 3 lands): **PR 2 + PR 4** — fully independent, run in parallel worktrees

## Non-goals

- Full rewrite of any page except `home.html`
- New framework / SPA — stays Jinja + CSS tokens
- Notification / toast system — punt to a later spec
- Marketing / product landing page for `/` — operator-cockpit polish only
- Mobile polish beyond nav collapse — ops tool, desktop-first

## Testing

- **Visual smoke:** each of the 6 nav entries loads and highlights correctly; dropdowns open/close; mobile collapse works; theme toggle flips every new component (banners, page-heads, filter bars, alerts, empty states)
- **Dark-mode regression:** run through the 12 audit pages in dark mode, screenshot to compare — no white cards, no unreadable dark-on-dark text
- **Dashboard gracefully handles missing `service_health` table** (hides strip; rest of page renders)
- **URL permanence:** every old link (`/provision`, `/template`, `/settings`, etc.) still works
- **Existing tests pass:** `pytest autopilot-proxmox/tests` — no template-rendering regressions
