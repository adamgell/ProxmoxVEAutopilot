import type { MigratedRoute, OperatorFlow, OperatorNavGroup, OperatorRoute } from "./contracts";

export const operatorNavGroups: readonly OperatorNavGroup[] = [
  {
    label: "Observe",
    items: [
      { path: "/react-shell", label: "Workspace", group: "Observe", phase: "foundation", active: true },
      { path: "/react/dashboard", label: "Dashboard", group: "Observe", phase: "read-only", active: true },
      { path: "/react/jobs", label: "Jobs", group: "Observe", phase: "read-only", active: true },
      { path: "/react/jobs/:jobId", label: "Job Detail", group: "Observe", phase: "operational", active: true },
      { path: "/react/monitoring", label: "Signals Hub", group: "Observe", phase: "read-only", active: true },
      { path: "/react/runs", label: "Runs", group: "Observe", phase: "read-only", active: true },
      { path: "/react/runs/:runId", label: "Run Detail", group: "Observe", phase: "read-only", active: true },
      { path: "/react/install-tracking", label: "Install Tracking", group: "Observe", phase: "read-only", active: true }
    ]
  },
  {
    label: "Deploy",
    items: [
      { path: "/react/cloudosd", label: "OSDCloud Desktop", group: "Deploy", phase: "operational", active: true },
      { path: "/react/cloudosd/runs/:runId", label: "OSDCloud Run", group: "Deploy", phase: "operational", active: true },
      { path: "/react/osdeploy", label: "OSDeploy Server", group: "Deploy", phase: "operational", active: true },
      { path: "/react/osdeploy/runs/:runId", label: "OSDeploy Run", group: "Deploy", phase: "operational", active: true },
      { path: "/react/provision", label: "Provision", group: "Deploy", phase: "operational", active: true }
    ]
  },
  {
    label: "Build",
    items: [
      { path: "/react/template", label: "Template", group: "Build", phase: "operational", active: true },
      { path: "/react/task-engine", label: "Task Engine", group: "Build", phase: "operational", active: true },
      { path: "/react/task-engine/sequences/list", label: "Task Sequences", group: "Build", phase: "operational", active: true },
      { path: "/react/task-engine/sequences/new", label: "New Task Sequence", group: "Build", phase: "operational", active: true },
      { path: "/react/task-engine/sequences/templates/:templateId", label: "Task Template", group: "Build", phase: "read-only", active: true },
      { path: "/react/task-engine/sequences/:sequenceId/edit", label: "Edit Task Sequence", group: "Build", phase: "operational", active: true },
      { path: "/react/answer-isos", label: "Answer ISOs", group: "Build", phase: "operational", active: true }
    ]
  },
  {
    label: "Fleet",
    items: [
      { path: "/react/vms", label: "VMs", group: "Fleet", phase: "operational", active: true },
      { path: "/react/agent-download", label: "Agent Download", group: "Fleet", phase: "operational", active: true },
      { path: "/react/legacy-vms", label: "Classic VM Table", group: "Fleet", phase: "read-only", active: true },
      { path: "/react/utm-vms", label: "UTM VMs", group: "Fleet", phase: "operational", active: true },
      { path: "/react/devices", label: "Cloud Devices", group: "Fleet", phase: "read-only", active: true },
      { path: "/react/hashes", label: "Hashes", group: "Fleet", phase: "operational", active: true },
      { path: "/react/files", label: "Files", group: "Fleet", phase: "operational", active: true },
      { path: "/react/sequences", label: "Sequences", group: "Fleet", phase: "operational", active: true },
      { path: "/react/sequences/new", label: "New Sequence", group: "Fleet", phase: "operational", active: true },
      { path: "/react/sequences/:sequenceId/edit", label: "Edit Sequence", group: "Fleet", phase: "operational", active: true }
    ]
  },
  {
    label: "Settings",
    items: [
      { path: "/react/onboarding", label: "Onboarding wizard", group: "Settings", phase: "operational", active: true },
      {
        path: "/react/onboarding/setup",
        label: "Onboarding setup monitor",
        group: "Settings",
        phase: "operational",
        active: true,
        navParentPath: "/react/onboarding",
        showInNav: false
      },
      { path: "/react/settings", label: "General", group: "Settings", phase: "operational", active: true },
      { path: "/react/credentials", label: "Credentials", group: "Settings", phase: "operational", active: true },
      { path: "/react/monitoring/settings", label: "Monitoring settings", group: "Settings", phase: "operational", active: true }
    ]
  }
];

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
      { label: "Task Engine", href: "/react/task-engine", group: "Build", state: "React" },
      { label: "Template", href: "/react/template", group: "Build", state: "React" },
      { label: "Answer ISOs", href: "/react/answer-isos", group: "Build", state: "React" }
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
      { label: "UTM VMs", href: "/react/utm-vms", group: "Fleet", state: "React" },
      { label: "Hashes", href: "/react/hashes", group: "Fleet", state: "React" },
      { label: "Files", href: "/react/files", group: "Fleet", state: "React" },
      { label: "Sequences", href: "/react/sequences", group: "Fleet", state: "React" }
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

export function reactRouteForPath(path: string): OperatorRoute | undefined {
  return operatorNavGroups.flatMap((group) => group.items).find((route) => {
    if (!route.active) {
      return false;
    }
    if (route.path === path) {
      return true;
    }
    const pattern = `^${route.path.replaceAll("/", "\\/").replace(/:[^/]+/gu, "[^/]+")}$`;
    return new RegExp(pattern, "u").test(path);
  });
}
