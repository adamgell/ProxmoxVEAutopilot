# Outcome Map Operator Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current equal-weight React operator rail with an outcome-first shell and `/react-shell` control room based on the approved Direction 1 outcome-map mockup.

**Architecture:** Keep FastAPI, existing `/react/*` URLs, and existing page components intact. Add a typed outcome navigation model in `routes.ts`, extract command-search routing into a pure helper, introduce focused React presentation components for the compact mode rail/outcome cards/quick routes, and wire the existing `OperatorShell`/`PageFrame` public API to the new layout. The first production slice implements the Daily Control Room and compact outcome shell; the guided deploy path and evidence verification map stay as design references for follow-up pages.

**Tech Stack:** React 19, TypeScript 6, Vite 8, Vitest, Testing Library, Playwright, lucide-react, existing FastAPI-hosted React shell.

## Global Constraints

- Preserve every existing `/react/*` route and deep link; no URL changes.
- Do not change backend API contracts for this first shell slice.
- Keep `/auth/login`, `/setup`, WinPE, CloudOSD PE, OSDeploy PE, setup artifact, and MCP protocol pages outside this change.
- Use the existing `AppBootstrap` values for build, user, and runtime labels; do not add a new bootstrap API.
- Use `lucide-react` icons for menu/search/log-out controls where an icon is useful.
- Keep the visual theme high-contrast and operator-focused; avoid low-contrast slate-on-slate text.
- Keep TypeScript tests tight and explicit enough to pin intended behavior before and after implementation.
- Implementation is incomplete until frontend checks pass and the tested change is deployed to the live server.

---

## Design References

- Approved expanded mockup: `docs/specs/2026-06-19-outcome-map-expanded-mockups.html`
- Desktop control-room screenshot: `docs/screenshots/2026-06-19-outcome-map-expanded-control-room.png`
- Guided deploy reference: `docs/screenshots/2026-06-19-outcome-map-expanded-guided-deploy.png`
- Evidence-map reference: `docs/screenshots/2026-06-19-outcome-map-expanded-evidence-map.png`
- Mobile reference: `docs/screenshots/2026-06-19-outcome-map-expanded-mobile.png`

Baseline in this worktree before plan writing:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm ci
npm test
npm run typecheck
npm run build
```

Expected baseline:

```text
Test Files  7 passed (7)
Tests  85 passed (85)
tsc --noEmit exits 0
vite build exits 0
```

## File Structure

- Modify `autopilot-proxmox/frontend/src/contracts.ts`: add typed outcome navigation interfaces.
- Modify `autopilot-proxmox/frontend/src/routes.ts`: add outcome modes, outcome cards, quick routes, hidden-detail route search targets, and path-to-mode helpers.
- Modify `autopilot-proxmox/frontend/src/routes.test.ts`: pin the outcome model and ensure detail routes stay searchable without dominating the shell.
- Create `autopilot-proxmox/frontend/src/navigation.ts`: pure command-search target resolver shared by shell and tests.
- Create `autopilot-proxmox/frontend/src/navigation.test.ts`: exact behavior tests for route, VMID, and text searches.
- Create `autopilot-proxmox/frontend/src/components/OutcomeNavigation.tsx`: presentational components for top bar, mode rail, outcome cards, quick route lane, and system tray.
- Create `autopilot-proxmox/frontend/src/components/OutcomeNavigation.test.tsx`: rendering tests for accessible labels and active mode state.
- Modify `autopilot-proxmox/frontend/src/components/Shell.tsx`: preserve `OperatorShell` and `PageFrame` APIs while rendering the outcome shell.
- Modify `autopilot-proxmox/frontend/src/pages/ShellIndexPage.tsx`: replace the operator-map board with the Daily Control Room outcome map.
- Modify `autopilot-proxmox/frontend/src/App.test.tsx`: assert the shell/control room renders outcome-first labels and command search remains functional.
- Modify `autopilot-proxmox/frontend/tests/e2e/react-shell.spec.ts`: add desktop/mobile smoke coverage for the outcome shell.
- Modify `autopilot-proxmox/frontend/src/styles.css`: replace old wide rail styles with compact outcome shell styles and responsive behavior.

---

### Task 1: Outcome Navigation Model

**Files:**
- Modify: `autopilot-proxmox/frontend/src/contracts.ts`
- Modify: `autopilot-proxmox/frontend/src/routes.ts`
- Modify: `autopilot-proxmox/frontend/src/routes.test.ts`

**Interfaces:**
- Produces: `OperatorMode`, `OperatorOutcome`, `OperatorQuickRoute`, `operatorModes`, `operatorOutcomes`, `operatorQuickRoutes`, `routeSearchTargets`, `modeForPath(path: string): OperatorModeId`, `routeMatchesPath(routePath: string, path: string): boolean`.
- Consumes: existing `OperatorRoute`, `OperatorGroupLabel`, `operatorNavGroups`.

- [ ] **Step 1: Write failing route model tests**

Append these tests to `autopilot-proxmox/frontend/src/routes.test.ts`:

```ts
test("defines the compact outcome modes in operator order", () => {
  expect(operatorModes.map((mode) => [mode.id, mode.label, mode.href])).toEqual([
    ["home", "Home", "/react-shell"],
    ["deploy", "Deploy", "/react/cloudosd"],
    ["build", "Build", "/react/task-engine"],
    ["fleet", "Fleet", "/react/vms"],
    ["settings", "Set", "/react/settings"]
  ]);
});

test("defines the daily control room outcomes without equal-weight detail routes", () => {
  expect(operatorOutcomes.map((outcome) => outcome.id)).toEqual([
    "deploy-desktop",
    "deploy-server",
    "prove-ready",
    "build-media",
    "watch-health",
    "fix-configuration"
  ]);
  expect(operatorOutcomes.find((outcome) => outcome.id === "deploy-desktop")).toMatchObject({
    mode: "deploy",
    title: "Deploy a Windows desktop",
    primaryHref: "/react/cloudosd",
    actionLabel: "Start desktop run",
    tone: "good"
  });
  expect(operatorOutcomes.flatMap((outcome) => outcome.relatedRoutes.map((route) => route.href))).toEqual(
    expect.arrayContaining([
      "/react/jobs",
      "/react/vms",
      "/react/hashes",
      "/react/devices",
      "/react/task-engine",
      "/react/monitoring",
      "/react/credentials"
    ])
  );
  expect(operatorOutcomes.flatMap((outcome) => outcome.relatedRoutes.map((route) => route.href))).not.toEqual(
    expect.arrayContaining(["/react/jobs/:jobId", "/react/cloudosd/runs/:runId"])
  );
});

