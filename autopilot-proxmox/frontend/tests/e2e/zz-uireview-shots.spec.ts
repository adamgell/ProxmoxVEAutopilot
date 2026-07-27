// TEMPORARY - UI review screenshot capture. Delete after use.
import { test, type Page } from "@playwright/test";

const SHOTS = "/private/tmp/claude-502/-Users-Adam-Gell-repo-ProxmoxVEAutopilot/570b0dcc-b611-45df-a913-0f0e27549323/scratchpad/shots";

const NODES = ["pve1", "pve2"];
const PHASES = ["cloudosd", "domain_join", "autopilot_enroll", "idle", "hash_capture"];

function makeVm(i: number) {
  const vmid = 200 + i;
  const serial = `Gell-${String(1000 + i * 37)}A${i}`;
  const running = i % 5 !== 3;
  return {
    vmid,
    name: `ring0ivy24-${String(i).padStart(2, "0")}`,
    hostname: `RING0IVY24-${String(i).padStart(2, "0")}`,
    serial,
    status: running ? "running" : "stopped",
    ip_address: running ? `192.168.16.${String(40 + i)}` : "",
    os_caption: "Microsoft Windows 11 Enterprise",
    os_build: "26100.2033",
    in_autopilot: i % 3 !== 0,
    in_intune: i % 4 === 0,
    aad_joined: i % 3 !== 1,
    part_of_domain: i % 2 === 0,
    hybrid_joined: i % 6 === 0,
    entra_id_joined: i % 3 !== 1,
    has_hash: i % 3 !== 2,
    lifecycle_state: ["provisioned", "domain_joined", "enrolled", "failed", "deploying"][i % 5],
    lifecycle_label: ["Provisioned", "Domain joined", "Enrolled", "Failed", "Deploying"][i % 5],
    lifecycle_source: "agent",
    lifecycle_observed_at: "2026-07-27T11:58:00Z",
    lifecycle_domain_joined: i % 2 === 0,
    lifecycle_entra_joined: i % 3 !== 1,
    lifecycle_intune_enrolled: i % 4 === 0,
    lifecycle_autopilot_registered: i % 3 !== 0,
    target_os: "windows",
    sequence_name: i % 2 === 0 ? "CloudOSD Win11 24H2 Enterprise" : null,
    monitor_checked_at: "2026-07-27T12:41:03Z",
    monitor_probed_at: "2026-07-27T12:40:11Z",
    hostname_join_label: i % 2 === 0 ? "home.gell.one" : "WORKGROUP",
    hostname_join_title: i % 2 === 0 ? "Joined to home.gell.one" : "Not domain joined",
    node: NODES[i % 2],
    qga: running ? "running" : "stopped"
  };
}

function makeAgent(i: number) {
  const vmid = 200 + i;
  return {
    agent_id: `agent-ring0ivy24-${String(i).padStart(2, "0")}`,
    approval_id: `appr-${String(i)}`,
    approval_status: i % 7 === 5 ? "pending" : "active",
    pairing_status: i % 7 === 5 ? "waiting_for_approval" : "paired",
    needs_pairing: i % 7 === 5,
    update_status: i % 5 === 2 ? "upgrade_available" : "current",
    upgrade_available: i % 5 === 2,
    published_agent_version: "0.1.4",
    agent_version: i % 5 === 2 ? "0.1.3" : "0.1.4",
    vmid,
    computer_name: `RING0IVY24-${String(i).padStart(2, "0")}`,
    serial_number: `Gell-${String(1000 + i * 37)}A${i}`,
    primary_ipv4: `192.168.16.${String(40 + i)}`,
    os_name: "Windows 11 Enterprise",
    os_build: "26100.2033",
    qga_state: "Running",
    domain_joined: i % 2 === 0,
    entra_joined: i % 3 !== 1,
    lifecycle_state: ["provisioned", "domain_joined", "enrolled", "failed", "deploying"][i % 5],
    lifecycle_label: ["Provisioned", "Domain joined", "Enrolled", "Failed", "Deploying"][i % 5],
    current_phase: PHASES[i % PHASES.length],
    current_run_id: `run-${String(9000 + i)}`,
    hash_capture_supported: true,
    last_heartbeat_at: "2026-07-27T12:42:10Z",
    last_seen_at: "2026-07-27T12:42:10Z"
  };
}

const VMS = Array.from({ length: 16 }, (_, i) => makeVm(i + 1));
const AGENTS = Array.from({ length: 14 }, (_, i) => makeAgent(i + 1));

const DEVICES = VMS.slice(0, 10).map((vm, i) => ({
  id: `device-${String(i)}`,
  serial: vm.serial,
  display_name: vm.hostname,
  group_tag: i % 2 === 0 ? "Lab-Ivy" : "Pilot",
  profile_status: i % 3 === 0 ? "assigned" : "unassigned",
  profile_ok: i % 3 === 0,
  enrollment_state: i % 4 === 0 ? "enrolled" : "not enrolled",
  manufacturer: "QEMU",
  has_local_hash: i % 3 !== 2
}));

