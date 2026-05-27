import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const osdeployPayload = {
  view: "overview",
  catalog: {
    os_versions: ["Windows Server 2022"],
    os_editions: ["Datacenter", "Standard"],
    os_languages: ["en-us"],
    server_roles: ["base", "file_server", "isolated_domain_controller", "mecm_prereq", "lab_in_a_box"],
    role_catalog: {
      base: {
        name: "Windows Server Base",
        readiness_status: "base_ready",
        required_fields: [],
        credential_fields: [],
        step_kinds: []
      },
      isolated_domain_controller: {
        name: "Isolated Domain Controller",
        readiness_status: "domain_controller_ready",
        required_fields: ["forest_fqdn", "netbios_name"],
        credential_fields: ["forest_admin_credential_id", "dsrm_credential_id"],
        step_kinds: ["install_ad_ds"]
      }
    },
    defaults: {
      osdeploy_module_version: "1.0.1",
      vm_cores: 4,
      vm_memory_mb: 8192,
      vm_disk_size_gb: 100,
      minimum_vm_memory_mb: 6144,
      minimum_vm_disk_size_gb: 80,
      os_version: "Windows Server 2022",
      os_edition: "Datacenter",
      os_language: "en-us"
    }
  },
  proxmox_options: {
    nodes: ["pve2"],
    bridges: ["vmbr0"],
    network_targets: [
      { kind: "bridge", value: "vmbr0", label: "vmbr0" },
      { kind: "sdn_vnet", value: "lab101", label: "Lab 101", zone: "lab-simple" }
    ],
    storages: {
      iso: ["isos"],
      disk: ["ssdpool"]
    },
    defaults: {
      node: "pve2",
      iso_storage: "isos",
      disk_storage: "ssdpool",
      bridge: "vmbr0"
    }
  },
  osdeploy_build_defaults: {
    remote: "builder@10.0.0.5",
    remote_root: "F:\\BuildRoot",
    ssh_key_path: "/app/secrets/osdeploy_build_host",
    ssh_key_exists: true,
    ssh_public_key: "ssh-rsa AAAATEST"
  },
  osdeploy_credentials: [
    { id: 7, name: "Lab Admin", type: "ad" }
  ],
  artifacts: [
    {
      id: "artifact-server",
      architecture: "amd64",
      osdeploy_module_version: "1.0.1",
      osdbuilder_module_version: "23.10.1",
      adk_version: "10.1.25398",
      build_sha: "srvsha",
      readiness: "ready",
      ready: true,
      source_media: "server.iso",
      image_name: "Windows Server 2022 Datacenter",
      image_index: 4,
      os_version: "Windows Server 2022",
      os_edition: "Datacenter",
      os_language: "en-us",
      iso_path: "/app/output/osdeploy.iso",
      wim_path: "/app/output/install.wim",
      manifest_path: "/app/output/manifest.json",
      proxmox_volid: "isos:iso/osdeploy.iso"
    }
  ],
  ready_artifacts: [
    {
      id: "artifact-server",
      architecture: "amd64",
      osdeploy_module_version: "1.0.1",
      build_sha: "srvsha",
      readiness: "ready",
      ready: true,
      os_version: "Windows Server 2022",
      os_edition: "Datacenter",
      os_language: "en-us",
      proxmox_volid: "isos:iso/osdeploy.iso"
    }
  ],
  active_runs: [
    {
      run_id: "run-active",
      requested_vm_name: "OSDEPLOY-BASE",
      state: "provisioning"
    }
  ],
  stale_failed_runs: [
    {
      run_id: "run-stale",
      requested_vm_name: "OSDEPLOY-OLD",
      state: "failed"
    }
  ],
  runs: [
    {
      run_id: "run-complete",
      artifact_id: "artifact-server",
      requested_vm_name: "OSDEPLOY-BASE",
      expected_computer_name: "OSDEPLOY-BASE",
      vmid: 220,
      state: "complete",
      server_role: "base",
      os_version: "Windows Server 2022",
      os_edition: "Datacenter",
      created_at: "2026-05-20T10:00:00-04:00",
      first_heartbeat_at: "2026-05-20T10:08:00-04:00"
    }
  ],
  osdeploy_cache: {
    storage: {
      root: "/app/cache/osdeploy",
      ready: true
    },
    summary: {
      ready: 1,
      total: 2,
      warming: 1,
      failed: 0
    },
    entries: [
      {
        id: "cache-server",
        entry_type: "feature_image",
        status: "ready",
        windows_version: "Windows Server 2022",
        architecture: "amd64",
        edition: "Datacenter",
        language: "en-us",
        file_name: "install.wim",
        size_bytes: 4096,
        sha256: "a".repeat(64),
        verified_at: "2026-05-20T10:15:00-04:00"
      }
    ]
  }
};

function payloadForUrl(url: URL) {
  return {
    ...osdeployPayload,
    view: url.searchParams.get("view") || "overview"
  };
}