test("keeps detail routes searchable without promoting them to primary outcome cards", () => {
  expect(routeSearchTargets.map((route) => route.path)).toEqual(
    expect.arrayContaining([
      "/react/jobs/:jobId",
      "/react/cloudosd/runs/:runId",
      "/react/osdeploy/runs/:runId",
      "/react/task-engine/sequences/:sequenceId/edit",
      "/react/sequences/:sequenceId/edit"
    ])
  );
  expect(routeSearchTargets.find((route) => route.path === "/react/jobs/:jobId")?.label).toBe("Job Detail");
});

test("maps the active path to the correct outcome mode", () => {
  expect(modeForPath("/react-shell")).toBe("home");
  expect(modeForPath("/react/cloudosd")).toBe("deploy");
  expect(modeForPath("/react/cloudosd/runs/run-1")).toBe("deploy");
  expect(modeForPath("/react/task-engine/sequences/list")).toBe("build");
  expect(modeForPath("/react/vms/109")).toBe("fleet");
  expect(modeForPath("/react/monitoring/settings")).toBe("settings");
  expect(modeForPath("/react/monitoring")).toBe("home");
});
```

Update the import at the top of `routes.test.ts`:

```ts
import {
  migratedRoutes,
  modeForPath,
  operatorFlows,
  operatorModes,
  operatorNavGroups,
  operatorOutcomes,
  operatorQuickRoutes,
  reactRouteForPath,
  routeSearchTargets
} from "./routes";
```

- [ ] **Step 2: Run route tests to verify failure**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test -- src/routes.test.ts
```

Expected: fails because `operatorModes`, `operatorOutcomes`, `routeSearchTargets`, and `modeForPath` are not exported.

- [ ] **Step 3: Add navigation interfaces**

Add this block after `OperatorNavGroup` in `autopilot-proxmox/frontend/src/contracts.ts`:

```ts
export type OperatorModeId = "home" | "deploy" | "build" | "fleet" | "settings";

export type OperatorOutcomeTone = "good" | "blue" | "teal" | "purple" | "warn" | "bad";

export interface OperatorMode {
  readonly id: OperatorModeId;
  readonly label: string;
  readonly longLabel: string;
  readonly href: string;
}

export interface OperatorOutcomeRoute {
  readonly label: string;
  readonly href: string;
  readonly purpose: string;
}

export interface OperatorOutcome {
  readonly id: string;
  readonly mode: OperatorModeId;
  readonly eyebrow: string;
  readonly title: string;
  readonly summary: string;
  readonly primaryHref: string;
  readonly actionLabel: string;
  readonly tone: OperatorOutcomeTone;
  readonly relatedRoutes: readonly OperatorOutcomeRoute[];
}

export interface OperatorQuickRoute {
  readonly label: string;
  readonly href: string;
  readonly summary: string;
  readonly mode: OperatorModeId;
}
```

- [ ] **Step 4: Add outcome model exports**

Update the import in `autopilot-proxmox/frontend/src/routes.ts`:

```ts
import type {
  MigratedRoute,
  OperatorFlow,
  OperatorMode,
  OperatorModeId,
  OperatorNavGroup,
  OperatorOutcome,
  OperatorQuickRoute,
  OperatorRoute
} from "./contracts";
```

Add this block after `operatorNavGroups` in `routes.ts`:

```ts
export const operatorModes: readonly OperatorMode[] = [
  { id: "home", label: "Home", longLabel: "Home", href: "/react-shell" },
  { id: "deploy", label: "Deploy", longLabel: "Deploy", href: "/react/cloudosd" },
  { id: "build", label: "Build", longLabel: "Build", href: "/react/task-engine" },
  { id: "fleet", label: "Fleet", longLabel: "Fleet", href: "/react/vms" },
  { id: "settings", label: "Set", longLabel: "Settings", href: "/react/settings" }
];

export const operatorOutcomes: readonly OperatorOutcome[] = [
  {
    id: "deploy-desktop",
    mode: "deploy",
    eyebrow: "Recommended",
    title: "Deploy a Windows desktop",
    summary: "Open OSDCloud Desktop, use saved defaults, watch first boot, and verify OOBE handoff.",
    primaryHref: "/react/cloudosd",
    actionLabel: "Start desktop run",
    tone: "good",
    relatedRoutes: [
      { label: "Jobs", href: "/react/jobs", purpose: "Live output and pause gates" },
      { label: "VMs", href: "/react/vms", purpose: "Console, agent, and power actions" },
      { label: "Hashes", href: "/react/hashes", purpose: "Hardware identity proof" },
      { label: "Cloud Devices", href: "/react/devices", purpose: "Intune and Autopilot visibility" }
    ]
  },
  {
    id: "deploy-server",
    mode: "deploy",
    eyebrow: "Server path",
    title: "Deploy a Windows server",
    summary: "Open OSDeploy Server, choose role steps, and track the agent heartbeat gate.",
    primaryHref: "/react/osdeploy",
    actionLabel: "Start server run",
    tone: "blue",
    relatedRoutes: [
      { label: "Jobs", href: "/react/jobs", purpose: "Live worker output" },
      { label: "Runs", href: "/react/runs", purpose: "Deployment timeline" },
      { label: "Signals Hub", href: "/react/monitoring", purpose: "Service and deployment health" }
    ]
  },
  {
    id: "prove-ready",
    mode: "fleet",
    eyebrow: "Proof path",
    title: "Prove a machine is ready",
    summary: "Jump through VMs, Hashes, Cloud Devices, and Jobs as one evidence workflow.",
    primaryHref: "/react/vms",
    actionLabel: "Open evidence path",
    tone: "teal",
    relatedRoutes: [
      { label: "VMs", href: "/react/vms", purpose: "Machine state and console" },
      { label: "Hashes", href: "/react/hashes", purpose: "Hardware hash evidence" },
      { label: "Cloud Devices", href: "/react/devices", purpose: "Cloud identity evidence" },
      { label: "Jobs", href: "/react/jobs", purpose: "Run output evidence" }
    ]
  },
  {
    id: "build-media",
    mode: "build",
    eyebrow: "Build path",
    title: "Build or refresh media",
    summary: "Open Task Engine, Template, Task Sequences, and Answer ISOs without hunting.",
    primaryHref: "/react/task-engine",
    actionLabel: "Open build tools",
    tone: "purple",
    relatedRoutes: [
      { label: "Task Engine", href: "/react/task-engine", purpose: "Build orchestration" },
      { label: "Template", href: "/react/template", purpose: "Template image workflow" },
      { label: "Answer ISOs", href: "/react/answer-isos", purpose: "Generated media" }
    ]
  },
  {
    id: "watch-health",
    mode: "home",
    eyebrow: "Observe path",
    title: "Watch service health",
    summary: "Open Signals Hub, live Jobs, monitor settings, and service logs together.",
    primaryHref: "/react/monitoring",
    actionLabel: "Open signals",
    tone: "warn",
    relatedRoutes: [
      { label: "Signals Hub", href: "/react/monitoring", purpose: "Health signals" },
      { label: "Jobs", href: "/react/jobs", purpose: "Live worker output" },
      { label: "Monitoring settings", href: "/react/monitoring/settings", purpose: "Watch configuration" }
    ]
  },
  {
    id: "fix-configuration",
    mode: "settings",
    eyebrow: "Guarded path",
    title: "Fix configuration",
    summary: "Open Credentials, General, and Monitoring settings with clear safer labels.",
    primaryHref: "/react/credentials",
    actionLabel: "Open settings",
    tone: "bad",
    relatedRoutes: [
      { label: "Credentials", href: "/react/credentials", purpose: "Secrets and identity" },
      { label: "General", href: "/react/settings", purpose: "Application settings" },
      { label: "Monitoring settings", href: "/react/monitoring/settings", purpose: "Signals configuration" }
    ]
  }
];

export const operatorQuickRoutes: readonly OperatorQuickRoute[] = [
  { label: "Jobs", href: "/react/jobs", summary: "Live output and pause gates", mode: "home" },
  { label: "VMs", href: "/react/vms", summary: "Console, agent, and power actions", mode: "fleet" },
  { label: "Hashes", href: "/react/hashes", summary: "Capture and upload hardware identity", mode: "fleet" },
  { label: "Runs", href: "/react/runs", summary: "Deployment timeline and history", mode: "home" }
];

export const routeSearchTargets: readonly OperatorRoute[] = operatorNavGroups.flatMap((group) => group.items);
```

