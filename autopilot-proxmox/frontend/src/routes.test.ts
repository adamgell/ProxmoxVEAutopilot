import { describe, expect, test } from "vitest";

import { migratedRoutes, operatorFlows, operatorNavGroups, reactRouteForPath } from "./routes";

describe("operator route registry", () => {
  test("registers only active React routes as migrated routes", () => {
    expect(migratedRoutes).toEqual([
      {
        path: "/react-shell",
        label: "Workspace",
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
      },
      {
        path: "/react/vms",
        label: "VMs",
        group: "Fleet",
        phase: "operational"
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
    expect(reactRouteForPath("/react/vms")?.label).toBe("VMs");
    expect(reactRouteForPath("/monitoring")).toBeUndefined();
  });

  test("maps refined operator flows to React starts and legacy deep links", () => {
    expect(operatorFlows.map((flow) => flow.label)).toEqual([
      "Observe",
      "Deploy",
      "Build",
      "Fleet",
      "Settings"
    ]);
    const reactSteps = operatorFlows.flatMap((flow) => flow.steps.filter((step) => step.state === "React"));
    expect(reactSteps).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "Signals Hub", href: "/react/monitoring" }),
        expect.objectContaining({ label: "Jobs", href: "/react/jobs" }),
        expect.objectContaining({ label: "VMs", href: "/react/vms" })
      ])
    );
    expect(reactSteps.filter((step) => step.label === "Signals Hub")).toHaveLength(1);
    expect(operatorFlows.find((flow) => flow.id === "deploy")?.steps).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "OSDeploy Server", href: "/osdeploy", state: "Jinja" }),
        expect.objectContaining({ label: "OSDCloud Desktop", href: "/cloudosd", state: "Jinja" })
      ])
    );
  });
});
