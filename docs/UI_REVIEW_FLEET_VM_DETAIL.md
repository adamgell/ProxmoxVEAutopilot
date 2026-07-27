# Fleet and VM Detail UI Review

## Verdict

The root cause is that **nothing on either page was ever ranked**. Both pages render everything they know, all at once, at the same visual weight and the same permanence: 94 of 118 `font-weight` declarations in `src/styles.css` are 800 or 900, and `:root` (styles.css:12-24) defines exactly 14 custom properties of which all 14 are colors, so there is no token for weight, size, or spacing with which rank could ever have been expressed. The pages are not badly designed; they are undesigned in the specific sense that no element was ever told it was less important than another. Everything downstream follows from that: the Fleet page renders its own table last, behind three configuration panels (VmsPage.tsx:1723-1804); the detail page gives 30 static key/value rows a four-column grid and gives the live picture of the machine a 220px dashed empty box (VmsPage.tsx:2444-2495). Fix the ranking and most of the "busy" complaint dissolves without deleting a single feature.

## What is actually on these pages today

### Fleet list (/react/vms)

| Order | Region | Source | Weight on screen |
|---|---|---|---|
| 1 | Progress bar + up to 5 notice paragraphs | VmsPage.tsx:1679-1691 | 14px, **all red** (styles.css:1745-1748), ~171px |
| 2 | Metric strip, **10 tiles in a 7-column grid** | VmsPage.tsx:1693-1704, styles.css:2732-2735 | 10 numbers at 25px, 9 saturated, 4 empty cells expose `--line` as a bare slab |
| 3 | Filter row, **no result count** | VmsPage.tsx:1706-1721 | Refresh sits where the count belongs |
| 4 | BubbleTopologyOverview: 3 headed panels + nested credential CRUD | VmsPage.tsx:1723-1780 (46 props) | Full width, pushes table 2-3 screens down |
| 5 | Fleet machine table, **13 columns** | VmsPage.tsx:1905-1923, MachineRow :2254-2337 | `min-width: 1520px` (styles.css:3236) in a ~1272px column: cols 11-13 off screen |

Per row: 13 content objects, 5 bordered sub-shapes (3 `.status` pills + 2 `.fleet-action` buttons). At 20 rows that is 260 objects and 100 nested outlines. Five headed panels render simultaneously with live data. Seven call-to-action controls render on load; the only one with an icon is `Add agent` (VmsPage.tsx:1870-1871), the rarest repair operation on the page.

### VM detail (/react/vms/:vmid)

| Order | Region | Source | Weight on screen |
|---|---|---|---|
| 1 | Breadcrumb + hero | VmsPage.tsx:2394-2410 | Reprints 7 fields from the row you just clicked |
| 2 | Toolbar, **15 buttons** (17 on Ubuntu + pending agent) | VmsPage.tsx:2412-2442 | One flat `flex-wrap` row, no grouping; `Delete agent` and `Delete VM` adjacent and identical |
| 3 | Action zone | VmsPage.tsx:2444-2454 | **220px dashed empty box on first paint** (`activeAction` inits null at :544) |
| 4 | `.vm-detail-grid`, 4 panels / **30 key/value rows** | VmsPage.tsx:2456-2495 | `repeat(4, 1fr)`; `dt` 11px/900 vs `dd` 12px/800 (styles.css:3589-3601) |
| 5 | 5 evidence panels | VmEvidencePanels.tsx:171-275 | Timeline is **last** and hard-capped at 8 (:126) |

Redundancy is the dominant fact: the 9 `Essentials` rows (VmsPage.tsx:2458-2466) are the same 9 table headers in the same order (:1912-1920) calling the same accessors. On a lab VM with no agent, all 9 `Agent` rows render `-`; with no Autopilot record, all 6 `Intune` rows render `-`; `Directory evidence` adds 4 more. That is 19 rows of dashes.

## Root causes

Ranked by contribution.