Add these helper exports near `reactRouteForPath`:

```ts
export function routeMatchesPath(routePath: string, path: string): boolean {
  if (routePath === path) {
    return true;
  }
  const pattern = `^${routePath.replaceAll("/", "\\/").replace(/:[^/]+/gu, "[^/]+")}$`;
  return new RegExp(pattern, "u").test(path);
}

export function modeForPath(path: string): OperatorModeId {
  if (path === "/react-shell") {
    return "home";
  }
  const route = reactRouteForPath(path);
  if (!route) {
    return "home";
  }
  if (route.group === "Deploy") {
    return "deploy";
  }
  if (route.group === "Build") {
    return "build";
  }
  if (route.group === "Fleet") {
    return "fleet";
  }
  if (route.group === "Settings") {
    return "settings";
  }
  return "home";
}
```

Update `reactRouteForPath` to reuse `routeMatchesPath`:

```ts
export function reactRouteForPath(path: string): OperatorRoute | undefined {
  return operatorNavGroups.flatMap((group) => group.items).find((route) => {
    if (!route.active) {
      return false;
    }
    return routeMatchesPath(route.path, path);
  });
}
```

- [ ] **Step 5: Run route tests to verify pass**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test -- src/routes.test.ts
```

Expected: `1` test file passes.

- [ ] **Step 6: Commit**

```bash
git add src/contracts.ts src/routes.ts src/routes.test.ts
git commit -m "Add outcome navigation model"
```

---

### Task 2: Command Search Resolver

**Files:**
- Create: `autopilot-proxmox/frontend/src/navigation.ts`
- Create: `autopilot-proxmox/frontend/src/navigation.test.ts`

**Interfaces:**
- Consumes: `OperatorRoute` from `contracts.ts`.
- Produces: `resolveCommandTarget(query: string, routes: readonly OperatorRoute[]): string | null`.

- [ ] **Step 1: Write failing command resolver tests**

Create `autopilot-proxmox/frontend/src/navigation.test.ts`:

```ts
import { describe, expect, test } from "vitest";

import type { OperatorRoute } from "./contracts";
import { resolveCommandTarget } from "./navigation";

const routes: readonly OperatorRoute[] = [
  { path: "/react/dashboard", label: "Dashboard", group: "Observe", phase: "read-only", active: true },
  { path: "/react/cloudosd", label: "OSDCloud Desktop", group: "Deploy", phase: "operational", active: true },
  { path: "/react/jobs/:jobId", label: "Job Detail", group: "Observe", phase: "operational", active: true },
  { path: "/react/hashes", label: "Hashes", group: "Fleet", phase: "operational", active: true }
];

describe("resolveCommandTarget", () => {
  test("returns null for blank input", () => {
    expect(resolveCommandTarget("", routes)).toBeNull();
    expect(resolveCommandTarget("   ", routes)).toBeNull();
  });

  test("prefers exact route label matches", () => {
    expect(resolveCommandTarget("hashes", routes)).toBe("/react/hashes");
    expect(resolveCommandTarget("OSDCloud Desktop", routes)).toBe("/react/cloudosd");
  });

  test("uses partial route label matches after exact matches", () => {
    expect(resolveCommandTarget("desktop", routes)).toBe("/react/cloudosd");
  });

  test("routes numeric searches to VM detail", () => {
    expect(resolveCommandTarget("109", routes)).toBe("/react/vms/109");
  });

  test("routes unknown searches to filtered fleet search", () => {
    expect(resolveCommandTarget("NTTENANT01", routes)).toBe("/react/vms?search=NTTENANT01");
    expect(resolveCommandTarget("VM 109 ready", routes)).toBe("/react/vms?search=VM%20109%20ready");
  });
});
```

- [ ] **Step 2: Run command resolver tests to verify failure**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test -- src/navigation.test.ts
```

Expected: fails because `src/navigation.ts` does not exist.

- [ ] **Step 3: Implement resolver**

Create `autopilot-proxmox/frontend/src/navigation.ts`:

