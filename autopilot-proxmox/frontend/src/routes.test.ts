import { describe, expect, test } from "vitest";

import { migratedRoutes } from "./routes";

describe("migratedRoutes", () => {
  test("starts with only the protected shell route", () => {
    expect(migratedRoutes).toEqual([
      {
        path: "/react-shell",
        label: "React Shell",
        phase: "foundation"
      }
    ]);
  });
});
