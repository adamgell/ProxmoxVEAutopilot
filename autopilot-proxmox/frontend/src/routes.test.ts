import { describe, expect, test } from "vitest";

import {
  migratedRoutes,
  navPathForPath,
  operatorFlows,
  operatorNavGroups,
  operatorNavItems,
  reactHrefForUiPath,
  reactRouteForPath
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
        "/react/vms/:vmid",
        "/react/utm-vms",
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
    expect(reactRouteForPath("/react/jobs/job-123")?.label).toBe("Job Detail");
    expect(reactRouteForPath("/react/vms")?.label).toBe("VMs");
    expect(reactRouteForPath("/react/vms/108")?.label).toBe("VM Detail");
    expect(reactRouteForPath("/react/agent-download")?.label).toBe("Agent Download");
    expect(reactRouteForPath("/react/hashes")?.label).toBe("Hashes");
    expect(reactRouteForPath("/react/settings")?.label).toBe("General");
    expect(reactRouteForPath("/react/cloudosd")?.label).toBe("OSDCloud Desktop");
    expect(reactRouteForPath("/react/task-engine")?.label).toBe("Task Sequences");
    expect(reactRouteForPath("/monitoring")).toBeUndefined();
  });

  test("keeps primary navigation free of parameterized route templates", () => {
    expect(operatorNavItems.every((route) => !route.path.includes(":"))).toBe(true);
    expect(operatorNavItems.map((route) => route.label)).not.toEqual(
      expect.arrayContaining([
        "Job Detail",
        "Run Detail",
        "OSDCloud Run",
        "OSDeploy Run",
        "VM Detail",
        "UTM VMs",
        "Sequence Library",
        "New Sequence",
        "Task Template",
        "Edit Task Sequence",
        "Edit Sequence"
      ])
    );
    expect(navPathForPath("/react/jobs/job-123")).toBe("/react/jobs");
    expect(navPathForPath("/react/vms/108")).toBe("/react/vms");
    expect(navPathForPath("/react/task-engine/sequences/7/edit")).toBe("/react/task-engine/sequences/list");
  });

  test("normalizes old operator UI hrefs into React destinations", () => {
    expect(reactHrefForUiPath("/jobs/job-running")).toBe("/react/jobs/job-running");
    expect(reactHrefForUiPath("/osdeploy")).toBe("/react/osdeploy");
    expect(reactHrefForUiPath("/osdeploy/runs/run-1")).toBe("/react/osdeploy/runs/run-1");
    expect(reactHrefForUiPath("/cloudosd/runs/run-1")).toBe("/react/cloudosd/runs/run-1");
    expect(reactHrefForUiPath("/devices/108")).toBe("/react/vms/108");
    expect(reactHrefForUiPath("/vms/108/console")).toBe("/react/vms/108?action=console");
    expect(reactHrefForUiPath("/vms/108/console?source=tray")).toBe("/react/vms/108?action=console&source=tray");
    expect(reactHrefForUiPath("/sequences")).toBe("/react/task-engine/sequences/list");
    expect(reactHrefForUiPath("/sequences/new")).toBe("/react/task-engine/sequences/new");
    expect(reactHrefForUiPath("/sequences/1/edit")).toBe("/react/task-engine/sequences/list");
    expect(reactHrefForUiPath("/files/AutopilotAgent.msi")).toBe("/files/AutopilotAgent.msi");
    expect(reactHrefForUiPath("/api/hashes/foo.csv")).toBe("/api/hashes/foo.csv");
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
        expect.objectContaining({ label: "Task Sequences", href: "/react/task-engine" }),
        expect.objectContaining({ label: "VMs", href: "/react/vms" }),
        expect.objectContaining({ label: "Agent Download", href: "/react/agent-download" }),
        expect.objectContaining({ label: "Cloud Devices", href: "/react/devices" }),
        expect.objectContaining({ label: "Monitoring settings", href: "/react/monitoring/settings" })
      ])
    );
    expect(reactSteps.filter((step) => step.label === "Signals Hub")).toHaveLength(1);
    expect(reactSteps.some((step) => step.label === "UTM VMs")).toBe(false);
    expect(reactSteps.some((step) => step.href === "/react/sequences")).toBe(false);
  });
});