**1. No ranking in the JSX.** The primary object of each page renders last or renders empty. Fleet puts the machine table (VmsPage.tsx:1782-1804) behind three configuration panels (:1723). Detail puts the VNC canvas as the **last child** of the console panel (VmActionWorkspace.tsx:523), under a status line, a toolbar, and a `.vm-console-controls` block (:433-522) totalling roughly 390px, above a canvas whose `min-height` is 280px (styles.css:4172-4179). This alone is most of the complaint.

**2. No typographic scale, so nothing can be emphasized.** 94/118 weights at 800+; exactly one rule in 5406 lines sets a normal weight (`.filter input`, styles.css:4471). 127/167 font sizes sit in a 10-13px band. At 12px/800 you will find, simultaneously: the global `h3` section heading (styles.css:81-87), `.machine-primary-value` table data (:3326), `.breadcrumb` (:3501), and `.vm-detail-list dd` (:3595). Four different roles, one appearance. Compounding it, `.bubble-card h3` (:2783) and `.fleet-card h3` (:3398) override only margin, color, size and line-height, so they **inherit uppercase and weight 800** from the global `h3` while their parent `Panel` `h2` (styles.css:74-78) sets no weight at all and takes the browser default. Children visually outrank their parents.

**3. Color has no neutral resting state.** `.status` carries `border: 1px solid var(--line)` unconditionally (styles.css:1707-1721), so every pill draws an outline even when it means nothing. The Agent pill can never be neutral: `agentLabel === "Stale" || agentLabel === "None" ? "status status--bad" : "status status--good"` (VmsPage.tsx:2303, repeated verbatim in the detail hero at :2407). Six metric tiles use `x ? "bad" : "good"` (:1696-1703) and so can never render neutral either. A completely healthy fleet still lights up green everywhere, which means red stops meaning anything. `--warn` (styles.css:23) is referenced exactly once in the whole file. (Correction to one input claim: the Runtime pill at :2298 and Managed By pill at :2279 **do** already have neutral branches. Only Agent is unconditional.)

**4. The same data is rendered two and three times.** The `Essentials` panel duplicates the table row (above). Every table row carries two links to the identical destination: `machine-name--link` to `/react/vms/{vmid}` (:2268) and `machine-vmid-link` via `reactHrefForUiPath("/devices/{vmid}")` (:2291), which `routes.ts:455` rewrites to the same URL. Credentials render twice with **independent reveal state** hitting the same endpoint (VmActionWorkspace.tsx:207/508 and VmEvidencePanels.tsx:127/222), so revealing a password in one leaves it masked in the other.

**5. Space has no scale, so proximity carries no grouping signal.** `.fleet-card-list` gap is 12px (styles.css:3221), `.fleet-card` padding is 12px and its internal gap is 12px (styles.css:3376-3384). Three hierarchy levels at exactly the same value. `.bubble-fleet-grid` gap 14px equals `.bubble-card` padding 14px equals its gap 14px (styles.css:2754-2773). Section separation is only 18-22px against 12-14px intra-card, a ratio of about 1.5:1 where 2.5:1 or more is needed to read as separation.

**6. Borders are the only elevation tool, and they nest.** 143 `border` declarations against 9 `box-shadow`. `.fleet-detail-grid` (styles.css:3416-3424) fakes table rules with `gap: 1px` over a background, drawing 6 visible line segments around 4 short strings, and renders three times on the Fleet page (VmsPage.tsx:2958, :3010, :3114). On detail, the console credential row sits inside four concentric bordered boxes at radii 8/7/6/6 with alternating fills (styles.css:3854, 4047, 4121, 3799).

**Bonus finding, verified and not in the inputs:** IBM Plex Sans is declared at styles.css:3-9 but there is **no `@font-face` and no webfont import anywhere in the project**. The face actually rendering is `system-ui`. This is harmless but means any advice premised on "IBM Plex ships no 750 weight" is moot; `system-ui` is a variable font and renders 750 fine. The case for the weight sweep rests on the 80-percent-at-800 concentration, not on face availability.

## Recommendations

**Direction taken.** The spine is **"Quiet Console: declutter by subtraction"** (proposal 1), because it is the only one of the three that delivers visible relief without a file split, without new routes, and without a design-system refactor landing first. Onto that spine I grafted:

