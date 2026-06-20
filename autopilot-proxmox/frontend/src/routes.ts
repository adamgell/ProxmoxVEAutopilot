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

export const operatorNavGroups: readonly OperatorNavGroup[] = [
  {
    label: "Observe",
    items: [
      { path: "/react-shell", label: "Workspace", group: "Observe", phase: "foundation", active: true },
      { path: "/react/dashboard", label: "Dashboard", group: "Observe", phase: "read-only", active: true },
      { path: "/react/jobs", label: "Jobs", group: "Observe", phase: "read-only", active: true },
      {
        path: "/react/jobs/:jobId",
        label: "Job Detail",
        group: "Observe",
        phase: "operational",
        active: true,
        navParentPath: "/react/jobs",
        showInNav: false
      },
      { path: "/react/monitoring", label: "Signals Hub", group: "Observe", phase: "read-only", active: true },
      { path: "/react/runs", label: "Runs", group: "Observe", phase: "read-only", active: true },
      {
        path: "/react/runs/:runId",
        label: "Run Detail",
        group: "Observe",
        phase: "read-only",
        active: true,
        navParentPath: "/react/runs",
        showInNav: false
      },
      { path: "/react/install-tracking", label: "Install Tracking", group: "Observe", phase: "read-only", active: true }
    ]
  },
  {
    label: "Deploy",
    items: [
      { path: "/react/cloudosd", label: "OSDCloud Desktop", group: "Deploy", phase: "operational", active: true },
      {
        path: "/react/cloudosd/runs/:runId",
        label: "OSDCloud Run",
        group: "Deploy",
        phase: "operational",
        active: true,
        navParentPath: "/react/cloudosd",
        showInNav: false
      },
      { path: "/react/osdeploy", label: "OSDeploy Server", group: "Deploy", phase: "operational", active: true },
      {
        path: "/react/osdeploy/runs/:runId",
        label: "OSDeploy Run",
        group: "Deploy",
        phase: "operational",
        active: true,
        navParentPath: "/react/osdeploy",
        showInNav: false
      },
      { path: "/react/provision", label: "Provision", group: "Deploy", phase: "operational", active: true }
    ]
  },
  {
    label: "Build",
    items: [
      { path: "/react/template", label: "Template", group: "Build", phase: "operational", active: true },
      { path: "/react/task-engine", label: "Task Sequences", group: "Build", phase: "operational", active: true },
      {
        path: "/react/task-engine/sequences/list",
        label: "Sequence Library",
        group: "Build",
        phase: "operational",
        active: true,
        navParentPath: "/react/task-engine",
        showInNav: false
      },
      {
        path: "/react/task-engine/sequences/new",
        label: "New Sequence",
        group: "Build",
        phase: "operational",
        active: true,
        navParentPath: "/react/task-engine",
        showInNav: false
      },
      {
        path: "/react/task-engine/sequences/templates/:templateId",
        label: "Task Template",
        group: "Build",
        phase: "read-only",
        active: true,
        navParentPath: "/react/task-engine/sequences/list",
        showInNav: false
      },
      {
        path: "/react/task-engine/sequences/:sequenceId/edit",
        label: "Edit Task Sequence",
        group: "Build",
        phase: "operational",
        active: true,
        navParentPath: "/react/task-engine/sequences/list",
        showInNav: false
      },
      { path: "/react/answer-isos", label: "Answer ISOs", group: "Build", phase: "operational", active: true }
    ]
  },
  {
    label: "Infrastructure",
    items: [
      { path: "/react/networks", label: "Networks", group: "Infrastructure", phase: "operational", active: true }
    ]
  },
  {
    label: "Fleet",
    items: [
      { path: "/react/vms", label: "VMs", group: "Fleet", phase: "operational", active: true },
      {
        path: "/react/vms/:vmid",
        label: "VM Detail",
        group: "Fleet",
        phase: "operational",
        active: true,
        navParentPath: "/react/vms",
        showInNav: false
      },
      { path: "/react/agent-download", label: "Agent Download", group: "Fleet", phase: "operational", active: true },
      { path: "/react/legacy-vms", label: "Classic VM Table", group: "Fleet", phase: "read-only", active: true },
      { path: "/react/utm-vms", label: "UTM VMs", group: "Fleet", phase: "operational", active: true, showInNav: false },
      { path: "/react/devices", label: "Cloud Devices", group: "Fleet", phase: "read-only", active: true },
      { path: "/react/hashes", label: "Hashes", group: "Fleet", phase: "operational", active: true },
      { path: "/react/files", label: "Files", group: "Fleet", phase: "operational", active: true }
    ]
  },
  {
    label: "Settings",
    items: [
      { path: "/react/settings", label: "General", group: "Settings", phase: "operational", active: true },
      { path: "/react/credentials", label: "Credentials", group: "Settings", phase: "operational", active: true },
      { path: "/react/monitoring/settings", label: "Monitoring settings", group: "Settings", phase: "operational", active: true }
    ]
  }
];

