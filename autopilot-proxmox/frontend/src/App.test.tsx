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
    ],
    bubble_topology: {
      workstation_fleets: [
        {
          bubble: {
            id: "bubble-1",
            name: "ACME Lab",
            lifecycle_state: "active",
            domain_name: "lab.gell.one",
            cidr: "10.42.12.0/24",
            dhcp_scope: "10.42.12.0"
          },
          workstation_count: 1,
          running_count: 1,
          stopped_count: 0,
          assets: [
            {
              id: "asset-ws",
              bubble_id: "bubble-1",
              asset_type: "vm",
              asset_role: "workstation",
              vmid: 108,
              membership_state: "active",
              evidence_state: "operator_tagged"
            }
          ],
          vms: [],
          readiness: {
            dc_ready: true,
            dns_ready: true,
            dhcp_ready: true,
            workload_ready: true
          }
        }
      ],
      critical_infrastructure: [
        {
          bubble: { id: "bubble-1", name: "ACME Lab" },
          role: "domain_controller",
          asset: {
            id: "asset-dc",
            bubble_id: "bubble-1",
            asset_type: "vm",
            asset_role: "domain_controller",
            vmid: 130,
            membership_state: "active",
            evidence_state: "ready",
            agent_id: "dc01-agent"
          },
          vm: { vmid: 130, name: "ACME-DC01", status: "running" },
          agent: { agent_id: "dc01-agent", approval_status: "active" }
        }
      ],
      connected_services: [
        {
          id: "svc-ad",
          bubble_id: "bubble-1",
          bubble: { id: "bubble-1", name: "ACME Lab" },
          service_kind: "entra",
          service_name: "Entra ID",
          scope: "external",
          readiness_state: "ready",
          provider_asset_id: "asset-dc",
          consumer_refs: []
        }
      ],
      unassigned_assets: [],
      warnings: [],
      gate_states: [
        {
          bubble_id: "bubble-1",
          workgroup: { state: "allowed", allowed: true, reasons: [] },
          domain_join: { state: "allowed", allowed: true, reasons: [] }
        }
      ]
    }
  },
  "/api/vms/108/detail": {
    vmid: 108,
    pve: {
      vmid: 108,
      name: "WrkGrp-525570B6",
      status: "running",
      node: "pve2",
      checked_at: "2026-05-19T00:00:00Z"
    },
    probe: {
      vmid: 108,
      win_name: "WRKGRP-525570B6",
      serial: "WrkGrp-525570B6",
      os_build: "10.0.26200.8246",
      checked_at: "2026-05-19T00:01:00Z"
    },
    ad_matches: [
      {
        cn: "WRKGRP-525570B6",
        objectSid: "S-1-5-21-1"
      }
    ],
    entra_matches: [
      {
        displayName: "WRKGRP-525570B6",
        trustType: "AzureAD",
        deviceId: "entra-1"
      }
    ],
    intune_matches: [
      {
        deviceName: "WRKGRP-525570B6",
        serialNumber: "WrkGrp-525570B6",
        complianceState: "compliant",
        azureADDeviceId: "entra-1"
      }
    ],
    linkage: [
      {
        label: "SMBIOS.serial -> Intune.serialNumber",
        ok: true,
        value: "WrkGrp-525570B6"
      },
      {
        label: "Windows.Name -> AD.cn",
        ok: true,
        value: "WRKGRP-525570B6"
      }
    ],
    known_credentials: [
      {
        source: "CloudOSD",
        label: "Local admin",
        username: "localadmin",
        password_available: true,
        password_mask: "********",
        vm_name: "WrkGrp-525570B6",
        run_id: "run-1",
        run_url: "/cloudosd/runs/run-1",
        updated_at: "2026-05-18T17:10:00Z",
        note: "Visible workgroup credential from the deployment run."
      }
    ],
    latest_screenshot: {
      vmid: 108,
      image_url: "/api/vms/108/screenshots/latest-image",
      content_type: "image/png",
      captured_at: "2026-05-19T00:02:00Z",
      expires_at: "2026-05-19T00:17:00Z",
      source: "collector",
      bytes: 1200
    },
    timeline: [
      {
        at: "2026-05-19T00:00:00Z",
        source: "pve",
        type: "first-observed",
        severity: "event",
        summary: "VM 108 first observed on pve2: status=running"
      },
      {
        at: "2026-05-19T00:02:00Z",
        source: "screenshot",
        type: "screenshot-captured",
        severity: "event",
        summary: "Screenshot captured by collector"
      }
    ],
    history: {
      pve_snapshots: [],
      device_probes: []
    },
    identity_sync: {
      source: "monitoring_sweep",
      last_checked_at: "2026-05-19T00:01:00Z",
      ad_count: 1,
      entra_count: 1,
      intune_count: 1
    }
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

  test("renders the VMs fleet workspace as a reduced inventory", async () => {
    mockFetch(dashboardResponses);

    renderRoute("/react/vms");

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /^VMs$/ })).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getAllByText("WrkGrp-525570B6").length).toBeGreaterThan(0);
    });
    expect(screen.getByRole("table", { name: "Fleet machines" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "VM Workstation Fleets" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Critical Infrastructure" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Connected Services" })).toBeInTheDocument();
    expect(screen.getAllByText("ACME Lab").length).toBeGreaterThan(0);
    expect(screen.getByText("domain controller")).toBeInTheDocument();
    expect(screen.getByText("Entra ID")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Device Name" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Heartbeat" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Managed By" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Agent" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Bubble" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "WrkGrp-525570B6" })).toHaveAttribute("href", "/react/vms/108");
    expect(screen.getByRole("button", { name: "New bubble" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tag VM 108" })).toBeInTheDocument();
    expect(screen.getByText("ACME Lab / workstation")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "AutopilotAgent" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Screenshot VM 108" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete VM 108" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Console VM 108" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Console VM 108" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Autopilot Devices" })).not.toBeInTheDocument();
  });

  test("tags an existing VM asset into a selected bubble from the React fleet", async () => {
    const fetchMock = mockFetch({
      ...dashboardResponses,
      "/api/bubbles/bubble-1/assets/asset-ws": {
        id: "asset-ws",
        bubble_id: "bubble-1",
        asset_type: "vm",
        asset_role: "domain_controller",
        vmid: 108,
        membership_state: "active"
      }
    });
    vi.spyOn(window, "prompt")
      .mockReturnValueOnce("1")
      .mockReturnValueOnce("domain_controller");

    renderRoute("/react/vms");

    const tagButton = await screen.findByRole("button", { name: "Tag VM 108" });
    fireEvent.click(tagButton);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/bubbles/bubble-1/assets/asset-ws",
        expect.objectContaining({ method: "PATCH" })
      );
    });
    const patchCall = fetchMock.mock.calls.find(([input, init]) => (
      input === "/api/bubbles/bubble-1/assets/asset-ws"
      && init && typeof init !== "function" && "method" in init
    ));
    expect(patchCall).toBeDefined();
    const init = patchCall?.[1] as RequestInit;
    expect(typeof init.body).toBe("string");
    expect(JSON.parse(init.body as string)).toMatchObject({
      asset_role: "domain_controller",
      vmid: 108,
      membership_state: "active"
    });
  });

  test("edits a bubble from the React fleet", async () => {
    const fetchMock = mockFetch({
      ...dashboardResponses,
      "/api/bubbles/bubble-1": {
        id: "bubble-1",
        name: "LAB 3",
        domain_name: "lab3.home.gell.one",
        netbios_name: "LAB3",
        cidr: "192.168.3.0/24",
        gateway_ip: "192.168.3.1",
        dhcp_scope: "192.168.3.0",
        dhcp_pool_start: "192.168.3.100",
        dhcp_pool_end: "192.168.3.199",
        lifecycle_state: "active",
        isolation_status: "ready"
      }
    });
    vi.spyOn(window, "prompt")
      .mockReturnValueOnce("LAB 3")
      .mockReturnValueOnce("lab3.home.gell.one")
      .mockReturnValueOnce("LAB3")
      .mockReturnValueOnce("192.168.3.0/24")
      .mockReturnValueOnce("192.168.3.1")
      .mockReturnValueOnce("192.168.3.0")
      .mockReturnValueOnce("192.168.3.100")
      .mockReturnValueOnce("192.168.3.199")
      .mockReturnValueOnce("active")
      .mockReturnValueOnce("ready");

    renderRoute("/react/vms");

    fireEvent.click(await screen.findByRole("button", { name: "Edit bubble ACME Lab" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/bubbles/bubble-1",
        expect.objectContaining({ method: "PATCH" })
      );
    });
    const patchCall = fetchMock.mock.calls.find(([input, init]) => (
      input === "/api/bubbles/bubble-1"
      && init && typeof init !== "function" && "method" in init
    ));
    expect(patchCall).toBeDefined();
    const init = patchCall?.[1] as RequestInit;
    expect(typeof init.body).toBe("string");
    expect(JSON.parse(init.body as string)).toMatchObject({
      name: "LAB 3",
      domain_name: "lab3.home.gell.one",
      netbios_name: "LAB3",
      cidr: "192.168.3.0/24",
      gateway_ip: "192.168.3.1",
      dhcp_scope: "192.168.3.0",
      dhcp_pool_start: "192.168.3.100",
      dhcp_pool_end: "192.168.3.199",
      lifecycle_state: "active",
      isolation_status: "ready"
    });
  });

  test("deletes a bubble from the React fleet after typed confirmation", async () => {
    const fetchMock = mockFetch({
      ...dashboardResponses,
      "/api/bubbles/bubble-1": { ok: true }
    });
    vi.spyOn(window, "prompt").mockReturnValueOnce("ACME Lab");

    renderRoute("/react/vms");

    fireEvent.click(await screen.findByRole("button", { name: "Delete bubble ACME Lab" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/bubbles/bubble-1",
        expect.objectContaining({ method: "DELETE" })
      );
    });
  });

  test("opens VM console and screenshot actions inside a VM detail page", async () => {
    mockFetch(dashboardResponses);

    renderRoute("/react/vms/108");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Console VM 108" })).toBeInTheDocument();
    });
    expect(screen.getAllByRole("heading", { name: "WrkGrp-525570B6" }).length).toBeGreaterThan(0);
    expect(screen.getByText("Agent ID")).toBeInTheDocument();
    expect(screen.getByText("agent-wrkgrp-525570b6")).toBeInTheDocument();
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

  test("renders VM evidence hub panels from detail API without revealing passwords", async () => {
    mockFetch(dashboardResponses);

    renderRoute("/react/vms/108");

    expect(await screen.findByRole("heading", { name: "Identity linkage" })).toBeInTheDocument();
    expect(screen.getByText("SMBIOS.serial -> Intune.serialNumber")).toBeInTheDocument();
    expect(screen.getByText("Windows.Name -> AD.cn")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Known credentials" })).toBeInTheDocument();
    expect(screen.getByText("localadmin")).toBeInTheDocument();
    expect(screen.getByText("********")).toBeInTheDocument();
    expect(screen.queryByText("Mep7!Qav2")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Latest screenshot" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Latest VM 108 screenshot" })).toHaveAttribute("src", "/api/vms/108/screenshots/latest-image");
    expect(screen.getByRole("link", { name: "Open screenshot" })).toHaveAttribute("href", "/api/vms/108/screenshots/latest-image");
    expect(screen.getByRole("heading", { name: "Timeline" })).toBeInTheDocument();
    expect(screen.getByText("Screenshot captured by collector")).toBeInTheDocument();
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
    expect(screen.queryByText("2026-05-18T12:00:00Z")).not.toBeInTheDocument();
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
    expect(screen.queryByText(/May 19 00:00Z/u)).not.toBeInTheDocument();
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