- from **"One page, one job"** (proposal 2): the framing that each route serves exactly one job, the detail page's 6-section order, attention-first sorting, the preset chip row, and the `?view=` segmented rail (deferred to Phase 2, not Phase 1);
- from **"Fix the system"** (proposal 3): the `:root` token block and the observation that `Panel` (styles.css:1466-1470, border-top only, no box, no fill) is the one restrained surface in the system and should be the model everything else converges on.

**Rejected:** proposal 3's front-loading of a 5-primitive `ui.tsx` refactor across all 25 pages. The blast radius is the whole app, there is zero visual-regression coverage, and the user asked about two pages. Its token block is worth taking; its `<Table>` primitive across 37 call sites is not, at least not first. Also rejected: proposal 3's `--w-body: 450`, which was a guess.

### Phase 1 - Ship this week (highest payoff, lowest risk)

Every item here is local, reversible, and touches no shared component. Together they are roughly a day.

| # | Change | Files | Effort | Payoff |
|---|---|---|---|---|
| 1 | **Metric strip 10 tiles to 4.** Keep Proxmox VMs, Running, Attention, Agents. Delete the `.metric-strip--fleet` override so it inherits the 4-column house default (styles.css:1325) that Dashboard and Labs already use. Change surviving tones from `x ? "bad" : "good"` to `x ? "bad" : "neutral"`. | VmsPage.tsx:1693-1704, styles.css:2732-2735 | S | Removes ~186px of the loudest region on the page and the bare-border slab. Biggest visible win per line changed. |
| 2 | **Table above topology.** Move the `<section className="fleet-lanes">` block ahead of `<BubbleTopologyOverview>`. Wrap topology in a native `<details>`. | VmsPage.tsx:1723-1804 | S | Removes a 2-3 screen scroll from 3 of the page's 4 click paths. |
| 3 | **Filter joins its table, and reports a count.** Move `.filter-row` inside the Fleet machines Panel next to `.fleet-lane-command` (:1869); put `{filteredMachines.length} of {machineRows.length}` in the existing `.result-count` slot; move Refresh to the PageFrame action slot (:1677). | VmsPage.tsx:1677, :1706-1721, :1869 | S | `.result-count` already exists (styles.css:4453) and is the house pattern on 7 other list pages (HashesPage.tsx:119, CredentialsPage.tsx:163, FilesPage.tsx:334, ...). Fleet is the only one missing it. |
| 4 | **Notices stop being red.** Delete `color: var(--bad)` from styles.css:1747 so the base inherits `--muted`; make `.notice--bad` real; drop size 14px to 13px; apply `--bad` only to the fetch error (:1680); collapse the 3 advisory notices into one line; auto-clear `actionStatus` on a timer. | styles.css:1738-1752, VmsPage.tsx:1680-1691, :777-790 | S | "Rename VM 113 complete" currently renders in salmon red, larger than every table value, and never clears. |
| 5 | **Neutral resting state.** Add `border-color: transparent` to base `.status`. Add `fleetAgentTone()` to viewModels.ts (bad for None/Stale, warn for Pending/Upgrade available, neutral otherwise) and use it at VmsPage.tsx:2303 and :2407. Add the missing `.status--warn` using the `--warn` token. Change `readinessClass` (:2521) to return bare `"status"` when ok. Drop `--active` from `.machine-name--link` (styles.css:3300) in favour of underline-on-hover. | styles.css:1707-1736, :3300; viewModels.ts; VmsPage.tsx:2303, :2407, :2521 | S | Takes the table from ~60 permanent pill outlines and ~40 cyan runs to near zero at rest. One line of CSS does most of it. |
| 6 | **Delete the three duplicates.** The `Essentials` DetailPanel (VmsPage.tsx:2457-2467); the `Known credentials` evidence Panel (VmEvidencePanels.tsx:222-237, keep the console copy which can type into the guest); the VMID link cell (:2289-2293, make it plain text in a subline). | VmsPage.tsx, VmEvidencePanels.tsx | S | Detail drops 30 key/value rows to 21 in one edit. Removes a genuine correctness bug (split reveal state). |
| 7 | **Gate the all-dashes panels.** Wrap the Agent panel in `{row.agent ? ... : null}` and Intune in `{row.autopilotDevice ? ... : null}`. Change `.vm-detail-grid` to `repeat(auto-fit, minmax(260px, 1fr))` so absent panels leave no holes. Replace the positional `.vm-evidence-grid .panel:nth-child(5)` rule with an explicit modifier class first. | VmsPage.tsx:2476-2494, styles.css:3564-3568, :3614-3616 | S | A lab VM stops paying for 19 rows of `-`. |
| 8 | **Canvas first, controls second.** Move the `screenRef` div from last child to directly after the status line; update `grid-template-rows` (styles.css:3990-3994); wrap `.vm-console-controls` in a closed `<details>`; delete the duplicate "Open legacy console" link (:646, first is at :431). | VmActionWorkspace.tsx:415-524, styles.css:3990 | S | Removes ~390px of chrome from above the one thing the page exists to show. |
| 9 | **The stage never renders empty.** Read `?action=` on mount and seed `activeAction`, mirroring CloudosdPage.tsx:796. Default to screenshot mode when running (`evidence.latest_screenshot` is already fetched). | VmsPage.tsx:544, :590 | S | `routes.ts:456` already maps `/vms/{vmid}/console` to `?action=console` and VmsPage never reads `location.search` (verified: zero matches). The deep link is currently dead. |
| 10 | **Delete the dead control.** Remove `Update agent` (:2437). `updateAgent` (:1404-1413) only calls `setAgentFormDraft`, whose sole render site `FleetAgentFormModal` (:1806) sits inside the list branch, past the detail branch's return at :1614-1668. | VmsPage.tsx:2437 | S | Clicking it currently does nothing at all. |
| 11 | **Three free correctness fixes.** (a) One global rule: `:where(button,a,summary,[tabindex]):focus-visible { outline: 2px solid var(--active); outline-offset: 2px }` (10 `:focus` rules exist, none match any button family). (b) Replace `.workspace__content:focus { outline: none }` (styles.css:1255), which cancels the indicator on the exact element the skip link targets (Shell.tsx:38/47). (c) Add `role="progressbar"` to `.progress` (a bare div drops its `aria-label`). (d) Gate `"No fleet machines found."` (:1959) on `loading`, which inits `true` at :538, so that sentence is currently the guaranteed first paint. | styles.css, VmsPage.tsx, Shell.tsx | S | Real bugs, minutes each. |