```ts
import type { OperatorRoute } from "./contracts";

export function resolveCommandTarget(query: string, routes: readonly OperatorRoute[]): string | null {
  const trimmed = query.trim();
  if (!trimmed) {
    return null;
  }
  const normalized = trimmed.toLowerCase();
  const exactRoute = routes.find((route) => route.label.toLowerCase() === normalized);
  if (exactRoute) {
    return exactRoute.path;
  }
  const partialRoute = routes.find((route) => route.label.toLowerCase().includes(normalized));
  if (partialRoute) {
    return partialRoute.path;
  }
  if (/^\d+$/u.test(trimmed)) {
    return `/react/vms/${trimmed}`;
  }
  return `/react/vms?search=${encodeURIComponent(trimmed)}`;
}
```

- [ ] **Step 4: Run command resolver tests to verify pass**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test -- src/navigation.test.ts
```

Expected: `1` test file passes.

- [ ] **Step 5: Commit**

```bash
git add src/navigation.ts src/navigation.test.ts
git commit -m "Extract operator command search routing"
```

---

### Task 3: Outcome Navigation Components

**Files:**
- Create: `autopilot-proxmox/frontend/src/components/OutcomeNavigation.tsx`
- Create: `autopilot-proxmox/frontend/src/components/OutcomeNavigation.test.tsx`

**Interfaces:**
- Consumes: `OperatorMode`, `OperatorOutcome`, `OperatorQuickRoute`, `OperatorModeId`.
- Produces: `OutcomeModeRail`, `OutcomeCardGrid`, `QuickRouteLane`, `OperatorTopBar`, `SystemTray`.

- [ ] **Step 1: Write failing component tests**

Create `autopilot-proxmox/frontend/src/components/OutcomeNavigation.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import type { AppBootstrap, OperatorMode, OperatorOutcome, OperatorQuickRoute } from "../contracts";
import { OperatorTopBar, OutcomeCardGrid, OutcomeModeRail, QuickRouteLane, SystemTray } from "./OutcomeNavigation";

const modes: readonly OperatorMode[] = [
  { id: "home", label: "Home", longLabel: "Home", href: "/react-shell" },
  { id: "deploy", label: "Deploy", longLabel: "Deploy", href: "/react/cloudosd" },
  { id: "build", label: "Build", longLabel: "Build", href: "/react/task-engine" }
];

const outcomes: readonly OperatorOutcome[] = [
  {
    id: "deploy-desktop",
    mode: "deploy",
    eyebrow: "Recommended",
    title: "Deploy a Windows desktop",
    summary: "Open OSDCloud Desktop.",
    primaryHref: "/react/cloudosd",
    actionLabel: "Start desktop run",
    tone: "good",
    relatedRoutes: [{ label: "Jobs", href: "/react/jobs", purpose: "Live output" }]
  }
];

const quickRoutes: readonly OperatorQuickRoute[] = [
  { label: "Jobs", href: "/react/jobs", summary: "Live output and pause gates", mode: "home" }
];

const bootstrap: AppBootstrap = {
  buildSha: "abc1234",
  buildTime: "2026-06-19T19:00:00Z",
  userName: "Adam"
};

