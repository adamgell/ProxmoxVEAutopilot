import { describe, expect, test } from "vitest";

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

describe("operator route registry", () => {
  test("registers only active React routes as migrated routes", () => {
    expect(migratedRoutes.every((route) => route.path.startsWith("/react"))).toBe(true);
    expect(migratedRoutes.map((route) => route.path)).toEqual(
      expect.arrayContaining([
        "/react-shell",
        "/react/dashboard",
        "/react/jobs",
        "/react/jobs/:jobId",
        "/react/monitoring",
        "/react/runs",
        "/react/runs/:runId",
        "/react/provision",
        "/react/cloudosd",
        "/react/cloudosd/runs/:runId",
        "/react/osdeploy",
        "/react/osdeploy/runs/:runId",
        "/react/template",
        "/react/task-engine",
        "/react/task-engine/sequences/list",
        "/react/task-engine/sequences/new",
        "/react/task-engine/sequences/templates/:templateId",
        "/react/task-engine/sequences/:sequenceId/edit",
        "/react/answer-isos",
        "/react/vms",
        "/react/utm-vms",
        "/react/sequences",
        "/react/sequences/new",
        "/react/sequences/:sequenceId/edit",
        "/react/settings",
        "/react/credentials",
        "/react/monitoring/settings"
      ])
    );
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
    expect(reactRouteForPath("/react/agent-download")?.label).toBe("Agent Download");
    expect(reactRouteForPath("/react/hashes")?.label).toBe("Hashes");
    expect(reactRouteForPath("/react/settings")?.label).toBe("General");
    expect(reactRouteForPath("/react/cloudosd")?.label).toBe("OSDCloud Desktop");
    expect(reactRouteForPath("/react/task-engine")?.label).toBe("Task Engine");
    expect(reactRouteForPath("/monitoring")).toBeUndefined();
  });

  test("maps refined operator flows to React starts without Jinja steps", () => {
    expect(operatorFlows.map((flow) => flow.label)).toEqual([
      "Observe",
      "Deploy",
      "Build",
      "Fleet",
      "Settings"
    ]);
    const reactSteps = operatorFlows.flatMap((flow) => flow.steps.filter((step) => step.state === "React"));
    const jinjaSteps = operatorFlows.flatMap((flow) => flow.steps.filter((step) => step.state === "Jinja"));
    expect(jinjaSteps).toEqual([]);
    expect(reactSteps).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "Signals Hub", href: "/react/monitoring" }),
        expect.objectContaining({ label: "Jobs", href: "/react/jobs" }),
        expect.objectContaining({ label: "Runs", href: "/react/runs" }),
        expect.objectContaining({ label: "OSDeploy Server", href: "/react/osdeploy" }),
        expect.objectContaining({ label: "OSDCloud Desktop", href: "/react/cloudosd" }),
        expect.objectContaining({ label: "Provision", href: "/react/provision" }),
        expect.objectContaining({ label: "Task Engine", href: "/react/task-engine" }),
        expect.objectContaining({ label: "VMs", href: "/react/vms" }),
        expect.objectContaining({ label: "Agent Download", href: "/react/agent-download" }),
        expect.objectContaining({ label: "Cloud Devices", href: "/react/devices" }),
        expect.objectContaining({ label: "Monitoring settings", href: "/react/monitoring/settings" })
      ])
    );
    expect(reactSteps.filter((step) => step.label === "Signals Hub")).toHaveLength(1);
  });

  test("defines the compact outcome modes in operator order", () => {
    expect(operatorModes.map((mode) => [mode.id, mode.label, mode.href])).toEqual([
      ["home", "Home", "/react-shell"],
      ["deploy", "Deploy", "/react/cloudosd"],
      ["build", "Build", "/react/task-engine"],
      ["fleet", "Fleet", "/react/vms"],
      ["settings", "Set", "/react/settings"]
    ]);
  });

  test("defines quick routes for repeated operator jumps", () => {
    expect(operatorQuickRoutes.map((route) => [route.label, route.href, route.mode])).toEqual([
      ["Jobs", "/react/jobs", "home"],
      ["VMs", "/react/vms", "fleet"],
      ["Hashes", "/react/hashes", "fleet"],
      ["Runs", "/react/runs", "home"]
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
    const relatedRouteHrefs = operatorOutcomes.flatMap((outcome) => outcome.relatedRoutes.map((route) => route.href));
    expect(relatedRouteHrefs).toEqual(
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
    expect(relatedRouteHrefs).not.toContain("/react/jobs/:jobId");
    expect(relatedRouteHrefs).not.toContain("/react/cloudosd/runs/:runId");
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
});