Also worth 10 minutes, unrelated to busyness: at 1800-2032px the shell overflows horizontally. `styles.css:4337` widens the rail to 272px but `.workspace--outcome .workspace__content` (styles.css:255, specificity 0,2,0) beats the override at :4341 (0,1,0), so the width formula still assumes a 120px gutter. At 1920px that is 1760px of content in a 1648px column.

### Phase 2 - The real fix

| # | Change | Files | Effort | Payoff |
|---|---|---|---|---|
| 12 | **Token block plus mechanical sweep.** Add to `:root`: `--w-body: 400; --w-med: 500; --w-strong: 650;` / `--fs-1: 12px` through `--fs-5: 28px` / `--sp-1: 4px` through `--sp-5: 40px` / `--radius: 6px; --line-strong: #3d4c63;`. Sweep the 101 weights at 750+ down, reserving `--w-strong` for `h1`, Panel `h2`, `.machine-name`, `.metric-strip strong`. Reset the global `h3` to no-uppercase and give `.bubble-card h3` / `.fleet-card h3` explicit `text-transform: none`. Keep uppercase on only `.fleet-machine-table thead th` and `.metric-strip span`. Enforce a 2x step on the spacing chains. | styles.css only | L | The single largest mechanical contributor. Touches no JSX. **Blast radius is all 25 pages** and there is no visual-regression coverage, so land it alone. |
| 13 | **Table 13 columns to 8.** Final list below. Delete the 13 `nth-child` widths (styles.css:3276-3288), drop `min-width` 1520px to ~1040px so nothing clips at 1280px. Give `.fleet-machine-table-wrap` an explicit `max-height` so the already-declared `position: sticky` thead (styles.css:3252) finally works, plus `tabindex="0"` / `role="region"` / `aria-label`. | VmsPage.tsx:1905-1923, :2254-2337; styles.css:3226-3288 | M | Kills the permanent horizontal scrollbar and takes ~273 painted objects down to ~160. |
| 14 | **Row actions on hover, and give the row its missing verb.** Fold Tag and Edit into the single trailing actions cell, plus a conditional Approve when `row.agent?.approval_status === "pending"`. Thread `onApproveAgent` (defined :1606, currently passed only to topology and detail) into FleetMachineTable. Reveal with `opacity`, not `display`, on `tr:hover` and `tr:focus-within`, with a `@media (hover: none)` fallback. | VmsPage.tsx:1784-1802, :1829-1865, :2312-2335 | M | 40 permanent buttons become 2 transient ones, and "Pending" stops being a read-only dead end. |
| 15 | **Rank the rows.** Add `needsAttention(row)` to viewModels.ts from signals already computed there (`row.stale` :670/:742, `approval_status` :804, `upgrade_available` :801, status not running). Sort attention-first, VMID within group (currently VMID only, viewModels.ts:748-759). Add a `.segmented` preset row (All / Attention / Stale / Pending / No agent) using the existing CSS at styles.css:4474. Give `Metric` an optional `href` (`.metric-strip > a` is already styled at styles.css:1347) so the Attention tile stops being a dead end. | viewModels.ts, VmsPage.tsx, ui.tsx:7-21 | M | Converts the page from a spreadsheet you scan into a queue that tells you where to look. |
| 16 | **Topology behind `?view=`.** A `.segmented` rail (Machines / Topology / Services) exactly as NetworksPage.tsx:758 does it, URL-addressable as CloudosdPage.tsx:796 does it. Delete `CredentialInventory` (:3156-3163, :3416-3475) and its eager `/api/credentials` fetch (:576) and link to `/react/credentials`, already registered at routes.ts:155. | VmsPage.tsx | M | Removes settings-grade CRUD nested two levels inside a panel about services, and stops fetching credentials on the detail route where nothing consumes them. |
| 17 | **Group the detail toolbar, and pick one power path.** Three groups (Watch / Drive / Evidence) separated by a `border-left`. Move Rename, Delete agent, Delete VM into a closed `<details>` "Manage this VM" (precedent: NetworksPage.tsx:912) so the two irreversible buttons are no longer adjacent to Shutdown. Keep the console power row and route it through the toolbar's `window.confirm` logic (VmsPage.tsx:794-801); `sendPowerAction` (VmActionWorkspace.tsx:322-333) currently fires with no prompt at all. | VmsPage.tsx:2412-2442, VmActionWorkspace.tsx | M | Fixes a genuine safety asymmetry, not just density. |
| 18 | **Modal focus management.** A ~30-line `useModalDialog(ref, onClose)` hook: store `activeElement`, focus first node, Escape to close, trap Tab, restore on unmount. Both dialogs declare `aria-modal="true"` (VmsPage.tsx:1981, :2079) while a grep for `Escape|onKeyDown|autoFocus|useRef` across the 3476-line file returns **zero** matches. | VmsPage.tsx | S | Keyboard and screen-reader users are currently stranded. Small, but it is a real defect. |