function mockFetch() {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/api/osdeploy/page") {
      return Promise.resolve(new Response(JSON.stringify(payloadForUrl(url)), {
        status: 200,
        headers: { "content-type": "application/json" }
      }));
    }
    if (url.pathname === "/api/osdeploy/v1/preflight" && init?.method === "POST") {
      return Promise.resolve(new Response(JSON.stringify({
        launch_allowed: true,
        target: { computer_name: "OSDEPLOY-BASE-260520-0000" },
        blocking_checks: [],
        warnings: [{ id: "cache", message: "quality update warming" }]
      }), {
        status: 200,
        headers: { "content-type": "application/json" }
      }));
    }
    if (url.pathname === "/api/osdeploy/v1/runs" && init?.method === "POST") {
      return Promise.resolve(new Response(JSON.stringify({ run: { run_id: "run-created" } }), {
        status: 200,
        headers: { "content-type": "application/json" }
      }));
    }
    if (url.pathname === "/api/osdeploy/v1/runs/run-created/provision" && init?.method === "POST") {
      return Promise.resolve(new Response(JSON.stringify({ job_id: "job-provision" }), {
        status: 200,
        headers: { "content-type": "application/json" }
      }));
    }
    if (url.pathname === "/api/osdeploy/v1/artifacts/build/preflight" && init?.method === "POST") {
      return Promise.resolve(new Response(JSON.stringify({ blocking_checks: [] }), {
        status: 200,
        headers: { "content-type": "application/json" }
      }));
    }
    if (url.pathname === "/api/osdeploy/v1/artifacts/build" && init?.method === "POST") {
      return Promise.resolve(new Response(JSON.stringify({ job_id: "job-build" }), {
        status: 200,
        headers: { "content-type": "application/json" }
      }));
    }
    if (url.pathname === "/api/osdeploy/v1/cache/catalog/refresh" && init?.method === "POST") {
      return Promise.resolve(new Response(JSON.stringify({ job_id: "job-cache" }), {
        status: 200,
        headers: { "content-type": "application/json" }
      }));
    }
    return Promise.resolve(new Response("not found", { status: 404 }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderOsdeploy(path = "/react/osdeploy") {
  window.history.pushState({}, "", path);
  render(<App bootstrap={{ buildSha: "testsha", buildTime: "2026-05-20T12:00:00-04:00" }} />);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("OsdeployPage", () => {
  test("renders the OSDeploy cockpit instead of the generic retired payload page", async () => {
    mockFetch();
    renderOsdeploy();

    expect(await screen.findByRole("heading", { name: "OSDeploy Server" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "OSDeploy pages" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Builder" })).toHaveAttribute("href", "/react/osdeploy?view=builder");
    expect(screen.getByRole("heading", { name: "Operator Flow" })).toBeInTheDocument();
    expect(within(await screen.findByRole("table", { name: "OSDeploy runs" })).getAllByText("OSDEPLOY-BASE").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Archive stale failed" })).toBeInTheDocument();
    expect(screen.queryByText("ready_artifacts")).not.toBeInTheDocument();
  });

  test("builder view exposes role variables, preflight, and launch controls", async () => {
    const fetchMock = mockFetch();
    renderOsdeploy("/react/osdeploy?view=builder");

    expect(await screen.findByRole("heading", { name: "Server Deployment" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Deployable OS" })).toHaveValue("artifact-server");
    expect(screen.getByRole("combobox", { name: "Server role" })).toHaveValue("base");
    expect(screen.getByRole("combobox", { name: "Network target" })).toHaveValue("vmbr0");
    expect(screen.getByRole("option", { name: "Lab 101 (SDN: lab-simple)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Launch OSDeploy VM" })).toBeDisabled();

    fireEvent.change(screen.getByRole("combobox", { name: "Server role" }), { target: { value: "isolated_domain_controller" } });
    expect(screen.getByRole("textbox", { name: "Domain / forest FQDN" })).toHaveValue("lab.gell.one");
    expect(screen.getByRole("combobox", { name: "Forest admin credential" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run Preflight" }));

    await waitFor(() => expect(screen.getByText("Launch allowed")).toBeInTheDocument());
    expect(screen.getByText("cache: quality update warming")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Launch OSDeploy VM" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Launch OSDeploy VM" }));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/osdeploy/v1/runs", expect.objectContaining({ method: "POST" }));
    });
  });

  test("cache view queues refresh against the OSDeploy cache API", async () => {
    const fetchMock = mockFetch();
    renderOsdeploy("/react/osdeploy?view=cache");

    expect(await screen.findByRole("heading", { name: "OSDeploy Cache" })).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: "OSDeploy cache entries" })).getByText("Windows Server 2022")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh catalog" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/osdeploy/v1/cache/catalog/refresh", expect.objectContaining({ method: "POST" }));
    });
  });

  test("artifacts view preflights before queueing a build", async () => {
    const fetchMock = mockFetch();
    renderOsdeploy("/react/osdeploy?view=artifacts");

    expect(await screen.findByRole("heading", { name: "Artifacts" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Remote host" })).toHaveValue("builder@10.0.0.5");
    expect(within(screen.getByRole("table", { name: "OSDeploy artifacts" })).getByText("srvsha")).toBeInTheDocument();

    fireEvent.submit(screen.getByTestId("osdeploy-build-form"));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/osdeploy/v1/artifacts/build/preflight", expect.objectContaining({ method: "POST" }));
    });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/osdeploy/v1/artifacts/build", expect.objectContaining({ method: "POST" }));
    });
    expect(await screen.findByText("job-build")).toBeInTheDocument();
  });
});
