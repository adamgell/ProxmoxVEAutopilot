import { describe, expect, test } from "vitest";

import type { JobTableRow, MonitoringOverview } from "./contracts";
import {
  buildFleetMachineRows,
  buildSignalMetrics,
  fallbackText,
  fleetAgentLabel,
  fleetManagedByLabel,
  fleetOsName,
  fleetOsVersion,
  fleetRuntimeLabel,
  formatPercent,
  formatRelativeAge,
  formatShortDateTime,
  jobMatchesStatus,
  jobTarget,
  jobStatusFilters,
  monitoringStrip,
  rankedSignalPaths,
  statusLabel,
  statusTone,
  summarizeFleet,
  summarizeJobs,
  vmJoinLabels
} from "./viewModels";

describe("operator view models", () => {
  test("formats empty and optional values tightly", () => {
    expect(fallbackText(null)).toBe("-");
    expect(fallbackText("")).toBe("-");
    expect(fallbackText(" controller ")).toBe(" controller ");
    expect(formatPercent(72)).toBe("72%");
    expect(formatPercent(undefined)).toBe("-");
    expect(formatShortDateTime("2026-05-19T00:05:00Z", "America/New_York")).toBe("May 18, 8:05 PM EDT");
    expect(formatShortDateTime("2026-05-19T00:05:00Z", "UTC")).toBe("May 19, 12:05 AM UTC");
    expect(formatShortDateTime("2026-05-19T00:05:00Z", "America/New_York")).not.toContain("Z");
    expect(formatShortDateTime("not-a-date")).toBe("not-a-date");
    expect(formatRelativeAge("2026-05-19T00:00:30Z", Date.parse("2026-05-19T00:01:00Z"))).toBe("last 30s");
    expect(formatRelativeAge("2026-05-19T00:00:00Z", Date.parse("2026-05-19T00:09:00Z"))).toBe("last 9m");
    expect(formatRelativeAge("not-a-date", Date.parse("2026-05-19T00:09:00Z"))).toBe("not-a-date");
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

  test("filters jobs by operator status buckets", () => {
    const running: JobTableRow = { id: "running", status: "running" };
    const pending: JobTableRow = { id: "pending", status: "pending" };
    const orphaned: JobTableRow = { id: "orphaned", status: "orphaned" };
    const paused: JobTableRow = { id: "paused", status: "running", paused: true };

    expect(jobStatusFilters).toEqual(["all", "failed", "running", "queued", "complete", "paused"]);
    expect(jobMatchesStatus(running, "running")).toBe(true);
    expect(jobMatchesStatus(pending, "queued")).toBe(true);
    expect(jobMatchesStatus(orphaned, "failed")).toBe(true);
    expect(jobMatchesStatus(paused, "paused")).toBe(true);
    expect(jobMatchesStatus(paused, "running")).toBe(false);
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

  test("builds signal metrics from collected hub values", () => {
    expect(
      buildSignalMetrics({
        metrics: [],
        source_health: { runtime_available: true, setup_health: "ready" },
        signals: [
          { id: "runtime", family: "runtime", label: "Runtime", status: "healthy", tone: "good", summary: "5 up" },
          { id: "media", family: "deploy_readiness", label: "Media", status: "blocked", tone: "bad", summary: "missing" }
        ],
        operator_paths: [],
        generated_at: "2026-05-19T00:00:00Z",
        build: { sha_short: "75ea47a", build_time: "2026-05-19T00:10:41Z" },
        lifecycle_lanes: [],
        deployment_health: {
          summary: { total: 0, failed: 0 },
          active: [],
          recent_completions: [],
          bottlenecks: []
        },
        services: [],
        runtime: { available: true, error: "", containers: [] },
        fleet_attention: []
      })
    ).toEqual([
      { label: "Critical", value: "1", tone: "bad" },
      { label: "Needs operator", value: "1", tone: "bad" },
      { label: "Ready", value: "1", tone: "good" },
      { label: "Runtime", value: "up", tone: "good" },
      { label: "Setup", value: "ready", tone: "good" }
    ]);
  });

  test("ranks signal paths by priority without mutating input", () => {
    const paths = [
      { id: "watch", priority: 30, label: "Watch build", status: "ready", tone: "good", summary: "safe", action_label: "Watch", href: "/react/jobs" },
      { id: "media", priority: 5, label: "Stage media", status: "blocked", tone: "bad", summary: "missing", action_label: "Open", href: "/setup" }
    ] as const;

    expect(rankedSignalPaths(paths).map((path) => path.id)).toEqual(["media", "watch"]);
    expect(paths.map((path) => path.id)).toEqual(["watch", "media"]);
  });

  test("maps normalized lifecycle states to fleet chips", () => {
    expect(vmJoinLabels({
      vmid: 105,
      name: "WrkGrp-8F47E090",
      status: "running",
      lifecycle_state: "workgroup_unenrolled",
      lifecycle_label: "unenrolled"
    })).toEqual(["unenrolled"]);
    expect(vmJoinLabels({
      vmid: 106,
      name: "Domain-106",
      lifecycle_state: "ad_domain_joined",
      lifecycle_label: "domain",
      lifecycle_domain_joined: true
    })).toEqual(["domain"]);
    expect(vmJoinLabels({
      vmid: 107,
      name: "Entra-107",
      lifecycle_state: "entra_joined",
      lifecycle_label: "Entra ID",
      lifecycle_entra_joined: true
    })).toEqual(["Entra ID"]);
    expect(vmJoinLabels({
      vmid: 108,
      name: "Intune-108",
      lifecycle_state: "intune_enrolled",
      lifecycle_label: "Intune",
      lifecycle_domain_joined: true,
      lifecycle_entra_joined: true,
      lifecycle_intune_enrolled: true,
      lifecycle_autopilot_registered: true
    })).toEqual(["domain", "Entra ID", "Intune", "Autopilot ID"]);
  });

  test("counts workgroup lifecycle machines as fleet attention", () => {
    expect(summarizeFleet({
      vms: [
        {
          vmid: 105,
          name: "WrkGrp-8F47E090",
          status: "running",
          lifecycle_state: "workgroup_unenrolled",
          lifecycle_label: "unenrolled"
        },
        {
          vmid: 106,
          name: "Domain-106",
          status: "running",
          lifecycle_state: "ad_domain_joined",
          lifecycle_label: "domain",
          lifecycle_domain_joined: true
        }
      ],
      missing_vms: [],
      agents: [],
      autopilot_devices: [],
      ap_error: "",
      cache_refreshing: false,
      generated_at: "2026-05-19T00:00:00Z"
    })).toMatchObject({
      total: 2,
      running: 2,
      attention: 1
    });
  });

  test("counts full Proxmox inventory when the fleet API provides it", () => {
    expect(summarizeFleet({
      vms: [
        { vmid: 105, name: "WrkGrp-8F47E090", status: "running" }
      ],
      proxmox_vms: [
        { vmid: 100, name: "autopilot-buildhost-01", status: "running" },
        { vmid: 400, name: "Dev1", status: "stopped" },
        { vmid: 9109, name: "FS01", status: "running" }
      ],
      missing_vms: [],
      agents: [],
      autopilot_devices: [],
      ap_error: "",
      cache_refreshing: false,
      generated_at: "2026-05-19T00:00:00Z"
    })).toMatchObject({
      total: 3,
      running: 2
    });
  });

  test("merges VM, agent, and Autopilot device evidence into one machine row", () => {
    const rows = buildFleetMachineRows({
      vms: [
        {
          vmid: 108,
          name: "WrkGrp-525570B6",
          hostname: "WRKGRP-525570B6",
          serial: "WrkGrp-525570B6",
          status: "running",
          ip_address: "192.168.2.49",
          os_caption: "Microsoft Windows 11 Pro",
          os_build: "10.0.26200.8246",
          lifecycle_state: "workgroup_unenrolled",
          lifecycle_label: "unenrolled"
        }
      ],
      missing_vms: [],
      agents: [
        {
          agent_id: "agent-wrkgrp-525570b6",
          vmid: 108,
          computer_name: "WRKGRP-525570B6",
          serial_number: "WrkGrp-525570B6",
          primary_ipv4: "192.168.2.49",
          domain_joined: false,
          entra_joined: false,
          current_phase: "cloudosd",
          last_heartbeat_at: "2026-05-19T00:00:00Z",
          agent_version: "0.1.2"
        }
      ],
      autopilot_devices: [
        {
          id: "device-1",
          serial: "WrkGrp-525570B6",
          display_name: "WRKGRP-525570B6",
          profile_status: "assigned",
          enrollment_state: "enrolled",
          has_local_hash: true
        }
      ],
      ap_error: "",
      cache_refreshing: false,
      generated_at: "2026-05-19T00:00:00Z"
    });

    expect(rows).toHaveLength(1);
    const row = rows[0];
    if (!row) {
      throw new Error("expected merged fleet row");
    }
    expect(row).toMatchObject({
      id: "vm-108",
      name: "WrkGrp-525570B6",
      vmid: 108,
      agentId: "agent-wrkgrp-525570b6",
      phase: "cloudosd",
      mdmEnrollment: "enrolled",
      method: "agent + monitor",
      version: "0.1.2"
    });
    expect(row.lifecycleLabels).toEqual(["unenrolled", "Intune", "Autopilot ID", "hash"]);
    expect(fleetManagedByLabel(row)).toBe("Intune");
    expect(fleetOsName(row)).toBe("Windows");
    expect(fleetOsVersion(row)).toBe("10.0.26200.8246");
    expect(fleetRuntimeLabel(row)).toBe("running");
    expect(fleetAgentLabel(row)).toBe("Stale");
  });

  test("keeps duplicate agent rows for one live VM from creating duplicate machine rows", () => {
    const rows = buildFleetMachineRows({
      vms: [
        {
          vmid: 111,
          name: "LABZ1-DC01",
          hostname: "LABZ1-DC01",
          serial: "LABZ1-DC01",
          status: "running",
          ip_address: "192.168.16.10",
          os_build: "20348"
        }
      ],
      proxmox_vms: [
        {
          vmid: 111,
          name: "LABZ1-DC01",
          status: "running",
          ip_address: "192.168.16.10"
        }
      ],
      missing_vms: [],
      agents: [
        {
          agent_id: "agent-labz1-stale",
          approval_status: "active",
          pairing_status: "paired",
          vmid: 111,
          computer_name: "LABZ1-DC01",
          primary_ipv4: "192.168.16.10",
          last_seen_at: "2026-06-23T19:00:00+00:00"
        },
        {
          agent_id: "agent-labz1-current",
          approval_status: "active",
          pairing_status: "paired",
          vmid: 111,
          computer_name: "LABZ1-DC01",
          primary_ipv4: "192.168.16.10",
          qga_state: "Running",
          agent_version: "osd-client-0.1.0",
          last_heartbeat_at: "2026-06-23T20:00:00+00:00"
        }
      ],
      autopilot_devices: [],
      agent_identity_warnings: [
        "Multiple agent rows map to VM 111 (LABZ1-DC01): agent-labz1-stale, agent-labz1-current. Cached rows were left intact."
      ],
      ap_error: "",
      cache_refreshing: false,
      generated_at: "2026-06-23T20:05:00Z"
    });

    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      id: "vm-111",
      name: "LABZ1-DC01",
      vmid: 111,
      agentId: "agent-labz1-current",
      qga: "Running",
      method: "agent + monitor"
    });
  });

  test("includes unmatched agents as machine rows", () => {
    const rows = buildFleetMachineRows({
      vms: [],
      missing_vms: [],
      agents: [
        {
          agent_id: "agent-only",
          computer_name: "AGENT-ONLY",
          serial_number: "SERIAL-ONLY",
          approval_status: "pending",
          lifecycle_state: "entra_joined",
          lifecycle_label: "Entra ID",
          lifecycle_entra_joined: true
        }
      ],
      autopilot_devices: [],
      ap_error: "",
      cache_refreshing: false,
      generated_at: "2026-05-19T00:00:00Z"
    });

    expect(rows).toHaveLength(1);
    const row = rows[0];
    expect(row).toBeDefined();
    if (!row) {
      throw new Error("expected one fleet row");
    }
    expect(row).toMatchObject({
      id: "agent-agent-only",
      name: "AGENT-ONLY",
      agentId: "agent-only",
      method: "agent"
    });
    expect(fleetAgentLabel(row)).toBe("Pending");
  });

  test("fleetAgentLabel surfaces approved agents waiting for pairing", () => {
    const row = buildFleetMachineRows({
      vms: [],
      missing_vms: [],
      autopilot_devices: [],
      agents: [{
        agent_id: "agent-dc3",
        approval_status: "approved",
        pairing_status: "waiting_for_claim",
        needs_pairing: true,
        computer_name: "DC3"
      }],
      ap_error: "",
      cache_refreshing: false,
      generated_at: "2026-05-20T00:00:00Z"
    })[0];

    expect(row).toBeDefined();
    if (!row) {
      throw new Error("expected approved fleet row");
    }
    expect(fleetAgentLabel(row)).toBe("Approved");
  });

  test("fleetAgentLabel surfaces upgrade availability before plain version", () => {
    const row = buildFleetMachineRows({
      vms: [{
        vmid: 110,
        name: "DC3",
        status: "running"
      }],
      missing_vms: [],
      autopilot_devices: [],
      agents: [{
        agent_id: "agent-vm-110",
        approval_status: "active",
        pairing_status: "paired",
        vmid: 110,
        agent_version: "0.1.2",
        published_agent_version: "0.1.3",
        update_status: "upgrade_available",
        upgrade_available: true,
        last_heartbeat_at: new Date().toISOString()
      }],
      ap_error: "",
      cache_refreshing: false,
      generated_at: "2026-05-20T00:00:00Z"
    })[0];

    expect(row).toBeDefined();
    if (!row) {
      throw new Error("expected upgrade fleet row");
    }
    expect(fleetAgentLabel(row)).toBe("Upgrade available");
  });
});