### Phase 3 - Optional / later

| # | Change | Effort | Payoff |
|---|---|---|---|
| 19 | **Button consolidation.** 8 unrelated treatments render across these two pages (`.action-link` :1277, `.utility-button` :1754, `.credential-chip__action` :3097, `.machine-action-menu summary` :3348, `.fleet-action` :3799, `.vm-action-tabs button` :3947, `.fleet-bulk-bar__action` :4792, `.fleet-modal__secondary` :4907) with 5 sizes, 3 weights, 2 radii and a 3-vs-5 uppercase split. Two of them render 20px apart in the same panel (:3433 vs :3455). Collapse to `.fleet-action` plus `--primary`, `--danger`, `--quiet`. Raise its border to `--line-strong` (current boundary is ~1.37:1 against its backdrop; WCAG 1.4.11 wants 3:1). | M | Consistency, plus a real non-text-contrast fix. |
| 20 | **Kill the 1px lattice and collapse the 7 dt/dd treatments** into one `.stat-grid` with a `--cols` property (styles.css:1326, 2108, 2253, 3416, 4360). Standardise on `--radius: 6px`, retiring the 39 uses of 8px and 13 of 7px. | M | ~110 lines deleted, less ink per datum. |
| 21 | **Reserve image boxes.** Neither screenshot `<img>` carries width/height or `aspect-ratio` (VmEvidencePanels.tsx:178, VmActionWorkspace.tsx:643), so a socket-driven `screenshot.result` (VmsPage.tsx:638-657) shifts up to 320px of layout under the cursor. | S | Stops unsolicited reflow. |
| 22 | **Split VmsPage.tsx.** 3476 lines into a route shim plus FleetPage / VmDetailPage / FleetMachineTable / BubbleTopology. Mechanical, no behaviour change, own commit. | L | Maintainability only. Will conflict with parked branches in docs/CONSOLIDATION_BACKLOG.md, so time it deliberately. |
| 23 | **Give `missing_vms` a worklist.** Read only at viewModels.ts:383 (folded into attention) and :394 (reported again), so two adjacent tiles counted the same machines and neither was clickable. Render them as table rows with a distinct status. | M | Closes a counted-but-unactionable set. |

