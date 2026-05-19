import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    ],
    lifecycle_lanes: [
      {
        id: "provisioned",
        label: "Provisioned",
        value: "2/3",
        detail: "Running in Proxmox and visible to the monitor.",
        status: "attention",
        tone: "active"
      }
    ],
    deployment_health: {
      summary: {
        total: 4,
        active: 1,
        running: 1,
        completed: 2,
        succeeded: 2,
        failed: 1,
        stuck: 0,
        regressed: 1,
        slow: 0,
        median_completion_seconds: 300,
        p95_completion_seconds: 900,
        recent_failure_rate: 0.25
      },
      active: [
        {
          deployment_key: "osdeploy/run-1",
          deployment_type: "osdeploy",
          current_phase: "windows_setup",
          elapsed_seconds: 120,
          health: "running",
          state: "running",
          next_expected_evidence: "agent heartbeat"
        }
      ],
      recent_completions: [],
      bottlenecks: [
        {
          deployment_type: "osdeploy",
          phase_key: "windows_setup",
          phase_label: "Windows setup",
          count: 1,
          health: "regressed",
          p95_seconds: 900
        }
      ]
    },
    services: [
      {
        service_id: "autopilot-monitor",
        status: "ok",
        age_seconds: 12,
        detail: "sweep idle"
      }
    ],
    runtime: {
      available: true,
      error: "",
      containers: [
        {
          name: "autopilot",
          service: "autopilot",
          image: "proxmox-autopilot:latest",
          status: "running",
          health: "healthy"
        }
      ]
    },
    fleet_attention: [
      {
        vmid: 101,
        vm_name: "WIN-SRV-01",
        node: "pve1",
        lifecycle: "Needs check",
        tone: "bad",
        pve_status: "running",
        windows: "WIN-SRV-01",
        serial: "SER-101",
        ad: "ok",
        entra: "missing",
        intune: "missing",
        last_checked: "2026-05-19T00:00:00Z",
        href: "/devices/101"
      }
    ]
  },
  "/api/monitoring/service-logs?tail=180&container=autopilot": {
    container: "autopilot",
    service: "autopilot",
    tail: 180,
    lines: ["2026-05-19T00:00:00Z autopilot ready"]
  },
  "/api/version": {
    sha_short: "abc1234",
    build_time: "2026-05-18T12:00:00Z"
  },
  "/api/vms/fleet": {
    generated_at: "2026-05-19T00:00:00Z",
    cache_age_seconds: 12,
    cache_refreshing: false,
    monitor_sweep: { running: false, vm_count: 1 },
    ap_error: "",
    vms: [
      {
        vmid: 108,
        name: "WrkGrp-525570B6",
        hostname: "WRKGRP-525570B6",
        serial: "WrkGrp-525570B6",
        status: "running",
        ip_address: "192.168.2.49",
        in_autopilot: true,
        in_intune: false,
        aad_joined: true,
        part_of_domain: false,
        has_hash: true,
        target_os: "windows"
      }
    ],
    missing_vms: [],
    agents: [
      {
        agent_id: "agent-wrkgrp-525570b6",
        approval_status: "active",
        vmid: 108,
        computer_name: "WRKGRP-525570B6",
        primary_ipv4: "192.168.2.49",
        qga_state: "Running",
        current_phase: "cloudosd",
        last_heartbeat_at: "2026-05-19T00:00:00Z",
        hash_capture_supported: true
      }
    ],
    autopilot_devices: [
      {
        id: "device-1",
        serial: "WrkGrp-525570B6",
        display_name: "WRKGRP-525570B6",
        group_tag: "Lab",
        profile_status: "assigned",
        profile_ok: true,
        enrollment_state: "enrolled",
        has_local_hash: true
      }
    ]
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
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#react-content");
    expect(screen.getAllByRole("link", { name: "Signals Hub" })[0]).toHaveAttribute("href", "/react/monitoring");
    expect(screen.getAllByRole("link", { name: "OSDCloud Desktop legacy page" })[0]).toHaveAttribute("href", "/cloudosd");
    expect(screen.getAllByRole("heading", { name: "Deploy" }).length).toBeGreaterThan(1);
    expect(screen.getByText("Choose the deployment path, then open the guarded execution page.")).toBeInTheDocument();
    expect(screen.getAllByText("Jinja").length).toBeGreaterThan(0);
    expect(screen.getByText("Build abc1234")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /provision/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^clone$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /winpe/i })).not.toBeInTheDocument();
  });

  test("renders the VMs fleet workspace with full-control affordances", async () => {
    mockFetch(dashboardResponses);

    renderRoute("/react/vms");

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /^VMs$/ })).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getAllByText("WrkGrp-525570B6").length).toBeGreaterThan(0);
    });
    expect(screen.getByText("agent-wrkgrp-525570b6")).toBeInTheDocument();
    expect(screen.getAllByText("WRKGRP-525570B6").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Screenshot VM 108" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete VM 108" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Console VM 108" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Console VM 108" })).not.toBeInTheDocument();
  });

  test("opens VM console and screenshot actions inside the Fleet workspace", async () => {
    mockFetch(dashboardResponses);

    renderRoute("/react/vms");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Console VM 108" })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "Console VM 108" }));
    expect(screen.getByRole("region", { name: "VM action workspace" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "VM 108 action" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open legacy console" })).toHaveAttribute("href", "/vms/108/console");
    fireEvent.click(screen.getByRole("button", { name: "Expand console" }));
    expect(screen.getByRole("region", { name: "VM action workspace" })).toHaveClass("vm-action-workspace--expanded");
    fireEvent.click(screen.getByRole("button", { name: "Minimize action" }));
    expect(screen.getByRole("region", { name: "VM action workspace" })).toHaveClass("vm-action-workspace--minimized");
    expect(screen.getByRole("button", { name: "Restore action" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore action" }));
    expect(screen.getByRole("region", { name: "VM action workspace" })).not.toHaveClass("vm-action-workspace--minimized");

    fireEvent.click(screen.getByRole("button", { name: "Screenshot VM 108" }));
    expect(screen.getByRole("heading", { name: "Screenshot" })).toBeInTheDocument();
    expect(screen.getAllByText("Live WebSocket is not connected").length).toBeGreaterThan(0);
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
    expect(screen.getByText("2 jobs")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "job-running" })).toHaveAttribute("href", "/jobs/job-running");
    fireEvent.click(screen.getByRole("button", { name: "failed" }));
    expect(screen.getByText("1 of 2 jobs")).toBeInTheDocument();
    expect(screen.queryByText("PC-001")).not.toBeInTheDocument();
    expect(screen.getByText("SN-001")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Filter jobs")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /resume/i })).not.toBeInTheDocument();
  });

  test("renders the Signals Hub read-only slice from API data", async () => {
    mockFetch(dashboardResponses);

    renderRoute("/react/monitoring");

    expect(screen.getByRole("progressbar", { name: "Signals loading" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Signals Hub" })).toBeInTheDocument();
    expect((await screen.findAllByText("Runtime containers")).length).toBeGreaterThan(0);
    await waitFor(() => {
      expect(screen.queryByRole("progressbar", { name: "Signals loading" })).not.toBeInTheDocument();
    });
    expect(screen.getByText("Build host agent")).toBeInTheDocument();
    expect(screen.getAllByText("Stage Windows ISO and VirtIO media").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Open server deploy" })).toHaveAttribute("href", "/osdeploy");
    expect(screen.getByRole("link", { name: "Monitoring settings" })).toHaveAttribute(
      "href",
      "/monitoring/settings"
    );
    expect(screen.getByText("May 19 00:00Z")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Deployment speed" })).toBeInTheDocument();
    expect(screen.getByText("Windows setup")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Lifecycle lanes" })).toBeInTheDocument();
    expect(screen.getByText("Provisioned")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Service health" })).toBeInTheDocument();
    expect(screen.getByText("autopilot-monitor")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Runtime containers" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Tail" }));
    expect(await screen.findByText("2026-05-19T00:00:00Z autopilot ready")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Fleet attention" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Inspect" })).toHaveAttribute("href", "/devices/101");
    expect(screen.queryByRole("button", { name: /sweep/i })).not.toBeInTheDocument();
  });
});
