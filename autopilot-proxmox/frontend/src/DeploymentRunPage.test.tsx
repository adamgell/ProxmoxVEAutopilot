import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const runPayload = {
  run: {
    run_id: "run-1",
    requested_vm_name: "Gell-EC41E7EB",
    expected_computer_name: "GELL-EC41E7EB",
    vmid: 105,
    state: "running",
    artifact_id: "artifact-1",
    created_at: "2026-05-20T12:00:00-04:00"
  },
  artifact: { build_sha: "build-1" },
  readiness: { agent_status: "pending", qga_status: "ready", server_role_status: "domain_controller" },
  latest_heartbeat: { received_at: "2026-05-20T12:05:00-04:00", agent_version: "0.1.10.0" },
  heartbeat: { received_at: "2026-05-20T12:05:00-04:00" },
  v2_operator_status: { label: "waiting for heartbeat" },
  v2_steps: [
    { id: "step-1", ordinal: 0, phase: "pe", name: "Preflight", kind: "cloudosd_preflight", state: "done", attempt: 1 },
    { id: "step-2", ordinal: 1, phase: "full_os", name: "Heartbeat", kind: "wait_agent_heartbeat", state: "running", attempt: 1 }
  ],
  events: [{ id: 1, created_at: "2026-05-20T12:01:00-04:00", phase: "pe", event_type: "started", message: "run started" }],
  related_jobs: [{ id: "job-1", playbook: "cloudosd.yml", status: "running" }]
};

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" }
  });
}

function mockFetch() {
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/api/cloudosd/runs/run-1/page" || url.pathname === "/api/osdeploy/runs/run-1/page") {
      return Promise.resolve(response(runPayload));
    }
    return Promise.resolve(response({}));
  }));
}

function renderPath(path: string) {
  window.history.pushState({}, "", path);
  render(<App bootstrap={{ buildSha: "testsha", buildTime: "2026-05-20T12:00:00-04:00" }} />);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DeploymentRunPage", () => {
  test("renders CloudOSD run evidence, v2 steps, events, and related jobs", async () => {
    mockFetch();
    renderPath("/react/cloudosd/runs/run-1");

    expect(await screen.findByRole("heading", { name: "OSDCloud Run run-1" })).toBeInTheDocument();
    expect(await screen.findByText("Gell-EC41E7EB")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "OSDCloud v2 steps" })).toHaveTextContent("wait_agent_heartbeat");
    expect(screen.getByRole("table", { name: "OSDCloud events" })).toHaveTextContent("run started");
    expect(screen.getByRole("table", { name: "Related jobs" })).toHaveTextContent("job-1");
  });

  test("renders OSDeploy run evidence using the same deployment detail surface", async () => {
    mockFetch();
    renderPath("/react/osdeploy/runs/run-1");

    expect(await screen.findByRole("heading", { name: "OSDeploy Run run-1" })).toBeInTheDocument();
    expect(await screen.findByText("domain_controller")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "OSDeploy v2 steps" })).toHaveTextContent("cloudosd_preflight");
    expect(screen.getByRole("table", { name: "OSDeploy events" })).toHaveTextContent("started");
  });
});