## Proposed final shape

### Fleet table: exact final column list (13 to 8)

1. Select checkbox (36px) - unchanged, still needed for bulk agent delete
2. **Device Name** (28%) - link, plus a `.machine-subline` second line reading `#113 / 192.168.2.44 / Windows 11 24H2`, and the matched field value when a filter is active
3. **Runtime** (10%) - neutral pill unless stopped
4. **Agent** (14%) - `fleetAgentTone`: bad for None/Stale, warn for Pending/Upgrade, neutral otherwise
5. **Phase** (18%) - **new column**
6. **Heartbeat** (12%)
7. **Bubble** (14%)
8. Row actions (96px) - Tag, Edit, conditional Approve; revealed on hover/focus

Cut, with reasons: **VMID** (second link to the identical URL, routes.ts:455; survives as subline text and stays filterable), **IP Address** (a copy target, not a scan target; moves to subline), **OS** and **OS Version** (near-constant on a homogeneous Windows fleet; merge into subline), **Managed By** (binary Intune/None, one bit rendered as a 74px pill on every row), **Tag** and **Edit** (controls, not data; move to the actions cell).

Added: **Phase**. It is populated at viewModels.ts:664, already searchable at viewModels.ts:850, and renders **nowhere** today, so filtering by phase silently narrows the list with no visible reason. It is also the only value that changes minute to minute during a deploy, which is exactly what earns a column when OS does not. `.machine-subline` already exists in CSS (styles.css:3308-3320) and is referenced by zero TSX files.

```
BEFORE (/react/vms)                      AFTER
+--------------------------------+       +--------------------------------+
| [red notice] [red notice]      |       | VMs            [Refresh][Sig]  |
| [red notice] [red notice] [red]|       +--------------------------------+
+--------------------------------+       | VMs | Running | Attn | Agents  |
|10 tiles / 7 cols               |       +--------------------------------+
| [][][][][][][]                 |       | Fleet machines                 |
| [][][]  <-- bare border slab   |       | [filter______]      18 of 24   |
+--------------------------------+       | (All)(Attn)(Stale)(Pending)    |
| Filter fleet        [Refresh]  |       | Dev | Run | Agent | Phase |HB|B|
+--------------------------------+       | vm113  running  v2.1  Setup    |
| VM Workstation Fleets          |       |  #113 / .2.44 / Win11 24H2     |
|  [card][card][card]            |       | vm114  running  STALE  -       |
+--------------------------------+       |  #114 / .2.51 / Win11 24H2     |
| Critical Infrastructure        |       |  ... (attention rows first)    |
|  [card][card]                  |       +--------------------------------+
+--------------------------------+       | > Bubbles, infra and services  |
| Connected Services             |       +--------------------------------+
|  [card]  + Credential CRUD     |
+--------------------------------+       8 columns, min-width 1040px,
| ...scroll 2-3 screens...       |       no horizontal scrollbar,
+--------------------------------+       0 pill outlines at rest
| Fleet machines (13 cols,       |
| 1520px, cols 11-13 clipped)    |
+--------------------------------+
```

