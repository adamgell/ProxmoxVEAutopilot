import type { MigratedRoute, OperatorFlow, OperatorNavGroup, OperatorRoute } from "./contracts";

export const operatorNavGroups: readonly OperatorNavGroup[] = [
  {
    label: "Observe",
    items: [
      { path: "/react-shell", label: "Workspace", group: "Observe", phase: "foundation", active: true },
      { path: "/react/dashboard", label: "Dashboard", group: "Observe", phase: "read-only", active: true },
      { path: "/react/jobs", label: "Jobs", group: "Observe", phase: "read-only", active: true },
      { path: "/react/monitoring", label: "Signals Hub", group: "Observe", phase: "read-only", active: true },
      { path: "/runs", label: "Runs", group: "Observe", phase: "legacy", active: false, legacy: true }
    ]
  },
  {
    label: "Deploy",
    items: [
      { path: "/cloudosd", label: "OSDCloud Desktop", group: "Deploy", phase: "legacy", active: false, legacy: true },
      { path: "/osdeploy", label: "OSDeploy Server", group: "Deploy", phase: "legacy", active: false, legacy: true },
      { path: "/provision", label: "Provision", group: "Deploy", phase: "legacy", active: false, legacy: true }
    ]
  },
  {
    label: "Build",
    items: [
      { path: "/template", label: "Template", group: "Build", phase: "legacy", active: false, legacy: true },
      { path: "/task-engine", label: "Task Engine", group: "Build", phase: "legacy", active: false, legacy: true },
      { path: "/answer-isos", label: "Answer ISOs", group: "Build", phase: "legacy", active: false, legacy: true }
    ]
  },
  {
    label: "Fleet",
    items: [
      { path: "/vms", label: "VMs", group: "Fleet", phase: "legacy", active: false, legacy: true },
      { path: "/devices", label: "Devices", group: "Fleet", phase: "legacy", active: false, legacy: true },
      { path: "/hashes", label: "Hashes", group: "Fleet", phase: "legacy", active: false, legacy: true },
      { path: "/files", label: "Files", group: "Fleet", phase: "legacy", active: false, legacy: true }
    ]
  },
  {
    label: "Settings",
    items: [
      { path: "/settings", label: "General", group: "Settings", phase: "legacy", active: false, legacy: true },
      { path: "/credentials", label: "Credentials", group: "Settings", phase: "legacy", active: false, legacy: true },
      { path: "/monitoring/settings", label: "Monitoring settings", group: "Settings", phase: "legacy", active: false, legacy: true }
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
      { label: "Runs", href: "/runs", group: "Observe", state: "Jinja" }
    ]
  },
  {
    id: "deploy",
    label: "Deploy",
    group: "Deploy",
    summary: "Start with readiness, then open the existing execution flow.",
    steps: [
      { label: "Signals Hub", href: "/react/monitoring", group: "Observe", state: "React" },
      { label: "OSDeploy Server", href: "/osdeploy", group: "Deploy", state: "Jinja" },
      { label: "OSDCloud Desktop", href: "/cloudosd", group: "Deploy", state: "Jinja" },
      { label: "Provision", href: "/provision", group: "Deploy", state: "Jinja" }
    ]
  },
  {
    id: "build",
    label: "Build",
    group: "Build",
    summary: "Watch build host signals before opening legacy build pages.",
    steps: [
      { label: "Signals Hub", href: "/react/monitoring", group: "Observe", state: "React" },
      { label: "Task Engine", href: "/task-engine", group: "Build", state: "Jinja" },
      { label: "Template", href: "/template", group: "Build", state: "Jinja" },
      { label: "Answer ISOs", href: "/answer-isos", group: "Build", state: "Jinja" }
    ]
  },
  {
    id: "fleet",
    label: "Fleet",
    group: "Fleet",
    summary: "Triage VM, device, hash, and artifact evidence.",
    steps: [
      { label: "Signals Hub", href: "/react/monitoring", group: "Observe", state: "React" },
      { label: "VMs", href: "/vms", group: "Fleet", state: "Jinja" },
      { label: "Devices", href: "/devices", group: "Fleet", state: "Jinja" },
      { label: "Hashes", href: "/hashes", group: "Fleet", state: "Jinja" }
    ]
  },
  {
    id: "settings",
    label: "Settings",
    group: "Settings",
    summary: "Keep credentials and monitoring configuration in guarded pages.",
    steps: [
      { label: "Monitoring settings", href: "/monitoring/settings", group: "Settings", state: "Jinja" },
      { label: "Credentials", href: "/credentials", group: "Settings", state: "Jinja" },
      { label: "General", href: "/settings", group: "Settings", state: "Jinja" }
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
  return operatorNavGroups.flatMap((group) => group.items).find((route) => route.active && route.path === path);
}