const BUBBLE = {
  id: "labz1",
  name: "LABZ1",
  slug: "labz1",
  lifecycle_state: "active",
  domain_name: "test.gell.one",
  netbios_name: "LABZ1",
  cidr: "192.168.16.0/24",
  gateway_ip: "192.168.16.1",
  planned_bridge: "vmbr0",
  planned_vlan: 16,
  isolation_status: "isolated",
  dhcp_scope: "192.168.16.40-192.168.16.200",
  dhcp_pool_start: "192.168.16.40",
  dhcp_pool_end: "192.168.16.200",
  dhcp_owner_asset_id: "asset-dc",
  dc_ready: true,
  dns_ready: false,
  dhcp_ready: true,
  workload_ready: false
};

const TOPOLOGY = {
  workstation_fleets: [
    {
      bubble: BUBBLE,
      running_count: 12,
      stopped_count: 4,
      workstation_count: 8,
      assets: VMS.slice(0, 8).map((vm, i) => ({ id: `asset-${String(i)}`, bubble_id: "labz1", asset_type: "vm", asset_role: "workstation", vmid: vm.vmid, agent_id: `agent-ring0ivy24-${String(i + 1).padStart(2, "0")}`, membership_state: "joined", evidence_state: "verified" })),
      vms: VMS.slice(0, 8),
      readiness: { network: true, dhcp: true, snat: true, dns: false }
    }
  ],
  critical_infrastructure: [
    {
      bubble: BUBBLE,
      asset: { id: "asset-dc", bubble_id: "labz1", asset_type: "vm", asset_role: "domain_controller", vmid: 190, agent_id: "agent-labz1-dc01", membership_state: "joined", evidence_state: "verified" },
      role: "domain_controller",
      vm: { vmid: 190, name: "labz1-dc01", status: "running", ip_address: "192.168.16.10" },
      agent: null
    },
    {
      bubble: BUBBLE,
      asset: { id: "asset-fs", bubble_id: "labz1", asset_type: "vm", asset_role: "file_server", vmid: 191, agent_id: "agent-labz1-fs01", membership_state: "joined", evidence_state: "pending" },
      role: "file_server",
      vm: { vmid: 191, name: "labz1-fs01", status: "running", ip_address: "192.168.16.11" },
      agent: null
    }
  ],
  connected_services: [
    { id: "svc-1", bubble_id: "labz1", bubble: BUBBLE, service_kind: "directory", service_name: "AD DS", scope: "bubble", provider_asset_id: "asset-dc", readiness_state: "ready", evidence_summary: { credential_ids: [1] } },
    { id: "svc-2", bubble_id: "labz1", bubble: BUBBLE, service_kind: "pxe", service_name: "WDS", scope: "bubble", provider_asset_id: "asset-fs", readiness_state: "degraded", evidence_summary: { credential_ids: [] } }
  ],
  unassigned_assets: VMS.slice(8, 12),
  warnings: ["labz1: DNS forwarder not reachable from 192.168.16.0/24"],
  gate_states: [
    { bubble_id: "labz1", workgroup: { ok: true, checked_at: "2026-07-27T12:30:00Z" }, domain_join: { ok: false, reason: "2 of 8 pending" } }
  ]
};

const FLEET = {
  generated_at: "2026-07-27T12:42:31Z",
  cache_age_seconds: 14,
  cache_fetched_at_iso: "2026-07-27T12:42:17Z",
  cache_refreshing: false,
  monitor_sweep: { running: true, vm_count: 16, started_at: "2026-07-27T12:41:00Z" },
  ap_error: "",
  vms: VMS,
  proxmox_vms: VMS,
  missing_vms: [{ vmid: 178, name: "ring0ivy23-04", serial: "Gell-880C4", status: "absent" }],
  agents: AGENTS,
  agent_identity_warnings: ["agent-ring0ivy24-06 reports a serial that does not match VM 206"],
  autopilot_devices: DEVICES,
  bubble_topology: TOPOLOGY
};

