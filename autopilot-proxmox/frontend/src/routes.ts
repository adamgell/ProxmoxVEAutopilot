import type { MigratedRoute, OperatorNavGroup, OperatorRoute } from "./contracts";

export const operatorNavGroups: readonly OperatorNavGroup[] = [
  {
    label: "Observe",
    items: [
      { path: "/react-shell", label: "Shell", group: "Observe", phase: "foundation", active: true },
      { path: "/react/dashboard", label: "Dashboard", group: "Observe", phase: "read-only", active: true },
      { path: "/react/jobs", label: "Jobs", group: "Observe", phase: "read-only", active: true },
      { path: "/react/monitoring", label: "Monitoring", group: "Observe", phase: "read-only", active: true },
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
