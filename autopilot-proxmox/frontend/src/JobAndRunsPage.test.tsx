import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const responses: Readonly<Record<string, unknown>> = {
  "/api/jobs/job-1/page": {
    job: {
      id: "job-1",
      playbook: "provision.yml",
      status: "running",
      started: "2026-05-20T12:00:00-04:00",
      ended: null,
      exit_code: null,
      paused: true,
      args: { pause_enabled: true, pause_signal_path: "/tmp/pause" }
    },
    plan: {
      title: "Provision VM",
      summary: "Create and stage a VM.",
      metadata: [["VMID", "105"]],
      steps: ["Create VM", "Install agent"],
      end_goal: "VM ready"
    },
    log: ["started", "running"],
    log_content: "started\nrunning",
    stream_url: "/api/jobs/job-1/stream"
  },
  "/api/runs/page": {
    runs: [{
      id: 1,
      sequence_name: "Windows baseline",
      vmid: 105,
      vm_uuid: "uuid-1",
      state: "running",
      ok_count: 1,
      running_count: 1,
      error_count: 0,
      step_count: 3,
      started_at: "2026-05-20T12:10:00-04:00"
    }]
  },
  "/api/runs/1/page": {
    run: {
      id: 1,
      sequence_name: "Windows baseline",
      vmid: 105,
      vm_uuid: "uuid-1",
      state: "running",
      started_at: "2026-05-20T12:10:00-04:00"
    },
    steps: [
      { order_index: 0, phase: "winpe", kind: "partition_disk", state: "ok", started_at: "2026-05-20T12:11:00-04:00" },
      { order_index: 1, phase: "winpe", kind: "apply_wim", state: "running", started_at: "2026-05-20T12:12:00-04:00" }
    ],
    step_counts: { total: 2, ok: 1, running: 1, error: 0 }
  }
};

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" }
  });
}

function mockFetch() {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/api/jobs/job-1/resume-template-build" && init?.method === "POST") {
      return Promise.resolve(response({ ok: true, signal_path: "/tmp/pause" }));
    }
    if (url.pathname === "/api/jobs/job-1/kill" && init?.method === "POST") {
      return Promise.resolve(response({ ok: true }));
    }
    return Promise.resolve(response(responses[url.pathname] ?? {}));
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("WebSocket", undefined);
  return fetchMock;
}

function renderPath(path: string) {
  window.history.pushState({}, "", path);
  render(<App bootstrap={{ buildSha: "testsha", buildTime: "2026-05-20T12:00:00-04:00" }} />);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("job and run React pages", () => {
  test("renders job detail metadata, plan, output, and resume action", async () => {
    const fetchMock = mockFetch();
    renderPath("/react/jobs/job-1");

    expect(await screen.findByRole("heading", { name: "Job job-1" })).toBeInTheDocument();
    expect(await screen.findByText("provision.yml")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Provision VM" })).toBeInTheDocument();
    expect(screen.getByLabelText("Job output")).toHaveTextContent("started");

    fireEvent.click(screen.getByRole("button", { name: /Resume/u }));
    await waitFor(() => expect(screen.getByText("resume signal sent")).toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([input, init]) => input === "/api/jobs/job-1/resume-template-build" && init?.method === "POST")).toBe(true);
  });

  test("renders and filters the WinPE runs list", async () => {
    mockFetch();
    renderPath("/react/runs");

    expect(await screen.findByRole("heading", { name: "WinPE Runs" })).toBeInTheDocument();
    expect(await screen.findByText("Windows baseline")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "WinPE runs" })).toHaveTextContent("Windows baseline");
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter runs" }), { target: { value: "missing" } });
    expect(screen.getByRole("table", { name: "WinPE runs" })).not.toHaveTextContent("Windows baseline");
  });

  test("renders run detail metrics and WinPE task plan", async () => {
    mockFetch();
    renderPath("/react/runs/1");

    expect(await screen.findByRole("heading", { name: "Run 1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Run Metadata" })).toBeInTheDocument();
    expect(await screen.findByText("partition_disk")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "WinPE task plan" })).toHaveTextContent("partition_disk");
    expect(screen.getByText("uuid-1")).toBeInTheDocument();
  });
});
