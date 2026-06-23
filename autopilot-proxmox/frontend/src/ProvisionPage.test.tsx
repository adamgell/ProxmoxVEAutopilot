import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

function mockStorage(store: Map<string, string>) {
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: vi.fn((key: string) => store.get(key) ?? null),
      removeItem: vi.fn((key: string) => {
        store.delete(key);
      }),
      setItem: vi.fn((key: string, value: string) => {
        store.set(key, value);
      })
    }
  });
}

function renderProvision(store = new Map<string, string>()) {
  mockStorage(store);
  window.history.pushState({}, "", "/react/provision");
  render(<App bootstrap={{ buildSha: "testsha", buildTime: "2026-05-20T12:00:00-04:00" }} />);
}

function namedControl(container: HTMLElement, name: string): HTMLElement {
  const control = container.querySelector<HTMLElement>(`[name="${name}"]`);
  if (!control) {
    throw new Error(`Missing form control named ${name}`);
  }
  return control;
}

afterEach(() => {
  cleanup();
  window.localStorage.removeItem("pveautopilot.provision.templates.v1");
  window.localStorage.removeItem("pveautopilot.provision.draft.v1");
  vi.unstubAllGlobals();
});

describe("ProvisionPage", () => {
  test("renders the provision launch layout with run-tag naming and CloudOSD readiness", async () => {
    mockFetch();
    renderProvision();

    expect(await screen.findByRole("heading", { name: "Provision" })).toBeInTheDocument();
    expect(await screen.findByRole("radio", { name: "OSDCloud" })).toBeChecked();
    expect(screen.getByRole("textbox", { name: "Run tag" })).toHaveValue("pilot");
    // Operators no longer pick the OSDCloud artifact; the backend auto-selects it.
    expect(screen.queryByRole("combobox", { name: "OSDCloud artifact" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Network target" })).toHaveValue("vmbr0");
    expect(screen.getByRole("option", { name: "Lab 101 (SDN: lab-simple)" })).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Task sequence" })).not.toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "VM count" })).toHaveValue(2);
    expect(screen.getByRole("textbox", { name: "Hostname pattern" })).toHaveValue("pilot-{index}");
    expect(screen.getAllByText("pilot-01").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Provision VMs" })).toBeInTheDocument();
    expect(screen.getByText("Gell-EC41E7EB")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Advanced OSDCloud Options" })).toBeInTheDocument();
    expect(screen.getByText("Artifact readiness")).toBeInTheDocument();
    expect(screen.getByText(/cloud123/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Autopilot Enrollment" })).toBeInTheDocument();
    expect(screen.getByText("Hash capture")).toBeInTheDocument();

    const form = await screen.findByTestId("provision-builder-form");
    expect(form).toHaveAttribute("method", "post");
    expect(form).toHaveAttribute("action", "/api/jobs/provision");
    expect(namedControl(form, "boot_mode")).toHaveValue("cloudosd");
    expect(namedControl(form, "profile")).toBeInTheDocument();
    expect(namedControl(form, "count")).toBeInTheDocument();
    expect(namedControl(form, "hostname_pattern")).toBeInTheDocument();
    expect(namedControl(form, "group_tag")).toBeInTheDocument();
    expect(namedControl(form, "cores")).toBeInTheDocument();
    expect(namedControl(form, "memory_mb")).toBeInTheDocument();
    expect(namedControl(form, "disk_size_gb")).toBeInTheDocument();
    expect(namedControl(form, "network_bridge")).toBeInTheDocument();
    expect(namedControl(form, "os_version")).toBeInTheDocument();
  });

  test("keeps the launch review in the desktop launch grid instead of the form bottom", async () => {
    mockFetch();
    renderProvision();

    await screen.findByRole("heading", { name: "Provision" });

    const form = await screen.findByTestId("provision-builder-form");
    const launchGrid = form.querySelector(".provision-launch-grid");
    expect(launchGrid).not.toBeNull();
    expect(Array.from(launchGrid?.children ?? []).map((element) => element.className)).toEqual([
      "provision-section-stack",
      "provision-enrollment-stack",
      "provision-review-column"
    ]);

    const reviewColumn = form.querySelector(".provision-review-column");
    const launchReview = screen.getByLabelText("Launch review");
    expect(reviewColumn).toContainElement(launchReview);
    expect(reviewColumn).toContainElement(screen.getByRole("button", { name: "Provision VMs" }));
    expect(form.lastElementChild).not.toHaveClass("utility-form-actions");
  });

  test("blocks unsafe manual hostname patterns and shows the normalized preview", async () => {
    mockFetch();
    renderProvision();

    const hostnamePattern = await screen.findByRole("textbox", { name: "Hostname pattern" });
    expect(hostnamePattern).toHaveValue("pilot-{index}");

    fireEvent.change(hostnamePattern, { target: { value: "autopilot-{serial}" } });

    expect(screen.getByRole("button", { name: "Provision VMs" })).toBeDisabled();
    expect(screen.getAllByText("autopilot-SERIAL01").length).toBeGreaterThan(0);
    expect(screen.getByText("18 / 15")).toBeInTheDocument();
    expect(screen.getAllByText(/Normalized preview: autopilot-seria/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Provisioning is blocked/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reset hostname from run tag" }));

    expect(hostnamePattern).toHaveValue("pilot-{index}");
    expect(screen.getByRole("button", { name: "Provision VMs" })).not.toBeDisabled();
  });

  test("fills down run tag to group tag and derives a Windows-safe hostname preview", async () => {
    mockFetch();
    renderProvision();

    const runTag = await screen.findByRole("textbox", { name: "Run tag" });
    fireEvent.change(runTag, { target: { value: "NTTENANT01-Desktop" } });

    expect(screen.getByRole("textbox", { name: "Group tag" })).toHaveValue("NTTENANT01-Desktop");
    expect(screen.getByRole("textbox", { name: "Hostname pattern" })).toHaveValue("ntt01-{index}");
    expect(screen.getAllByText("ntt01-01").length).toBeGreaterThan(0);
    expect(screen.getByText("8 / 15")).toBeInTheDocument();

    fireEvent.change(runTag, { target: { value: "VeryLongTenantWorkstations" } });

    expect(screen.getByRole("textbox", { name: "Group tag" })).toHaveValue("VeryLongTenantWorkstations");
    expect(screen.getByRole("textbox", { name: "Hostname pattern" })).toHaveValue("verylongtena-{index}");
    expect(screen.getAllByText("verylongtena-01").length).toBeGreaterThan(0);
    expect(screen.getByText("15 / 15")).toBeInTheDocument();
  });

  test("switches boot-mode sections without losing shared form fields", async () => {
    mockFetch();
    renderProvision();

    await screen.findByRole("radio", { name: "OSDCloud" });
    fireEvent.change(screen.getByRole("textbox", { name: "Hostname pattern" }), { target: { value: "lab-{index}" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Group tag" }), { target: { value: "manual-group" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "VM count" }), { target: { value: "7" } });

    fireEvent.click(screen.getByRole("radio", { name: "OSDeploy v2" }));

    expect(screen.queryByRole("combobox", { name: "OSDCloud artifact" })).not.toBeInTheDocument();
    // Operators no longer pick the OSDeploy Server artifact; the backend auto-selects it.
    expect(screen.queryByRole("combobox", { name: "OSDeploy artifact" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Server role" })).toHaveValue("base");
    expect(screen.getByRole("combobox", { name: "OSDeploy network target" })).toHaveValue("vmbr0");
    expect(screen.getByRole("textbox", { name: "Hostname pattern" })).toHaveValue("lab-{index}");
    expect(screen.getByRole("textbox", { name: "Group tag" })).toHaveValue("manual-group");
    expect(screen.getByRole("spinbutton", { name: "VM count" })).toHaveValue(7);

    const osdeployForm = screen.getByTestId("provision-builder-form");
    expect(namedControl(osdeployForm, "boot_mode")).toHaveValue("osdeploy");
    expect(namedControl(osdeployForm, "osdeploy_network_bridge")).toBeInTheDocument();
    expect(namedControl(osdeployForm, "osdeploy_os_version")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "Ubuntu v2" }));
    expect(screen.queryByRole("combobox", { name: "OSDeploy artifact" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Ubuntu v2 sequence" })).toHaveValue("");
    expect(screen.getByRole("spinbutton", { name: "Ubuntu template VMID" })).toHaveValue(250);
    expect(screen.getByRole("textbox", { name: "Hostname pattern" })).toHaveValue("lab-{index}");
    expect(screen.getByRole("textbox", { name: "Group tag" })).toHaveValue("manual-group");
    expect(screen.getByRole("spinbutton", { name: "VM count" })).toHaveValue(7);

    const ubuntuForm = screen.getByTestId("provision-builder-form");
    expect(namedControl(ubuntuForm, "boot_mode")).toHaveValue("ubuntu");
    expect(namedControl(ubuntuForm, "ubuntu_v2_sequence_id")).toBeInTheDocument();
    expect(namedControl(ubuntuForm, "ubuntu_template_vmid")).toBeInTheDocument();
  });

  test("saves and restores named provision templates from the form", async () => {
    mockFetch();
    renderProvision();

    await screen.findByRole("radiogroup", { name: "Boot mode" });
    fireEvent.change(screen.getByRole("textbox", { name: "Group tag" }), { target: { value: "ring0ivy24" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Serial prefix" }), { target: { value: "ring0" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "VM count" }), { target: { value: "4" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Template name" }), { target: { value: "Ring 0 Ivy24" } });

    fireEvent.click(screen.getByRole("button", { name: "Save template" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Group tag" }), { target: { value: "throwaway" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Serial prefix" }), { target: { value: "tmp" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "VM count" }), { target: { value: "1" } });

    fireEvent.change(screen.getByRole("combobox", { name: "Saved template" }), { target: { value: "Ring 0 Ivy24" } });
    fireEvent.click(screen.getByRole("button", { name: "Load template" }));

    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "Group tag" })).toHaveValue("ring0ivy24");
      expect(screen.getByRole("textbox", { name: "Serial prefix" })).toHaveValue("ring0");
      expect(screen.getByRole("spinbutton", { name: "VM count" })).toHaveValue(4);
    });
  });

  test("restores the last draft after the provision page remounts", async () => {
    const store = new Map<string, string>();
    mockFetch();
    renderProvision(store);

    await screen.findByRole("radiogroup", { name: "Boot mode" });
    fireEvent.change(screen.getByRole("textbox", { name: "Group tag" }), { target: { value: "draft-ring" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Serial prefix" }), { target: { value: "draft" } });

    cleanup();
    renderProvision(store);

    await screen.findByRole("textbox", { name: "Group tag" });
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "Group tag" })).toHaveValue("draft-ring");
      expect(screen.getByRole("textbox", { name: "Serial prefix" })).toHaveValue("draft");
    });
  });
});