const DETAIL = {
  vmid: 201,
  fleet_vm: VMS[0],
  pve: {
    node: "pve2", status: "running", cores: 4, maxmem: 8589934592, maxdisk: 103079215104,
    uptime: 48213, cpu: 0.031, mem: 4102103040, name: "ring0ivy24-01", template: 0,
    netin: 918273645, netout: 182736450, diskread: 8172635, diskwrite: 91827364
  },
  probe: {
    hostname: "RING0IVY24-01", os_caption: "Microsoft Windows 11 Enterprise", os_build: "26100.2033",
    domain: "home.gell.one", part_of_domain: true, aad_joined: true, in_intune: false,
    serial: "Gell-1037A1", manufacturer: "QEMU", model: "Standard PC (Q35 + ICH9, 2009)",
    probed_at: "2026-07-27T12:40:11Z", qga: "running", agent_version: "0.1.4"
  },
  ad_matches: [{ cn: "RING0IVY24-01", distinguishedName: "CN=RING0IVY24-01,OU=Lab,DC=home,DC=gell,DC=one", objectGUID: "11112222-3333-4444-5555-666677778888" }],
  entra_matches: [{ displayName: "RING0IVY24-01", deviceId: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", trustType: "AzureAd" }],
  intune_matches: [],
  linkage: [
    { label: "Serial in Autopilot", ok: true, value: "Gell-1037A1" },
    { label: "Hostname in AD", ok: true, value: "RING0IVY24-01 / home.gell.one" },
    { label: "Entra device object", ok: true, value: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" },
    { label: "Intune enrollment", ok: false, value: "No managed device for this serial" },
    { label: "Agent heartbeat", ok: true, value: "12s ago" }
  ],
  known_credentials: [
    { source: "domain_join", label: "home.gell.one join account", username: "HOME\\svc-join", password_available: true, password_mask: "********", vm_name: "ring0ivy24-01", run_id: "run-9001", run_url: "/react/runs/run-9001", updated_at: "2026-07-27T12:22:14Z", note: "Used by the domain join executor" },
    { source: "local_admin", label: "Built-in administrator", username: ".\\Administrator", password_available: true, password_mask: "********", vm_name: "ring0ivy24-01", run_id: "run-9001", run_url: "/react/runs/run-9001", updated_at: "2026-07-27T12:05:41Z", note: "" }
  ],
  latest_screenshot: null,
  screenshot_history: [],
  timeline: [
    { at: "2026-07-27T12:41:00Z", source: "monitor", type: "probe", severity: "info", summary: "Monitor sweep probed VM 201" },
    { at: "2026-07-27T12:22:14Z", source: "run", type: "phase_complete", severity: "info", summary: "Run run-9001 completed: domain_join" },
    { at: "2026-07-27T12:18:02Z", source: "agent", type: "heartbeat", severity: "warning", summary: "Agent agent-ring0ivy24-01 heartbeat resumed after 4m gap" },
    { at: "2026-07-27T12:05:41Z", source: "run", type: "phase_start", severity: "info", summary: "Run run-9001 started: CloudOSD Win11 24H2 Enterprise" },
    { at: "2026-07-27T11:58:00Z", source: "provision", type: "clone", severity: "info", summary: "VM 201 cloned from template 250" }
  ],
  history: { runs: 3, last_run_id: "run-9001", last_run_status: "completed", first_seen: "2026-07-27T11:58:00Z" },
  identity_sync: { source: "monitor", last_checked_at: "2026-07-27T12:30:00Z", ad_count: 1, entra_count: 1, intune_count: 0 }
};

async function mockFleet(page: Page) {
  // Playwright matches the most recently registered route first, so the
  // catch-all has to be registered before the specific handlers.
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/vms/fleet", (r) => r.fulfill({ json: FLEET }));
  await page.route("**/api/vms/*/detail", (r) => r.fulfill({ json: DETAIL }));
  await page.route("**/api/credentials", (r) => r.fulfill({
    json: [
      { id: 1, name: "home.gell.one join account", type: "ad", created_at: "2026-06-01T00:00:00Z", last_checked_at: "2026-07-27T09:00:00Z", ad_count: 14, entra_count: 0, intune_count: 0 },
      { id: 2, name: "Entra app registration", type: "entra", created_at: "2026-06-01T00:00:00Z", last_checked_at: "2026-07-27T09:00:00Z", ad_count: 0, entra_count: 10, intune_count: 4 }
    ]
  }));
  await page.route("**/api/sdn/labs/orphan-vnets", (r) => r.fulfill({ json: { orphan_vnets: [{ vnet: "lab99v", zone: "lab99z", subnet: "10.99.0.0/24" }] } }));
  await page.route("**/api/sdn/labs/*/network", (r) => r.fulfill({ json: { subnet: "192.168.16.0/24", gateway: "192.168.16.1", dhcp_start: "192.168.16.40", dhcp_end: "192.168.16.200", snat: true } }));
}

test("capture fleet and detail", async ({ page }) => {
  await mockFleet(page);

  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto("/react/vms");
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${SHOTS}/fleet-1600-fold.png` });
  await page.screenshot({ path: `${SHOTS}/fleet-1600-full.png`, fullPage: true });

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOTS}/fleet-1280-fold.png` });

  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto("/react/vms/201");
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${SHOTS}/detail-1600-fold.png` });
  await page.screenshot({ path: `${SHOTS}/detail-1600-full.png`, fullPage: true });

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOTS}/detail-1280-fold.png` });
});
