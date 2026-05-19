import { describe, expect, test } from "vitest";

import { migratedRoutes, operatorNavGroups, reactRouteForPath } from "./routes";

describe("operator route registry", () => {
  test("registers only active React routes as migrated routes", () => {
    expect(migratedRoutes).toEqual([
      {
        path: "/react-shell",
        label: "Shell",
        group: "Observe",
        phase: "foundation"
      },
      {
        path: "/react/dashboard",
        label: "Dashboard",
        group: "Observe",
        phase: "read-only"
      },
      {
        path: "/react/jobs",
        label: "Jobs",
        group: "Observe",
        phase: "read-only"
      },
      {
        path: "/react/monitoring",
        label: "Signals Hub",
        group: "Observe",
        phase: "read-only"
      }
    ]);
  });

  test("keeps refined operator groups stable without legacy clone or WinPE-first entries", () => {
    expect(operatorNavGroups.map((group) => group.label)).toEqual([
      "Observe",
      "Deploy",
      "Build",
      "Fleet",
      "Settings"
    ]);
    expect(
      operatorNavGroups.flatMap((group) => group.items.map((item) => item.label.toLowerCase()))
    ).not.toEqual(expect.arrayContaining(["clone", "winpe"]));
  });

  test("finds active React routes and ignores legacy deep links", () => {
    expect(reactRouteForPath("/react/jobs")?.label).toBe("Jobs");
    expect(reactRouteForPath("/monitoring")).toBeUndefined();
  });
});