### VM detail: exact final section order (17 regions to 6)

1. **Breadcrumb + hero** - name; one subtitle line carrying OS, version, VMID, IP, serial, QGA, **phase** and **bubble**; 2-3 neutral badges. Absorbs everything worth keeping from the deleted `Essentials` and most of `PVE`. Bubble comes from `assignmentsByVmid` (computed at :704, currently passed only to the table at :1793, never to detail).
2. **Action bar** - three groups: Watch (Console, Screenshot) | Drive (Start/Shutdown, Reset, Type, CAD, Enter) | Evidence (Hash, Logs, QGA, Enroll). Plus a closed `<details>` "Manage this VM" holding Rename, Delete agent, Delete VM.
3. **Stage** - the action zone, defaulting to the latest screenshot, with the VNC canvas as the first child of the console panel.
4. **Controls strip** - keys and power visible; type-text form and credential list inside a closed `<details>` "Input and credentials".
5. **Facts** - PVE reduced to 3 rows (Status, Target OS, Sequence); Agent panel only when `row.agent`; Intune panel only when `row.autopilotDevice`.
6. **Evidence rail** - `.segmented` over Timeline (first, cap raised from `slice(0, 8)`), Identity linkage, Screenshots; Directory evidence only when a match exists.

```
BEFORE (/react/vms/:vmid)                AFTER
+--------------------------------+       +--------------------------------+
| VMs / vm113                    |       | VMs / vm113                    |
| vm113   [running][Intune][v2.1]|       | vm113          [running][v2.1] |
| Win11 24H2 / VMID 113 / .2.44  |       | Win11 24H2 / #113 / .2.44 /    |
+--------------------------------+       | SN 4C4C / Specialize / labz1   |
|[Con][Shot][Shut][Stop][Reset]  |       +--------------------------------+
|[Hash][Logs][Rename][Type][CAD] |       | WATCH | DRIVE     | EVIDENCE   |
|[Enter][QGA][Upd*][DelAgt][DelVM]|      |[Con][Shot]|[Shut][Reset][Type] |
|  15 flat, DelAgt next to DelVM |       |           |[CAD][Enter]|[Hash] |
+--------------------------------+       | > Manage this VM               |
| +----------------------------+ |       +--------------------------------+
| |  ...dashed empty box...    | |       | +----------------------------+ |
| |  "Console opens here"      | |       | |                            | |
| +----------------------------+ |       | |     THE MACHINE SCREEN     | |
+--------------------------------+       | |                            | |
|Essen.| PVE  |Agent |Intune    |       | +----------------------------+ |
| 9 rws| 6 rws| 9 "-"| 6 "-"    |       | [keys] [power]  > Input+creds |
+--------------------------------+       +--------------------------------+
| Screenshot | Identity | Creds  |       | PVE (3 rows)                   |
| Directory  | Timeline (last,   |       | (Agent/Intune only if present) |
|             capped at 8)      |       +--------------------------------+
+--------------------------------+       | (Timeline)(Identity)(Shots)    |
                                         | Timeline first, uncapped       |
 30 kv rows, 19 of them "-"              +--------------------------------+
                                          ~8 data points on a lab VM
```

## What I would NOT change

