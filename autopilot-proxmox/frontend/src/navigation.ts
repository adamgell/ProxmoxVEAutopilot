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