export const operatorModes: readonly OperatorMode[] = [
  { id: "home", label: "Home", longLabel: "Home", href: "/react-shell" },
  { id: "deploy", label: "Deploy", longLabel: "Deploy", href: "/react/cloudosd" },
  { id: "build", label: "Build", longLabel: "Build", href: "/react/task-engine" },
  { id: "infra", label: "Infra", longLabel: "Infrastructure", href: "/react/networks" },
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
    id: "shape-lab-network",
    mode: "infra",
    eyebrow: "Network path",
    title: "Shape lab infrastructure",
    summary: "Open Networks to manage Proxmox SDN, firewall scopes, and isolated lab network targets.",
    primaryHref: "/react/networks",
    actionLabel: "Open networks",
    tone: "blue",
    relatedRoutes: [
      { label: "Networks", href: "/react/networks", purpose: "SDN and lab scopes" },
      { label: "Provision", href: "/react/provision", purpose: "Launch into the right network" },
      { label: "VMs", href: "/react/vms", purpose: "Validate attached machines" }
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

export const operatorFlows: readonly OperatorFlow[] = [
  {
    id: "observe",
    label: "Observe",
    group: "Observe",
    summary: "Signals, live jobs, service health, fleet drift.",
    steps: [
      { label: "Dashboard", href: "/react/dashboard", group: "Observe", state: "React" },
      { label: "Signals Hub", href: "/react/monitoring", group: "Observe", state: "React" },
      { label: "Jobs", href: "/react/jobs", group: "Observe", state: "React" },
      { label: "Runs", href: "/react/runs", group: "Observe", state: "React" },
      { label: "Install Tracking", href: "/react/install-tracking", group: "Observe", state: "React" }
    ]
  },
  {
    id: "deploy",
    label: "Deploy",
    group: "Deploy",
    summary: "Choose the deployment path, then open the guarded execution page.",
    steps: [
      { label: "OSDeploy Server", href: "/react/osdeploy", group: "Deploy", state: "React" },
      { label: "OSDCloud Desktop", href: "/react/cloudosd", group: "Deploy", state: "React" },
      { label: "Provision", href: "/react/provision", group: "Deploy", state: "React" }
    ]
  },
  {
    id: "build",
    label: "Build",
    group: "Build",
    summary: "Open build orchestration, templates, and generated media.",
    steps: [
      { label: "Task Sequences", href: "/react/task-engine", group: "Build", state: "React" },
      { label: "Template", href: "/react/template", group: "Build", state: "React" },
      { label: "Answer ISOs", href: "/react/answer-isos", group: "Build", state: "React" }
    ]
  },
  {
    id: "infrastructure",
    label: "Infrastructure",
    group: "Infrastructure",
    summary: "Manage Proxmox SDN, firewall scopes, and isolated lab networks.",
    steps: [
      { label: "Networks", href: "/react/networks", group: "Infrastructure", state: "React" }
    ]
  },
  {
    id: "fleet",
    label: "Fleet",
    group: "Fleet",
    summary: "Triage VM, device, hash, and artifact evidence.",
    steps: [
      { label: "VMs", href: "/react/vms", group: "Fleet", state: "React" },
      { label: "Agent Download", href: "/react/agent-download", group: "Fleet", state: "React" },
      { label: "Cloud Devices", href: "/react/devices", group: "Fleet", state: "React" },
      { label: "Classic VM Table", href: "/react/legacy-vms", group: "Fleet", state: "React" },
      { label: "Hashes", href: "/react/hashes", group: "Fleet", state: "React" },
      { label: "Files", href: "/react/files", group: "Fleet", state: "React" }
    ]
  },
  {
    id: "settings",
    label: "Settings",
    group: "Settings",
    summary: "Keep credentials and monitoring configuration in guarded pages.",
    steps: [
      { label: "Monitoring settings", href: "/react/monitoring/settings", group: "Settings", state: "React" },
      { label: "Credentials", href: "/react/credentials", group: "Settings", state: "React" },
      { label: "General", href: "/react/settings", group: "Settings", state: "React" }
    ]
  }
];

export const migratedRoutes: readonly MigratedRoute[] = operatorNavGroups.flatMap((group) =>
  group.items
    .filter((route): route is OperatorRoute & { readonly active: true } => route.active)
    .map(({ path, label, group: routeGroup, phase }) => ({
      path,
      label,
      group: routeGroup,
      phase: phase === "legacy" ? "read-only" : phase
    }))
);

export function isOperatorNavRoute(route: OperatorRoute): boolean {
  return route.active && route.showInNav !== false && !route.path.includes(":");
}

export const operatorNavItems: readonly OperatorRoute[] = operatorNavGroups.flatMap((group) =>
  group.items.filter(isOperatorNavRoute)
);

export function reactRouteForPath(path: string): OperatorRoute | undefined {
  return operatorNavGroups.flatMap((group) => group.items).find((route) => {
    if (!route.active) {
      return false;
    }
    return routeMatchesPath(route.path, path);
  });
}

export function routeMatchesPath(routePath: string, path: string): boolean {
  if (routePath === path) {
    return true;
  }
  const pattern = `^${routePath.replaceAll("/", "\\/").replace(/:[^/]+/gu, "[^/]+")}$`;
  return new RegExp(pattern, "u").test(path);
}

export function navPathForPath(path: string): string | undefined {
  const route = reactRouteForPath(path);
  return route?.navParentPath ?? (route && isOperatorNavRoute(route) ? route.path : undefined);
}

export function modeForPath(path: string): OperatorModeId {
  if (path === "/react-shell") {
    return "home";
  }
  const route =
    reactRouteForPath(path) ??
    operatorNavGroups
      .flatMap((group) => group.items)
      .filter((item) => item.active && path.startsWith(`${item.path}/`))
      .sort((first, second) => second.path.length - first.path.length)[0];
  if (!route) {
    return "home";
  }
  if (route.group === "Deploy") {
    return "deploy";
  }
  if (route.group === "Build") {
    return "build";
  }
  if (route.group === "Infrastructure") {
    return "infra";
  }
  if (route.group === "Fleet") {
    return "fleet";
  }
  if (route.group === "Settings") {
    return "settings";
  }
  return "home";
}

export function reactHrefForUiPath(href: string): string {
  if (!href.startsWith("/") || href.startsWith("/api/") || href.startsWith("/static/") || href.startsWith("/files/")) {
    return href;
  }
  const parsed = new URL(href, "http://autopilot.local");
  const suffix = `${parsed.search}${parsed.hash}`;
  const path = parsed.pathname;
  const withSuffix = (pathValue: string): string => {
    if (pathValue.includes("?") && suffix.startsWith("?")) {
      return `${pathValue}&${suffix.slice(1)}`;
    }
    return `${pathValue}${suffix}`;
  };
  const dynamicRules: readonly [RegExp, (match: RegExpExecArray) => string][] = [
    [/^\/jobs\/([^/]+)$/u, (match) => `/react/jobs/${match[1] ?? ""}`],
    [/^\/runs\/([^/]+)$/u, (match) => `/react/runs/${match[1] ?? ""}`],
    [/^\/(?:cloudosd|osdcloud)\/runs\/([^/]+)$/u, (match) => `/react/cloudosd/runs/${match[1] ?? ""}`],
    [/^\/osdeploy\/runs\/([^/]+)$/u, (match) => `/react/osdeploy/runs/${match[1] ?? ""}`],
    [/^\/sequences\/\d+\/edit$/u, () => "/react/task-engine/sequences/list"],
    [/^\/devices\/(\d+)$/u, (match) => `/react/vms/${match[1] ?? ""}`],
    [/^\/vms\/(\d+)\/console$/u, (match) => `/react/vms/${match[1] ?? ""}?action=console`]
  ];
  for (const [pattern, build] of dynamicRules) {
    const match = pattern.exec(path);
    if (match) {
      return withSuffix(build(match));
    }
  }
  const staticRules: Readonly<Record<string, string>> = {
    "/": "/react/dashboard",
    "/cloud": "/react/devices",
    "/cloudosd": "/react/cloudosd",
    "/credentials": "/react/credentials",
    "/hashes": "/react/hashes",
    "/legacy/vms": "/react/legacy-vms",
    "/monitoring": "/react/monitoring",
    "/monitoring/settings": "/react/monitoring/settings",
    "/networks": "/react/networks",
    "/osdcloud": "/react/cloudosd",
    "/osdeploy": "/react/osdeploy",
    "/provision": "/react/provision",
    "/runs": "/react/runs",
    "/sequences": "/react/task-engine/sequences/list",
    "/sequences/new": "/react/task-engine/sequences/new",
    "/settings": "/react/settings",
    "/template": "/react/template",
    "/vms": "/react/vms"
  };
  return withSuffix(staticRules[path] ?? path);
}
