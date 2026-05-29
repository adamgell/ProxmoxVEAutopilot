import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const provisionPayload = {
  profiles: {
    surface: {
      manufacturer: "Microsoft",
      product: "Surface Pro"
    }
  },
  defaults: {
    cores: 4,
    memory_mb: 8192,
    disk_size_gb: 96,
    count: 2,
    serial_prefix: "Gell",
    group_tag: "pilot",
    oem_profile: "surface",
    template_vmid: "250",
    hostname_pattern: "autopilot-{serial}"
  },
  template_disk_gb: 80,
  winpe_enabled: false,
  cloudosd_catalog: {
    os_versions: ["Windows 11 24H2"],
    os_editions: ["Enterprise"],
    os_activations: ["Volume"],
    os_languages: ["en-US"],
    driver_pack_policies: ["none", "manufacturer"],
    defaults: {
      os_version: "Windows 11 24H2",
      os_edition: "Enterprise",
      os_activation: "Volume",
      os_language: "en-US",
      driver_pack_policy: "none",
      vm_cores: 4,
      vm_memory_mb: 8192,
      vm_disk_size_gb: 96,
      minimum_vm_memory_mb: 4096,
      minimum_vm_disk_size_gb: 64
    }
  },
  cloudosd_options: {
    nodes: ["pve2"],
    bridges: ["vmbr0"],
    network_targets: [
      { kind: "bridge", value: "vmbr0", label: "vmbr0" },
      { kind: "sdn_vnet", value: "lab101", label: "Lab 101", zone: "lab-simple" }
    ],
    storages: {
      iso: ["local"],
      disk: ["local-lvm"]
    },
    defaults: {
      node: "pve2",
      iso_storage: "local",
      disk_storage: "local-lvm",
      bridge: "vmbr0"
    }
  },
  osdeploy_catalog: {
    server_roles: ["base", "file_server"],
    os_versions: ["Windows Server 2025"],
    os_editions: ["Datacenter"],
    os_languages: ["en-US"],
    defaults: {
      os_version: "Windows Server 2025",
      os_edition: "Datacenter",
      os_language: "en-US",
      vm_cores: 4,
      vm_memory_mb: 8192,
      vm_disk_size_gb: 128,
      minimum_vm_memory_mb: 4096,
      minimum_vm_disk_size_gb: 96
    }
  },
  osdeploy_options: {
    nodes: ["pve2"],
    bridges: ["vmbr0"],
    network_targets: [
      { kind: "bridge", value: "vmbr0", label: "vmbr0" },
      { kind: "sdn_vnet", value: "lab101", label: "Lab 101", zone: "lab-simple" }
    ],
    storages: {
      iso: ["local"],
      disk: ["local-lvm"]
    },
    defaults: {
      node: "pve2",
      iso_storage: "local",
      disk_storage: "local-lvm",
      bridge: "vmbr0"
    }
  },
  cloudosd_artifacts: [
    {
      id: "cloud-artifact",
      build_sha: "cloud123",
      osdcloud_module_version: "26.4.17.1",
      readiness: "ready",
      ready: true,
      proxmox_volid: "local:iso/cloud.iso"
    }
  ],
  osdeploy_artifacts: [
    {
      id: "server-artifact",
      build_sha: "srv123",
      os_version: "Windows Server 2025",
      os_edition: "Datacenter",
      readiness: "ready",
      ready: true,
      proxmox_volid: "local:iso/server.iso"
    }
  ],
  cloudosd_batch_progress: {
    summary: {
      total: 1,
      deployed: 1,
      uploaded: 1,
      assigned: 0,
      contacted_enrolled: 0
    },
    runs: [
      {
        run_id: "run-cloud-1",
        vm_name: "Gell-EC41E7EB",
        vmid: 116,
        done_count: 4,
        total_count: 8,
        failed_count: 0,
        milestones: {
          vm_created: { state: "done", label: "created", detail: "VM 116" },
          pe_registered: { state: "done", label: "registered", detail: "PE" },
          osdcloud_done: { state: "waiting", label: "waiting", detail: "OSDCloud" },
          agent_heartbeat: { state: "waiting", label: "waiting", detail: "agent" },
          v2_steps_done: { state: "waiting", label: "waiting", detail: "steps" },
          intune_state: { state: "waiting", label: "waiting", detail: "Intune" }
        }
      }
    ]
  },
  cloudosd_cache: {
    storage: { ready: true, root: "/app/cache/cloudosd" },
    summary: { ready: 2, total: 3 }
  },
  osdeploy_cache: {
    storage: { ready: false, root: "/app/cache/osdeploy" },
    summary: { ready: 1, total: 4 }
  },
  ubuntu_v2_sequences: [
    {
      id: "ubuntu-seq",
      name: "Ubuntu Desktop",
      step_count: 5
    }
  ]
};

