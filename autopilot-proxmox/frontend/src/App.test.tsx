import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const dashboardResponses: Record<string, unknown> = {
  "/api/services": {
    available: true,
    services: [
      {
        service: "autopilot",
        status: "healthy",
        age_seconds: 10,
        detail: "running"
      }
    ]
  },
  "/api/jobs/running": {
    running: [
      {
        id: "job-running",
        playbook: "provision.yml",
        target: "PC-001",
        started: "2026-05-18T12:00:00+00:00",
        elapsed_seconds: 120,
        progress_pct: 10,
        paused: false
      }
    ],
    running_count: 1,
    queued_count: 2
  },
  "/api/jobs/recent?limit=5": {
    jobs: [
      {
        id: "job-complete",
        playbook: "template.yml",
        status: "complete",
        started: "2026-05-18T11:00:00+00:00",
        ended: "2026-05-18T11:10:00+00:00",
        duration: "10m 0s",
        target: "Win11-Template"
      }
    ]
  },
  "/api/fleet/summary": {
    total: 4,
    ad_joined_pct: 75,
    autopilot_pct: 50,
    intune_pct: 25
  },
  "/api/monitoring/runtime-services": {
    available: true,
    error: "",
    containers: [
      {
        id: "abc123",
        name: "autopilot",
        service: "autopilot",
        image: "proxmox-autopilot:latest",
        status: "running",
        health: "healthy",
        restart_count: 0,
        log_url: "/api/monitoring/service-logs?container=autopilot"
      }
    ]
  },
  "/api/monitoring/deployments/summary": {
    total: 2,
    running: 1,
    succeeded: 1,
    failed: 0
  },
  "/api/monitoring/keytab/health": {
    status: "ok",
    detail: "keytab valid"
  },
  "/api/monitoring/signals": {
    generated_at: "2026-05-19T00:00:00Z",
    build: {
      sha_short: "abc1234",
      build_time: "2026-05-18T12:00:00Z"
    },
    source_health: {
      runtime_available: true,
      setup_health: "ready"
    },
    metrics: [],
    signals: [
      {
        id: "runtime",
        family: "runtime",
        label: "Runtime containers",
        status: "healthy",
        tone: "good",
        summary: "autopilot is healthy"
      },
      {
        id: "build-host",
        family: "build_host",
        label: "Build host agent",
        status: "ready",
        tone: "good",
        summary: "buildhost-100 heartbeat fresh"
      },
      {
        id: "artifacts",
        family: "artifacts",
        label: "Operational artifacts",
        status: "ready",
        tone: "good",
        summary: "1 OSDeploy artifact ready"
      }
    ],
    operator_paths: [
      {
        id: "stage-media",
        priority: 10,
        label: "Stage Windows ISO and VirtIO media",
        status: "blocked",
        tone: "bad",
        summary: "Bootstrap media is not staged.",
        action_label: "Open setup",
        href: "/setup"
      },
      {
        id: "server-deploy",
        priority: 30,
        label: "Windows Server OSDeploy artifact is available",
        status: "ready",
        tone: "good",
        summary: "Open the existing OSDeploy execution flow.",
        action_label: "Open server deploy",
        href: "/osdeploy"
      }
    ]
  },
  "/api/version": {
    sha_short: "abc1234",
    build_time: "2026-05-18T12:00:00Z"
  }
};

function mockFetch(responses: Record<string, unknown>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    let url: string;
    if (typeof input === "string") {
      url = input;
    } else if (input instanceof URL) {
      url = input.toString();
    } else {
      url = input.url;
    }
    const body = responses[url];
    if (body === undefined) {
      return Promise.resolve(new Response("not found", { status: 404 }));
    }
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );
  });
}

function renderRoute(path: string) {
  window.history.pushState({}, "", path);
  return render(<App bootstrap={{ buildSha: "abc1234", buildTime: "2026-05-18T12:00:00Z" }} />);
}

describe("App", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.pushState({}, "", "/");
  });

  test("renders the protected shell status without operational controls", () => {
    renderRoute("/react-shell");

    expect(screen.getByRole("heading", { name: "Proxmox VE Autopilot" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Operator workspace" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Signals Hub" })).toHaveAttribute("href", "/react/monitoring");
    expect(screen.getByRole("link", { name: "OSDCloud Desktop" })).toHaveAttribute("href", "/cloudosd");
    expect(screen.getByText("Build abc1234")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /provision/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^clone$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /winpe/i })).not.toBeInTheDocument();
  });

  test("renders the dashboard read-only slice from API data", async () => {
    mockFetch(dashboardResponses);

    renderRoute("/react/dashboard");

    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(await screen.findByText("autopilot")).toBeInTheDocument();
    expect(screen.getByText("PC-001")).toBeInTheDocument();
    expect(screen.getByText("Win11-Template")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Provision" })).toHaveAttribute("href", "/provision");
    expect(screen.queryByRole("button", { name: /kill/i })).not.toBeInTheDocument();
  });

  test("renders the jobs read-only slice from API data", async () => {
    mockFetch({
      "/api/jobs": [
        {
          id: "job-running",
          playbook: "provision.yml",
          status: "running",
          started: "2026-05-18T12:00:00+00:00",
          ended: null,
          duration: "2m 0s",
          args: { vm_name: "PC-001" },
          paused: false
        },
        {
          id: "job-failed",
          playbook: "capture.yml",
          status: "failed",
          started: "2026-05-18T10:00:00+00:00",
          ended: "2026-05-18T10:01:00+00:00",
          duration: "1m 0s",
          args: { serial: "SN-001" },
          paused: false
        }
      ]
    });

    renderRoute("/react/jobs");

    expect(await screen.findByRole("heading", { name: "Jobs" })).toBeInTheDocument();
    expect(await screen.findByText("PC-001")).toBeInTheDocument();
    expect(screen.getByText("SN-001")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "job-running" })).toHaveAttribute("href", "/jobs/job-running");
    await waitFor(() => expect(screen.getByLabelText("Filter jobs")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /resume/i })).not.toBeInTheDocument();
  });

  test("renders the Signals Hub read-only slice from API data", async () => {
    mockFetch(dashboardResponses);

    renderRoute("/react/monitoring");

    expect(await screen.findByRole("heading", { name: "Signals Hub" })).toBeInTheDocument();
    expect((await screen.findAllByText("Runtime containers")).length).toBeGreaterThan(0);
    expect(screen.getByText("Build host agent")).toBeInTheDocument();
    expect(screen.getAllByText("Stage Windows ISO and VirtIO media").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Open server deploy" })).toHaveAttribute("href", "/osdeploy");
    expect(screen.getByRole("link", { name: "Monitoring settings" })).toHaveAttribute(
      "href",
      "/monitoring/settings"
    );
    expect(screen.queryByRole("button", { name: /sweep/i })).not.toBeInTheDocument();
  });
});