describe("OutcomeNavigation components", () => {
  test("renders mode rail with active mode", () => {
    render(<OutcomeModeRail modes={modes} activeMode="deploy" />);

    expect(screen.getByRole("navigation", { name: "Outcome modes" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Deploy" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Home" })).not.toHaveAttribute("aria-current");
  });

  test("renders outcome cards with primary actions and related routes", () => {
    render(<OutcomeCardGrid outcomes={outcomes} />);

    expect(screen.getByRole("heading", { name: "Deploy a Windows desktop" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start desktop run" })).toHaveAttribute("href", "/react/cloudosd");
    expect(screen.getByRole("link", { name: "Jobs" })).toHaveAttribute("href", "/react/jobs");
  });

  test("renders quick routes", () => {
    render(<QuickRouteLane quickRoutes={quickRoutes} />);

    expect(screen.getByRole("navigation", { name: "Quick routes" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Jobs Live output and pause gates" })).toHaveAttribute("href", "/react/jobs");
  });

  test("renders top bar command and operator identity", () => {
    render(<OperatorTopBar bootstrap={bootstrap} query="" onQueryChange={() => {}} onSubmit={() => {}} />);

    expect(screen.getByRole("link", { name: "Proxmox VE Autopilot home" })).toHaveAttribute("href", "/react-shell");
    expect(screen.getByRole("searchbox", { name: "Search console" })).toBeInTheDocument();
    expect(screen.getByText("Adam")).toBeInTheDocument();
  });

  test("renders system tray build and socket state", () => {
    render(<SystemTray bootstrap={bootstrap} socketState="open" />);

    expect(screen.getByText("Live open")).toBeInTheDocument();
    expect(screen.getByText("Build abc1234")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run component tests to verify failure**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test -- src/components/OutcomeNavigation.test.tsx
```

Expected: fails because `OutcomeNavigation.tsx` does not exist.

- [ ] **Step 3: Implement outcome navigation components**

Create `autopilot-proxmox/frontend/src/components/OutcomeNavigation.tsx`:

```tsx
import { LogOut, Search } from "lucide-react";
import type { FormEvent } from "react";

import type {
  AppBootstrap,
  OperatorMode,
  OperatorModeId,
  OperatorOutcome,
  OperatorQuickRoute
} from "../contracts";
import { formatShortDateTime } from "../viewModels";

export function OutcomeModeRail({
  modes,
  activeMode
}: {
  readonly modes: readonly OperatorMode[];
  readonly activeMode: OperatorModeId;
}) {
  return (
    <nav className="outcome-rail" aria-label="Outcome modes">
      {modes.map((mode) => (
        <a
          key={mode.id}
          className={mode.id === activeMode ? "is-active" : ""}
          href={mode.href}
          aria-current={mode.id === activeMode ? "page" : undefined}
        >
          {mode.label}
        </a>
      ))}
    </nav>
  );
}

export function OperatorTopBar({
  bootstrap,
  query,
  onQueryChange,
  onSubmit
}: {
  readonly bootstrap: AppBootstrap;
  readonly query: string;
  readonly onQueryChange: (value: string) => void;
  readonly onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const userLabel = bootstrap.userName || bootstrap.userEmail || "Signed in";
  return (
    <header className="outcome-topbar" aria-label="Global console status">
      <a className="outcome-brand" href="/react-shell" aria-label="Proxmox VE Autopilot home">
        <span className="outcome-brand__mark" aria-hidden="true" />
        <span>
          <strong>Proxmox VE Autopilot</strong>
          <small>Control room</small>
        </span>
      </a>
      <form className="outcome-command" role="search" onSubmit={onSubmit}>
        <Search aria-hidden="true" focusable="false" size={16} strokeWidth={2.4} />
        <input
          type="search"
          value={query}
          onChange={(event) => {
            onQueryChange(event.currentTarget.value);
          }}
          placeholder="Type a VM, job, serial, route, or run ID"
          aria-label="Search console"
        />
      </form>
      <div className="outcome-operator">
        <span className="outcome-user" title={bootstrap.userEmail || userLabel}>{userLabel}</span>
        <a className="outcome-logout" href="/auth/logout" aria-label={`Log out ${userLabel}`}>
          <LogOut aria-hidden="true" focusable="false" size={16} strokeWidth={2.4} />
          <span>Log out</span>
        </a>
      </div>
    </header>
  );
}

export function OutcomeCardGrid({ outcomes }: { readonly outcomes: readonly OperatorOutcome[] }) {
  return (
    <section className="outcome-card-grid" aria-label="Operator outcomes">
      {outcomes.map((outcome) => (
        <article key={outcome.id} className={`outcome-card outcome-card--${outcome.tone}`}>
          <span className={`outcome-pill outcome-pill--${outcome.tone}`}>{outcome.eyebrow}</span>
          <h2>{outcome.title}</h2>
          <p>{outcome.summary}</p>
          <a className="outcome-card__primary" href={outcome.primaryHref}>{outcome.actionLabel}</a>
          <div className="outcome-card__routes" aria-label={`${outcome.title} related routes`}>
            {outcome.relatedRoutes.map((route) => (
              <a key={`${outcome.id}-${route.href}-${route.label}`} href={route.href}>
                <strong>{route.label}</strong>
                <span>{route.purpose}</span>
              </a>
            ))}
          </div>
        </article>
      ))}
    </section>
  );
}

export function QuickRouteLane({ quickRoutes }: { readonly quickRoutes: readonly OperatorQuickRoute[] }) {
  return (
    <nav className="quick-route-lane" aria-label="Quick routes">
      {quickRoutes.map((route) => (
        <a key={`${route.href}-${route.label}`} href={route.href}>
          <strong>{route.label}</strong>
          <span>{route.summary}</span>
        </a>
      ))}
    </nav>
  );
}

export function SystemTray({
  bootstrap,
  socketState
}: {
  readonly bootstrap: AppBootstrap;
  readonly socketState?: string | undefined;
}) {
  const buildLabel = bootstrap.buildSha ? `Build ${bootstrap.buildSha}` : "Build unknown";
  return (
    <aside className="outcome-system-tray" aria-label="Runtime status">
      {socketState ? <span className={`socket-state socket-state--${socketState}`}>Live {socketState}</span> : null}
      <span>{buildLabel}</span>
      {bootstrap.buildTime ? (
        <time dateTime={bootstrap.buildTime}>{formatShortDateTime(bootstrap.buildTime)}</time>
      ) : null}
    </aside>
  );
}
```

- [ ] **Step 4: Run component tests to verify pass**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test -- src/components/OutcomeNavigation.test.tsx
```

Expected: `1` test file passes.

- [ ] **Step 5: Commit**

```bash
git add src/components/OutcomeNavigation.tsx src/components/OutcomeNavigation.test.tsx
git commit -m "Add outcome navigation components"
```

---

### Task 4: Wire OperatorShell To Outcome Layout

**Files:**
- Modify: `autopilot-proxmox/frontend/src/components/Shell.tsx`
- Modify: `autopilot-proxmox/frontend/src/App.test.tsx`
- Modify: `autopilot-proxmox/frontend/src/styles.css`

**Interfaces:**
- Consumes: `resolveCommandTarget`, `operatorModes`, `modeForPath`, `routeSearchTargets`, `OutcomeModeRail`, `OperatorTopBar`, `SystemTray`.
- Produces: existing `OperatorShell` and `PageFrame` behavior with outcome layout.

- [ ] **Step 1: Add failing shell render/search assertions**

In `autopilot-proxmox/frontend/src/App.test.tsx`, add this test near existing shell tests:

```tsx
test("renders outcome shell chrome and keeps command search routing", async () => {
  window.history.pushState({}, "", "/react/jobs");
  render(<App bootstrap={{ userName: "Adam", buildSha: "abc1234" }} />);

  expect(await screen.findByRole("navigation", { name: "Outcome modes" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute("href", "/react-shell");
  expect(screen.getByRole("link", { name: "Deploy" })).toHaveAttribute("href", "/react/cloudosd");
  expect(screen.getByRole("link", { name: "Fleet" })).toHaveAttribute("href", "/react/vms");

  const search = screen.getByRole("searchbox", { name: "Search console" });
  fireEvent.change(search, { target: { value: "Hashes" } });
  fireEvent.submit(search.closest("form") as HTMLFormElement);

  expect(window.location.pathname).toBe("/react/hashes");
});
```

- [ ] **Step 2: Run shell test to verify failure**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test -- src/App.test.tsx -t "renders outcome shell chrome"
```

Expected: fails because the current shell still renders `Operator workspace`, not `Outcome modes`.

- [ ] **Step 3: Replace Shell imports and command handling**

In `autopilot-proxmox/frontend/src/components/Shell.tsx`, replace the imports with:

```tsx
import { useState, type FormEvent, type ReactNode } from "react";

import type { AppBootstrap } from "../contracts";
import { resolveCommandTarget } from "../navigation";
import { modeForPath, operatorModes, routeSearchTargets } from "../routes";
import { OperatorTopBar, OutcomeModeRail, SystemTray } from "./OutcomeNavigation";
```

Inside `OperatorShell`, remove `buildLabel`, `commandId`, `routes`, and `userLabel`. Replace `submitCommandSearch` with:

```tsx
  const activeMode = modeForPath(path);

  function submitCommandSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const target = resolveCommandTarget(commandQuery, routeSearchTargets);
    if (target) {
      window.location.assign(target);
    }
  }
```

Replace the `return` block in `OperatorShell` with:

```tsx
  return (
    <div className="workspace workspace--outcome">
      <a className="skip-link" href="#react-content">Skip to content</a>
      <OperatorTopBar
        bootstrap={bootstrap}
        query={commandQuery}
        onQueryChange={setCommandQuery}
        onSubmit={submitCommandSearch}
      />
      <OutcomeModeRail modes={operatorModes} activeMode={activeMode} />
      <div className="workspace__main">
        <main id="react-content" className="workspace__content" tabIndex={-1}>{children}</main>
      </div>
      <SystemTray bootstrap={bootstrap} socketState={socketState} />
    </div>
  );
```

- [ ] **Step 4: Add outcome shell CSS**

In `autopilot-proxmox/frontend/src/styles.css`, add this block after the existing `.workspace` block:

```css
.workspace--outcome {
  grid-template-columns: 96px minmax(0, 1fr);
  background:
    linear-gradient(135deg, #152034 0, #0c1422 45%, #12252a 100%);
  color: #edf5ff;
}

.outcome-topbar {
  position: sticky;
  top: 0;
  z-index: 5;
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: 248px minmax(280px, 1fr) max-content;
  gap: 14px;
  align-items: center;
  min-height: 66px;
  padding: 13px 18px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.14);
  background: color-mix(in srgb, #0f1828 94%, transparent);
  backdrop-filter: blur(12px);
}

.outcome-brand,
.outcome-operator,
.outcome-logout,
.outcome-user {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}

.outcome-brand {
  gap: 10px;
  color: #edf5ff;
  font-weight: 900;
  text-decoration: none;
}

.outcome-brand__mark {
  width: 36px;
  height: 36px;
  flex: 0 0 auto;
  border-radius: 8px;
  background:
    linear-gradient(135deg, #f25022 0 33%, #7fba00 33% 66%, #00a4ef 66%),
    #172033;
  box-shadow: inset 0 0 0 8px #172033, inset 0 0 0 9px rgba(255, 255, 255, 0.14);
}

.outcome-brand strong,
.outcome-brand small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.outcome-brand small {
  color: #aebbd0;
  font-size: 11px;
  font-weight: 900;
  text-transform: uppercase;
}

.outcome-command {
  display: flex;
  gap: 9px;
  align-items: center;
  min-width: 0;
  min-height: 40px;
  padding: 0 12px;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 8px;
  color: #607086;
  background: rgba(255, 255, 255, 0.96);
}

.outcome-command input {
  width: 100%;
  min-width: 0;
  border: 0;
  outline: 0;
  color: #152033;
  background: transparent;
  font: inherit;
  font-weight: 800;
}

.outcome-operator {
  justify-content: flex-end;
  gap: 8px;
}

.outcome-user,
.outcome-logout {
  min-height: 34px;
  padding: 0 10px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 7px;
  color: #edf5ff;
  background: rgba(255, 255, 255, 0.06);
  font-size: 13px;
  font-weight: 900;
  text-decoration: none;
}

.outcome-logout {
  gap: 7px;
}

.outcome-rail {
  grid-column: 1;
  grid-row: 2;
  display: grid;
  align-content: start;
  gap: 9px;
  min-height: calc(100vh - 66px);
  padding: 16px 12px;
  border-right: 1px solid rgba(255, 255, 255, 0.13);
}

.outcome-rail a {
  display: grid;
  place-items: center;
  min-height: 64px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 8px;
  color: #dce8f6;
  background: rgba(255, 255, 255, 0.04);
  font-weight: 900;
  text-decoration: none;
}

.outcome-rail a:hover,
.outcome-rail a.is-active {
  border-color: var(--accent);
  color: #07111f;
  background: var(--accent);
}

.workspace--outcome .workspace__main {
  grid-column: 2;
  grid-row: 2;
}

.workspace--outcome .workspace__content {
  width: min(1760px, calc(100vw - 120px));
  color: #edf5ff;
}

.outcome-system-tray {
  position: fixed;
  right: 14px;
  bottom: 12px;
  z-index: 6;
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 7px;
  align-items: center;
  max-width: min(520px, calc(100vw - 28px));
  padding: 6px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 8px;
  color: #edf5ff;
  background: rgba(15, 24, 40, 0.94);
  box-shadow: 0 10px 30px color-mix(in srgb, #000 24%, transparent);
  backdrop-filter: blur(10px);
}

.outcome-system-tray > span,
.outcome-system-tray > time {
  color: #b8c7d8;
  font-size: 11px;
  font-weight: 900;
  text-align: right;
  white-space: nowrap;
}

@media (max-width: 860px) {
  .workspace--outcome {
    grid-template-columns: 1fr;
  }

  .outcome-topbar {
    grid-template-columns: 1fr;
  }

  .outcome-operator {
    justify-content: flex-start;
  }

  .outcome-rail {
    position: sticky;
    top: 66px;
    z-index: 4;
    grid-column: 1;
    grid-row: auto;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    min-height: 0;
    padding: 10px;
    border-right: 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.13);
    background: #0f1828;
  }

  .outcome-rail a {
    min-height: 44px;
  }

  .workspace--outcome .workspace__main {
    grid-column: 1;
  }

  .workspace--outcome .workspace__content {
    width: 100%;
    padding: 18px 14px 42px;
  }
}
```

- [ ] **Step 5: Run shell test to verify pass**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test -- src/App.test.tsx -t "renders outcome shell chrome"
```

Expected: targeted test passes.

- [ ] **Step 6: Commit**

```bash
git add src/components/Shell.tsx src/App.test.tsx src/styles.css
git commit -m "Wire React shell to outcome navigation"
```

---

### Task 5: Daily Control Room Page

**Files:**
- Modify: `autopilot-proxmox/frontend/src/pages/ShellIndexPage.tsx`
- Modify: `autopilot-proxmox/frontend/src/App.test.tsx`
- Modify: `autopilot-proxmox/frontend/src/styles.css`

**Interfaces:**
- Consumes: `operatorOutcomes`, `operatorQuickRoutes`, `OutcomeCardGrid`, `QuickRouteLane`, existing `OperatorShell`.
- Produces: `/react-shell` Daily Control Room page.

- [ ] **Step 1: Write failing control-room assertions**

Add this test to `autopilot-proxmox/frontend/src/App.test.tsx`:

```tsx
test("renders the outcome-map control room at react shell", () => {
  window.history.pushState({}, "", "/react-shell");
  render(<App bootstrap={{ userName: "Adam", buildSha: "abc1234" }} />);

  expect(screen.getByRole("heading", { name: "What are you trying to finish?" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Deploy a Windows desktop" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Start desktop run" })).toHaveAttribute("href", "/react/cloudosd");
  expect(screen.getByRole("heading", { name: "Prove a machine is ready" })).toBeInTheDocument();
  expect(screen.getByRole("navigation", { name: "Quick routes" })).toBeInTheDocument();
});
```

- [ ] **Step 2: Run control-room test to verify failure**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test -- src/App.test.tsx -t "renders the outcome-map control room"
```

Expected: fails because the current `/react-shell` page still renders the old operator map board.

- [ ] **Step 3: Replace ShellIndexPage**

Replace `autopilot-proxmox/frontend/src/pages/ShellIndexPage.tsx` with:

```tsx
import type { AppBootstrap } from "../contracts";
import { OperatorShell } from "../components/Shell";
import { OutcomeCardGrid, QuickRouteLane } from "../components/OutcomeNavigation";
import { operatorOutcomes, operatorQuickRoutes } from "../routes";

export function ShellIndexPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  return (
    <OperatorShell bootstrap={bootstrap} path="/react-shell">
      <section className="control-room-hero" aria-labelledby="control-room-title">
        <div>
          <h1 id="control-room-title">What are you trying to finish?</h1>
          <p>
            Pick the operator outcome first. The menu routes to the right surface after that:
            run setup, build tools, fleet proof, live jobs, or settings.
          </p>
        </div>
        <aside className="suggested-next" aria-label="Suggested next step">
          <h2>Suggested next step</h2>
          <a href="/react/cloudosd"><span>Open OSDCloud Desktop run</span><strong>Ready</strong></a>
          <a href="/react/vms"><span>Check VM evidence</span><strong>Watch</strong></a>
          <a href="/react/hashes"><span>Review hash upload status</span><strong>Queued</strong></a>
        </aside>
      </section>
      <OutcomeCardGrid outcomes={operatorOutcomes} />
      <QuickRouteLane quickRoutes={operatorQuickRoutes} />
    </OperatorShell>
  );
}
```

- [ ] **Step 4: Add control-room CSS**

Add this block after the outcome shell CSS in `autopilot-proxmox/frontend/src/styles.css`:

```css
.control-room-hero {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 390px;
  gap: 18px;
  align-items: start;
  margin-bottom: 18px;
}

.control-room-hero h1 {
  max-width: 780px;
  color: #ffffff;
  font-size: 32px;
  line-height: 1.08;
}

.control-room-hero p {
  max-width: 860px;
  margin-top: 8px;
  color: #c5d2e2;
  font-size: 15px;
}

.suggested-next {
  display: grid;
  gap: 10px;
  padding: 14px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.2);
}

.suggested-next h2 {
  margin: 0;
  color: #b8c7d8;
  font-size: 12px;
  font-weight: 900;
  text-transform: uppercase;
}

.suggested-next a {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: center;
  min-height: 38px;
  padding: 8px 10px;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 7px;
  color: #ffffff;
  background: rgba(255, 255, 255, 0.06);
  font-weight: 900;
  text-decoration: none;
}

.suggested-next strong {
  color: #d9f2ff;
}

.outcome-card-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 13px;
}

.outcome-card {
  display: grid;
  grid-template-rows: auto auto auto auto 1fr;
  gap: 10px;
  min-height: 238px;
  padding: 14px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 8px;
  color: #edf5ff;
  background: rgba(255, 255, 255, 0.06);
}

.outcome-card h2 {
  margin: 0;
  color: #ffffff;
  font-size: 18px;
}

.outcome-card p {
  margin: 0;
  color: #c5d2e2;
}

.outcome-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 28px;
  padding: 0 10px;
  border-radius: 999px;
  color: #07111f;
  font-size: 12px;
  font-weight: 900;
}

.outcome-pill--good { background: #e7f4dc; color: #477400; }
.outcome-pill--blue { background: #dceefb; color: #0069a8; }
.outcome-pill--teal { background: #d9f2ee; color: #00756f; }
.outcome-pill--purple { background: #ece8fb; color: #5f4ba3; }
.outcome-pill--warn { background: #fff1c5; color: #855f00; }
.outcome-pill--bad { background: #fde4dc; color: #b33a18; }

.outcome-card__primary {
  justify-self: start;
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  padding: 0 12px;
  border: 1px solid #005d91;
  border-radius: 7px;
  color: #ffffff;
  background: #0069a8;
  font-weight: 900;
  text-decoration: none;
}

.outcome-card__routes {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  align-self: end;
}

.outcome-card__routes a {
  min-height: 54px;
  padding: 9px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 7px;
  color: #edf5ff;
  background: rgba(255, 255, 255, 0.05);
  text-decoration: none;
}

.outcome-card__routes strong,
.outcome-card__routes span {
  display: block;
}

.outcome-card__routes span {
  margin-top: 3px;
  color: #b8c7d8;
  font-size: 12px;
}

.quick-route-lane {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-top: 18px;
}

.quick-route-lane a {
  min-height: 72px;
  padding: 12px;
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 8px;
  color: #edf5ff;
  background: rgba(255, 255, 255, 0.05);
  text-decoration: none;
}

.quick-route-lane strong,
.quick-route-lane span {
  display: block;
}

.quick-route-lane span {
  margin-top: 4px;
  color: #b8c7d8;
  font-size: 12px;
}

@media (max-width: 1180px) {
  .control-room-hero,
  .outcome-card-grid,
  .quick-route-lane {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 700px) {
  .control-room-hero h1 {
    font-size: 28px;
  }

  .outcome-card__routes {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 5: Run control-room test to verify pass**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test -- src/App.test.tsx -t "renders the outcome-map control room"
```

Expected: targeted test passes.

- [ ] **Step 6: Commit**

```bash
git add src/pages/ShellIndexPage.tsx src/App.test.tsx src/styles.css
git commit -m "Build outcome-map control room"
```

---

### Task 6: Responsive Visual Smoke

**Files:**
- Modify: `autopilot-proxmox/frontend/tests/e2e/react-shell.spec.ts`
- Modify: `autopilot-proxmox/frontend/src/styles.css`

**Interfaces:**
- Consumes: rendered `/react-shell`, `Outcome modes`, outcome cards, quick routes.
- Produces: Playwright smoke coverage for desktop and narrow layout.

- [ ] **Step 1: Add failing Playwright smoke tests**

Append this test to `autopilot-proxmox/frontend/tests/e2e/react-shell.spec.ts`:

```ts
test("renders outcome shell on desktop and mobile widths", async ({ page }) => {
  await mockReadApis(page);
  await page.setViewportSize({ width: 1440, height: 980 });
  await page.goto("/react-shell");

  await expect(page.getByRole("navigation", { name: "Outcome modes" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "What are you trying to finish?" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Deploy a Windows desktop" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Quick routes" })).toBeVisible();

  await page.setViewportSize({ width: 390, height: 980 });
  await expect(page.getByRole("link", { name: "Deploy" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Set" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Deploy a Windows desktop" })).toBeVisible();

  const horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(horizontalOverflow).toBe(false);
});
```

- [ ] **Step 2: Run e2e test to verify failure or pass**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm run test:e2e -- tests/e2e/react-shell.spec.ts -g "renders outcome shell"
```

Expected before Tasks 4 and 5: fails because the outcome shell is not rendered. Expected after Tasks 4 and 5: passes or reveals a CSS overflow that must be fixed in `styles.css`.

- [ ] **Step 3: Fix any responsive overflow exposed by Playwright**

If the `horizontalOverflow` assertion fails, add this CSS near the responsive shell styles:

```css
.workspace--outcome *,
.workspace--outcome *::before,
.workspace--outcome *::after {
  min-width: 0;
}

.outcome-topbar,
.control-room-hero,
.outcome-card-grid,
.quick-route-lane,
.outcome-card__routes {
  max-width: 100%;
}
```

- [ ] **Step 4: Run e2e smoke again**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm run test:e2e -- tests/e2e/react-shell.spec.ts -g "renders outcome shell"
```

Expected: Playwright reports the targeted test passed.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/react-shell.spec.ts src/styles.css
git commit -m "Add outcome shell visual smoke coverage"
```

---

### Task 7: Full Frontend Verification

**Files:**
- No source ownership unless a previous task left a failing verification command.

**Interfaces:**
- Consumes: completed Tasks 1-6.
- Produces: verified branch ready for live deploy.

- [ ] **Step 1: Run full frontend test suite**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm test
```

Expected:

```text
Test Files  9 passed
```

The exact test-file count may be higher if additional focused tests were added during execution. Any failure must be fixed before continuing.

- [ ] **Step 2: Run TypeScript typecheck**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm run typecheck
```

Expected: `tsc --noEmit` exits `0`.

- [ ] **Step 3: Run production build**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm run build
```

Expected: Vite exits `0` and writes `dist/.vite/manifest.json`.

- [ ] **Step 4: Run Playwright shell smoke**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/autopilot-proxmox/frontend
npm run test:e2e -- tests/e2e/react-shell.spec.ts
```

Expected: all tests in `react-shell.spec.ts` pass.

- [ ] **Step 5: Review git status**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell
git status --short
```

Expected: only planned source/test/docs files are modified or added. `autopilot-proxmox/frontend/node_modules/` and generated `dist/` files must not appear in status.

---

### Task 8: Live Server Deploy And Smoke

**Files:**
- No source ownership unless live deployment reveals an implementation defect.

**Interfaces:**
- Consumes: verified branch from Task 7.
- Produces: live server updated and checked.

- [ ] **Step 1: Confirm MCP helper health before deploy**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell
./skill.sh status
```

Expected: `autopilot` and `autopilot-mcp` containers report healthy, and docs tools include `autopilot_docs.list`, `autopilot_docs.search`, and `autopilot_docs.read`.

- [ ] **Step 2: Sync branch to PVE staging path**

Run from the worktree root:

```bash
rsync -a --delete \
  --exclude 'autopilot-proxmox/.env' \
  --exclude 'autopilot-proxmox/inventory/group_vars/all/vault.yml' \
  --exclude 'autopilot-proxmox/secrets/' \
  --exclude 'autopilot-proxmox/output/' \
  --exclude 'autopilot-proxmox/frontend/node_modules/' \
  '/Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell/' \
  pve-dev-192-168-2-252:/root/ProxmoxVEAutopilot/
```

Expected: `rsync` exits `0`. No secret values are printed.

- [ ] **Step 3: Rebuild/restart controller from synced source**

Run:

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase foundation --resume --controller-ip 192.168.2.115 --non-interactive'
```

Expected: the command exits `0` and restarts the controller stack.

- [ ] **Step 4: Check controller health**

Run:

```bash
curl -fsS http://192.168.2.115:5000/healthz
curl -fsS http://192.168.2.115:5000/api/version
ssh pve-dev-192-168-2-252 'ssh -i /root/.local/share/proxmoxveautopilot/controller-bootstrap-ed25519 -o BatchMode=yes -o StrictHostKeyChecking=accept-new autopilot@192.168.2.115 "cd /opt/ProxmoxVEAutopilot/autopilot-proxmox && sudo docker compose ps"'
```

Expected: health endpoint returns success, version endpoint returns the deployed build metadata, and compose shows the controller services up.

- [ ] **Step 5: Browser smoke the live shell**

Open the live UI at `http://192.168.2.115:5000/react-shell` in Playwright or the in-app browser and verify:

```text
Outcome modes nav is visible.
"What are you trying to finish?" is visible.
"Deploy a Windows desktop" card links to /react/cloudosd.
Command search for "Hashes" navigates to /react/hashes.
Viewport width 390 has no horizontal scroll.
```

- [ ] **Step 6: Commit final verified state**

Run:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/outcome-map-operator-shell
git status --short
git add autopilot-proxmox/frontend/src autopilot-proxmox/frontend/tests docs/specs/2026-06-19-outcome-map-expanded-mockups.html docs/screenshots/2026-06-19-outcome-map-expanded-*.png
git add -f docs/superpowers/plans/2026-06-19-outcome-map-operator-shell.md
git commit -m "Build outcome-map operator shell"
```

Expected: commit succeeds on branch `codex/outcome-map-operator-shell`.

---

## Self-Review

- Spec coverage: Direction 1 Daily Control Room is implemented by Tasks 1, 3, 4, and 5. Existing URL preservation is covered by Tasks 1 and 4. Command search behavior is covered by Task 2. Visual and responsive behavior is covered by Task 6. Live deployment is covered by Task 8.
- Scope check: Guided Deploy and Evidence Map are intentionally not implemented in this first slice because they belong inside deploy/run/detail surfaces after the global shell model lands.
- Placeholder scan: the plan defines concrete files, interfaces, commands, and test snippets for each task.
- Type consistency: `OperatorModeId`, `OperatorOutcome`, `OperatorQuickRoute`, `modeForPath`, `routeSearchTargets`, and `resolveCommandTarget` are introduced before later tasks consume them.
