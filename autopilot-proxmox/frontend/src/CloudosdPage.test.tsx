import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const cloudosdPayload = {
  view: "overview",
  catalog: {
    os_versions: ["Windows 11 25H2"],
    os_editions: ["Enterprise"],
    os_activations: ["Volume"],
    os_languages: ["en-us"],
    defaults: {
      osdcloud_module_version: "26.4.17.1",
      vm_cores: 4,
      vm_memory_mb: 8192,
      vm_disk_size_gb: 96,
      minimum_vm_memory_mb: 4096,
      minimum_vm_disk_size_gb: 64,
      recommended_vm_memory_mb: 8192,
      os_version: "Windows 11 25H2",
      os_edition: "Enterprise",
      os_activation: "Volume",
      os_language: "en-us"
    }
  },
  proxmox_options: {
    nodes: ["pve2"],
    bridges: ["vmbr0"],
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
  artifacts: [
    {
      id: "artifact-1",
      architecture: "amd64",
      osdcloud_module_version: "26.4.17.1",
      build_sha: "abc123",
      readiness: "ready",
      ready: true,
      iso_sha256: "f".repeat(64),
      wim_sha256: "e".repeat(64),
      proxmox_volid: "isos:iso/cloudosd.iso",
      built_by_host: "builder",
      built_at: "2026-05-20T10:00:00-04:00"
    }
  ],
  ready_artifacts: [
    {
      id: "artifact-1",
      architecture: "amd64",
      osdcloud_module_version: "26.4.17.1",
      build_sha: "abc123",
      readiness: "ready",
      ready: true,
      proxmox_volid: "isos:iso/cloudosd.iso",
      iso_sha256: "f".repeat(64)
    }
  ],
  active_runs: [
    {
      run_id: "run-active",
      requested_vm_name: "CLOUDOSD-001",
      state: "provisioning"
    }
  ],
  stale_failed_runs: [
    {
      run_id: "run-failed",
      requested_vm_name: "CLOUDOSD-OLD",
      state: "failed"
    }
  ],
  runs: [
    {
      run_id: "run-active",
      artifact_id: "artifact-1",
      requested_vm_name: "CLOUDOSD-001",
      pve_vm_name: "CLOUDOSD-001",
      heartbeat_computer_name: "CLOUDOSD-001",
      vmid: 120,
      state: "complete",
      os_version: "Windows 11 25H2",
      os_edition: "Enterprise",
      os_activation: "Volume",
      created_at: "2026-05-20T10:00:00-04:00",
      first_heartbeat_at: "2026-05-20T10:08:00-04:00",
      name_comparison: {
        mismatch: false,
        requested_was_normalized: false
      },
      intune_evidence: {
        hash: { status: "uploaded" },
        autopilot: { status: "registered" },
        assignment: { status: "assigned" },
        enrollment: { status: "waiting" }
      }
    }
  ],
  cloudosd_cache: {
    storage: {
      root: "/app/cache/cloudosd",
      ready: true,
      free_bytes: 1024
    },
    summary: {
      ready: 2,
      total: 3,
      warming: 1,
      failed: 0
    },
    entries: [
      {
        id: "cache-1",
        entry_type: "feature_image",
        status: "ready",
        windows_version: "Windows 11 25H2",
        architecture: "amd64",
        edition: "Enterprise",
        activation: "Volume",
        language: "en-us",
        file_name: "install.esd",
        size_bytes: 1234,
        sha256: "a".repeat(64),
        verified_at: "2026-05-20T10:15:00-04:00"
      }
    ]
  }
};

function payloadForUrl(url: URL) {
  return {
    ...cloudosdPayload,
    view: url.searchParams.get("view") || "overview"
  };
}

function mockFetch() {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/api/cloudosd/page") {
      return Promise.resolve(new Response(JSON.stringify(payloadForUrl(url)), {
        status: 200,
        headers: { "content-type": "application/json" }
      }));
    }
    if (url.pathname === "/api/cloudosd/preflight" && init?.method === "POST") {
      return Promise.resolve(new Response(JSON.stringify({
        launch_allowed: true,
        normalized_computer_name: "CLOUDOSD-001",
        blocking_checks: [],
        warnings: [{ label: "cache", detail: "quality update warming" }]
      }), {
        status: 200,
        headers: { "content-type": "application/json" }
      }));
    }
    if (url.pathname === "/api/cloudosd/artifacts/build" && init?.method === "POST") {
      return Promise.resolve(new Response(JSON.stringify({ job_id: "job-build" }), {
        status: 200,
        headers: { "content-type": "application/json" }
      }));
    }
    if (url.pathname === "/api/cloudosd/cache/catalog/refresh" && init?.method === "POST") {
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

function renderCloudosd(path = "/react/cloudosd") {
  window.history.pushState({}, "", path);
  render(<App bootstrap={{ buildSha: "testsha", buildTime: "2026-05-20T12:00:00-04:00" }} />);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("CloudosdPage", () => {
  test("renders the OSDCloud cockpit instead of the generic retired payload page", async () => {
    mockFetch();
    renderCloudosd();

    expect(await screen.findByRole("heading", { name: "OSDCloud Desktop" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "OSDCloud pages" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Builder" })).toHaveAttribute("href", "/react/cloudosd?view=builder");
    expect(screen.getByRole("heading", { name: "Operator Flow" })).toBeInTheDocument();
    expect(within(await screen.findByRole("table", { name: "OSDCloud runs" })).getAllByText("CLOUDOSD-001").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Archive stale failed" })).toBeInTheDocument();
    expect(screen.queryByText("ready_artifacts")).not.toBeInTheDocument();
  });

  test("builder view exposes preflight and review controls", async () => {
    mockFetch();
    renderCloudosd("/react/cloudosd?view=builder");

    expect(await screen.findByRole("heading", { name: "Single-VM Deployment" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Ready artifact" })).toHaveValue("artifact-1");
    expect(screen.getByRole("spinbutton", { name: "Memory MB" })).toHaveValue(8192);
    expect(screen.getByRole("button", { name: "Launch OSDCloud VM" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Run Preflight" }));

    await waitFor(() => expect(screen.getByText("Launch allowed")).toBeInTheDocument());
    expect(screen.getByText("CLOUDOSD-001")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Launch OSDCloud VM" })).toBeEnabled();
  });

  test("cache view keeps cache actions and entry operations visible", async () => {
    mockFetch();
    renderCloudosd("/react/cloudosd?view=cache");

    expect(await screen.findByRole("heading", { name: "OSDCloud Cache" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh catalog" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Warm all Windows 11" })).toBeInTheDocument();
    expect(screen.getByText("install.esd")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Warm cache-1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Verify cache-1" })).toBeInTheDocument();
  });

  test("artifact view queues a build through the React form", async () => {
    const fetchMock = mockFetch();
    renderCloudosd("/react/cloudosd?view=artifacts");

    expect(await screen.findByRole("heading", { name: "Artifacts" })).toBeInTheDocument();
    fireEvent.submit(screen.getByTestId("cloudosd-build-form"));

    await waitFor(() => expect(screen.getByText("job-build")).toBeInTheDocument());
    const buildCall = fetchMock.mock.calls.find(([input]) => input === "/api/cloudosd/artifacts/build");
    expect(buildCall?.[1]?.method).toBe("POST");
    const body = buildCall?.[1]?.body;
    expect(typeof body).toBe("string");
    expect(JSON.parse(typeof body === "string" ? body : "{}")).toMatchObject({
      remote: "Adam.Gell@10.211.55.6",
      architecture: "amd64",
      osdcloud_module_version: "26.4.17.1"
    });
    expect(within(screen.getByRole("table", { name: "OSDCloud artifacts" })).getByText("abc123")).toBeInTheDocument();
  });
});
