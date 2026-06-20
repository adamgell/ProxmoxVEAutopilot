## What I implemented

- Replaced the legacy `OperatorShell` chrome with the outcome-layout shell by wiring `OperatorTopBar`, `OutcomeModeRail`, and `SystemTray` into [`autopilot-proxmox/frontend/src/components/Shell.tsx`](/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend/src/components/Shell.tsx).
- Kept command search routing on the shared `resolveCommandTarget(commandQuery, routeSearchTargets)` path and derived the active rail mode with `modeForPath(path)`.
- Added the outcome-shell regression test in [`autopilot-proxmox/frontend/src/App.test.tsx`](/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend/src/App.test.tsx).
- Added the outcome-shell layout and responsive styling block in [`autopilot-proxmox/frontend/src/styles.css`](/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend/src/styles.css).
- Ran `./skill.sh status` at task start as required by `AGENTS.md`; containers/tunnel were healthy, but the MCP docs/tool probe failed with HTTP 401, so I fell back to local repo files and did not expose any token material.

## What I tested and test results

- Targeted shell TDD command:
  - `cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend`
  - `npm test -- src/App.test.tsx -t "renders outcome shell chrome"`
- Result after implementation: PASS (`1 passed, 40 skipped`)

## TDD Evidence

### RED

- Command:
  - `cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend`
  - `npm test -- src/App.test.tsx -t "renders outcome shell chrome"`
- Relevant failing output before implementation:

```text
FAIL  src/App.test.tsx > App > renders outcome shell chrome and keeps command search routing
TestingLibraryElementError: Unable to find role="navigation" and name "Outcome modes"
```

- Why the failure was expected:
  - The pre-change shell still rendered the legacy `Operator workspace` navigation and old header/rail markup, so the new outcome-shell assertion had to fail first.

### GREEN

- Command:
  - `cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend`
  - `npm test -- src/App.test.tsx -t "renders outcome shell chrome"`
- Relevant passing output after implementation:

```text
Test Files  1 passed (1)
     Tests  1 passed | 40 skipped (41)
```

## Files changed

- [`autopilot-proxmox/frontend/src/components/Shell.tsx`](/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend/src/components/Shell.tsx)
- [`autopilot-proxmox/frontend/src/App.test.tsx`](/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend/src/App.test.tsx)
- [`autopilot-proxmox/frontend/src/styles.css`](/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend/src/styles.css)

## Self-review findings

- Confirmed the shell now consumes the outcome navigation primitives without touching routes, page content, or other non-owned areas.
- Confirmed command search still routes through `resolveCommandTarget` and `routeSearchTargets`.
- Confirmed the test stays scoped to public shell behavior: chrome render plus search submission.
- Added a small JSDOM-only branch in `submitCommandSearch` so the targeted test can observe navigation via `window.history.pushState` without changing real-browser `window.location.assign(...)` behavior.

## Any issues or concerns

- No blocking issues.
- The only notable environment concern was the expected `./skill.sh status` MCP probe failure (`HTTP 401 Unauthorized`), so repo docs MCP tools were unavailable for this task.

## Fix follow-up

### What changed

- Removed the `jsdom`/user-agent branch from `submitCommandSearch` in [`autopilot-proxmox/frontend/src/components/Shell.tsx`](/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend/src/components/Shell.tsx).
- Added an exported `shellNavigator.assign(target)` wrapper whose production behavior is `window.location.assign(target)`.
- Updated the App shell test in [`autopilot-proxmox/frontend/src/App.test.tsx`](/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend/src/App.test.tsx) to spy on `shellNavigator.assign` and assert the resolved route target directly.

### Covering test command

- `cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend && npm test -- src/App.test.tsx -t "renders outcome shell chrome"`

### Relevant passing output

```text
Test Files  1 passed (1)
     Tests  1 passed | 40 skipped (41)
```

### Files changed

- [`autopilot-proxmox/frontend/src/components/Shell.tsx`](/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend/src/components/Shell.tsx)
- [`autopilot-proxmox/frontend/src/App.test.tsx`](/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend/src/App.test.tsx)
