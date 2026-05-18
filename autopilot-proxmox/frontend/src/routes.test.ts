import { describe, expect, test } from "vitest";

import { migratedRoutes } from "./routes";

describe("migratedRoutes", () => {
  test("registers the protected shell and read-only React routes", () => {
    expect(migratedRoutes).toEqual([
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
    ]);
  });
});