function mockFetch() {
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
    const url = new URL(path, "http://localhost");
    if (url.pathname !== "/api/provision/page") {
      return Promise.resolve(new Response("not found", { status: 404 }));
    }
    return Promise.resolve(new Response(JSON.stringify(provisionPayload), {
      status: 200,
      headers: { "content-type": "application/json" }
    }));
  }));
}

function renderProvision() {
  window.history.pushState({}, "", "/react/provision");
  render(<App bootstrap={{ buildSha: "testsha", buildTime: "2026-05-20T12:00:00-04:00" }} />);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ProvisionPage", () => {
  test("renders the provision builder form instead of a generic payload list", async () => {
    mockFetch();
    renderProvision();

    expect(await screen.findByRole("heading", { name: "Provision" })).toBeInTheDocument();
    expect(await screen.findByRole("combobox", { name: "Boot mode" })).toHaveValue("cloudosd");
    // Operators no longer pick the OSDCloud artifact; the backend auto-selects it.
    expect(screen.queryByRole("combobox", { name: "OSDCloud artifact" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Network target" })).toHaveValue("vmbr0");
    expect(screen.getByRole("option", { name: "Lab 101 (SDN: lab-simple)" })).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Task sequence" })).not.toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "VM count" })).toHaveValue(2);
    expect(screen.getByRole("textbox", { name: "Hostname pattern" })).toHaveValue("autopilot-{serial}");
    expect(screen.getByRole("button", { name: "Provision VMs" })).toBeInTheDocument();
    expect(screen.getByText("Gell-EC41E7EB")).toBeInTheDocument();

    const form = screen.getByTestId("provision-builder-form");
    expect(form).toHaveAttribute("method", "post");
    expect(form).toHaveAttribute("action", "/api/jobs/provision");
  });

  test("switches boot-mode sections without losing shared form fields", async () => {
    mockFetch();
    renderProvision();

    const bootMode = await screen.findByRole("combobox", { name: "Boot mode" });
    fireEvent.change(bootMode, { target: { value: "osdeploy" } });

    expect(screen.queryByRole("combobox", { name: "OSDCloud artifact" })).not.toBeInTheDocument();
    // Operators no longer pick the OSDeploy Server artifact; the backend auto-selects it.
    expect(screen.queryByRole("combobox", { name: "OSDeploy artifact" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Server role" })).toHaveValue("base");
    expect(screen.getByRole("combobox", { name: "OSDeploy network target" })).toHaveValue("vmbr0");
    expect(screen.getByRole("textbox", { name: "Hostname pattern" })).toHaveValue("autopilot-{serial}");

    fireEvent.change(bootMode, { target: { value: "ubuntu" } });
    expect(screen.queryByRole("combobox", { name: "OSDeploy artifact" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Ubuntu v2 sequence" })).toHaveValue("");
    expect(screen.getByRole("spinbutton", { name: "Ubuntu template VMID" })).toHaveValue(250);
  });
});