- **`Panel` and `.panel`.** `styles.css:1466-1470` is 4 lines: `border-top: 1px solid var(--line)` and padding. No box, no radius, no fill. It is the one genuinely restrained surface in the system, it has an unused `action` slot already built (ui.tsx:39-50), and 127 instances across 25 files. Everything else should converge on it, not the reverse.
- **The color tokens themselves.** The 14 colors at styles.css:12-24 are a good palette and the text contrast is fine, measured: `--text` on `--surface` is about 15:1, `--muted` about 7:1. The problem is exclusively how often the saturated ones are spent, not what they are. Do not re-pick colors.
- **The socket/live-data layer.** `connectFleetLive` and the `onEvent` handlers (VmsPage.tsx:625-690) do the right thing and are not part of the complaint. The only adjustment is cosmetic (reserve image boxes, auto-clear status) so live updates stop reflowing the page.
- **The evidence data model.** `/api/vms/{vmid}/detail` already returns everything the page needs including `latest_screenshot` and `timeline` (VmEvidencePanels.tsx:121-126). The fix is presentation order and gating, not new endpoints.

## Tradeoffs and risks

- **Six fleet-wide counters disappear** with the metric strip cut. `Attention` covers unenrolled plus `missing_vms` only (viewModels.ts:383), so pending approvals and pairing genuinely lose their at-a-glance number until the Phase 2 preset chips land. If your morning routine is "glance at Approvals", Phase 1 alone is a regression for you. Sequence 15 sooner if so.
- **Hover-revealed row actions are worse on touch and worse for discovery.** They must use `opacity`/`visibility` (never `display: none`), must fire on `tr:focus-within` as well as `tr:hover`, and must not be `aria-hidden`, or a density win becomes an accessibility regression. A new operator will not know Tag exists.
- **Managed By and Bubble stop being scannable as columns** in the sense of running your eye down one. If bubble membership is a daily grouping question rather than a per-machine fact, the right answer is a bubble filter, which is not in this plan.
- **Neutral-by-default will read as a regression for about a week.** A green Running pill currently says "I checked, it is fine". After the change a healthy fleet is entirely grey and the absence of color has to be trusted as the signal. That is correct information design and it will still generate a complaint.
- **Recommendation 12 is the single largest risk here.** It rewrites 200+ declarations in one global stylesheet, so every route changes appearance in one commit, not just these two. `tests/e2e` exists but a grep for `toHaveScreenshot`/`toMatchSnapshot` finds nothing, so there is no visual-regression net. Land it alone, on its own commit, and eyeball Provision, TaskEngine, Networks and Cloudosd before pushing.
- **Conditional panels break a positional CSS rule silently.** `.vm-evidence-grid .panel:nth-child(5)` (styles.css:3614-3616) spans the fifth panel full width. Once panels render conditionally the fifth child differs per VM. Replace it with an explicit modifier class **in the same change** as recommendation 7 or the layout breaks in a way no test will catch.
- **Deleting `Essentials` removes a copy-paste block.** A dt/dd grid is easy to drag-select into a ticket; a slash-separated hero line is not. Nobody has asked for this, but somebody may be using it.
- **Deleting the second credential list means credentials are only reachable when the console is open.** If a machine is powered off or VNC fails, there is no read-only path to a saved credential on the detail page until `/react/credentials` is opened separately.
- **Defaulting the stage to a screenshot changes network behaviour.** Every detail page load now renders an image immediately, and `?action=console` opens a VNC socket on navigation rather than on an explicit click. More server load, and a behaviour change for anyone who opens the detail page just to read fields.
- **Attention-first sorting makes row position unstable.** Socket patches replace the agents array (VmsPage.tsx:634-636), so a row can move while the operator is reaching for it. Recompute the sort on load and on explicit user action, not on every socket patch.
- **`min-width: 1040px` still scrolls below about 1160px.** The table remains non-responsive: not one of the 16 media queries in styles.css mentions `.fleet-machine-table`. Sub-laptop widths stay bad, just less bad. The pre-existing 1080px cliff (narrowing the window past 1080px *gains* the table 120px, styles.css:5113-5116) is untouched.
- **`summarizeFleet` keeps computing counts nobody renders.** Leaving viewModels.ts:380-396 alone is deliberate: `ClassicVmsPage.tsx:46` also consumes it, and this plan does not touch a second page.
