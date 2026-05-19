import { describe, expect, test } from "vitest";

import type { JobTableRow, MonitoringOverview } from "./contracts";
import {
  fallbackText,
  formatPercent,
  jobTarget,
  monitoringStrip,
  statusLabel,
  statusTone,
  summarizeJobs
} from "./viewModels";

describe("operator view models", () => {
  test("formats empty and optional values tightly", () => {
    expect(fallbackText(null)).toBe("-");
    expect(fallbackText("")).toBe("-");
    expect(fallbackText(" controller ")).toBe(" controller ");
    expect(formatPercent(72)).toBe("72%");
    expect(formatPercent(undefined)).toBe("-");
  });

  test("maps job states to stable status labels and tones", () => {
    expect(statusLabel("running", false)).toBe("running");
    expect(statusLabel("complete", true)).toBe("paused");
    expect(statusTone("complete")).toBe("good");
    expect(statusTone("pending")).toBe("active");
    expect(statusTone("orphaned")).toBe("bad");
    expect(statusTone(undefined)).toBe("neutral");
  });

  test("chooses the first concrete job target from known argument keys", () => {
    const row: JobTableRow = {
      id: "job-1",
      args: {
        serial: "SER-1",
        vm_name: "VM-1"
      }
    };
    expect(jobTarget(row)).toBe("VM-1");
    expect(jobTarget({ id: "job-2", args: { unknown: "value" } })).toBe("-");
  });

  test("summarizes jobs with exact status buckets", () => {
    const jobs: readonly JobTableRow[] = [
      { id: "a", status: "running" },
      { id: "b", status: "pending" },
      { id: "c", status: "queued" },
      { id: "d", status: "failed" },
      { id: "e", status: "complete" },
      { id: "f", status: "orphaned" },
      { id: "g", paused: true }
    ];
    expect(summarizeJobs(jobs)).toEqual({
      total: 7,
      running: 1,
      queued: 2,
      failed: 2,
      complete: 1,
      paused: 1
    });
  });

  test("builds monitoring strip values without inventing missing data", () => {
    const overview: MonitoringOverview = {
      runtime: { available: true, error: "", containers: [{ name: "autopilot", service: "autopilot", status: "running", health: "healthy" }] },
      deployments: { total: 3, running: 1, succeeded: 2, failed: 0 },
      keytab: {}
    };
    expect(monitoringStrip(overview)).toEqual([
      { label: "Runtime", value: "1", tone: "good" },
      { label: "Deployments", value: "3", tone: "active" },
      { label: "Failed", value: "0", tone: "good" },
      { label: "Keytab", value: "-", tone: "neutral" }
    ]);
  });
});
