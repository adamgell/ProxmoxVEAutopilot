import { describe, expect, test } from "vitest";

import type { OperatorRoute } from "./contracts";
import { resolveCommandTarget } from "./navigation";

const routes: readonly OperatorRoute[] = [
  { path: "/react/dashboard", label: "Dashboard", group: "Observe", phase: "read-only", active: true },
  { path: "/react/cloudosd", label: "OSDCloud Desktop", group: "Deploy", phase: "operational", active: true },
  { path: "/react/jobs/:jobId", label: "Job Detail", group: "Observe", phase: "operational", active: true },
  { path: "/react/hashes", label: "Hashes", group: "Fleet", phase: "operational", active: true }
];

describe("resolveCommandTarget", () => {
  test("returns null for blank input", () => {
    expect(resolveCommandTarget("", routes)).toBeNull();
    expect(resolveCommandTarget("   ", routes)).toBeNull();
  });

  test("prefers exact route label matches", () => {
    expect(resolveCommandTarget("hashes", routes)).toBe("/react/hashes");
    expect(resolveCommandTarget("OSDCloud Desktop", routes)).toBe("/react/cloudosd");
  });

  test("uses partial route label matches after exact matches", () => {
    expect(resolveCommandTarget("desktop", routes)).toBe("/react/cloudosd");
  });

  test("routes numeric searches to VM detail", () => {
    expect(resolveCommandTarget("109", routes)).toBe("/react/vms/109");
  });

  test("routes unknown searches to filtered fleet search", () => {
    expect(resolveCommandTarget("NTTENANT01", routes)).toBe("/react/vms?search=NTTENANT01");
    expect(resolveCommandTarget("VM 109 ready", routes)).toBe("/react/vms?search=VM%20109%20ready");
  });
});
