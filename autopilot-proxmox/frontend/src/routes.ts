import type { MigratedRoute } from "./contracts";

export const migratedRoutes: readonly MigratedRoute[] = [
  {
    path: "/react-shell",
    label: "React Shell",
    phase: "foundation"
  },
  {
    path: "/react/dashboard",
    label: "Dashboard",
    phase: "read-only"
  },
  {
    path: "/react/jobs",
    label: "Jobs",
    phase: "read-only"
  }
];
