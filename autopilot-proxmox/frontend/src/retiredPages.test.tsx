import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const pageResponses: Record<string, unknown> = {
  "/api/install-tracking/page": {
    tracking: {
      run: { run_id: "install-1", name: "Clean install", target: "pve2" },
      items: [{ item_id: "media", label: "Media staged", status: "ok", detail: "ready" }],
      summary: { total: 1, ok: 1, blocked: 0 }
    }
  },
  "/api/provision/page": {
    defaults: { cores: 4, memory_mb: 8192, disk_size_gb: 96 },
    sequences: [{ id: 1, name: "Windows baseline", target_os: "windows", boot_modes: ["cloudosd"] }],
    cloudosd_catalog: { count: 2 },
    osdeploy_catalog: { count: 1 }
  },
  "/api/cloudosd/page": {
    view: "overview",
    runs: [{ run_id: "cloud-1", requested_vm_name: "Gell-EC41E7EB", state: "complete" }],
    artifacts: [{ artifact_id: "artifact-1", name: "Win 11", ready: true }],
    cloudosd_cache: { summary: { entries: 3 } }
  },
  "/api/osdeploy/page": {
    view: "overview",
    runs: [{ run_id: "osd-1", requested_vm_name: "SRV-01", state: "running" }],
    artifacts: [{ artifact_id: "osd-artifact-1", name: "Server 2025", ready: true }],
    osdeploy_cache: { summary: { entries: 2 } }
  },
  "/api/template/page": {
    profiles: { surface: { manufacturer: "Microsoft", product: "Surface Pro" } },
    ubuntu_sequences: [{ id: 2, name: "Ubuntu Desktop" }],
    hypervisor_type: "proxmox",
    utm_iso_dir: "/Users/Adam/UTM-ISOs"
  },
  "/api/answer-isos/page": {
    rows: [{
      hash: "answer-1",
      short_hash: "answer-1",
      volid: "/var/lib/vz/snippets/autopilot-unattend.img",
      compiled_at: "2026-05-20T12:00:00-04:00",
      last_used_at: null,
      in_use: true
    }],
    error: ""
  },
  "/api/sequences/page": {
    sequences: [{ id: 1, name: "Legacy baseline", target_os: "windows", steps: [] }]
  },
  "/api/utm-vms/page": {
    vms: [{ name: "Win11-UTM", status: "started", ip: "192.168.64.10" }],
    host_summary: { running: 1 },
    isos: [{ name: "Win11.iso" }]
  },
  "/api/setup/v1/state": {
    ready: true,
    phase: "ready",
    status: "ready",
    detail: "ready"
  }
};

function mockFetch() {
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
    const url = new URL(path, "http://localhost");
    const body = pageResponses[url.pathname] ?? pageResponses[`${url.pathname}${url.search}`] ?? {};
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" }
    }));
  }));
}

function renderPath(path: string) {
  window.history.pushState({}, "", path);
  render(<App bootstrap={{ buildSha: "testsha", buildTime: "2026-05-20T12:00:00-04:00" }} />);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("retired Jinja React pages", () => {
  test.each([
    ["/react/install-tracking", "Install Tracking", "Clean install"],
    ["/react/provision", "Provision", "Windows baseline"],
    ["/react/cloudosd", "OSDCloud Desktop", "Gell-EC41E7EB"],
    ["/react/osdeploy", "OSDeploy Server", "SRV-01"],
    ["/react/template", "Build Template", "Surface Pro"],
    ["/react/answer-isos", "Answer ISO Cache", "autopilot-unattend.img"],
    ["/react/sequences", "Sequences", "Legacy baseline"],
    ["/react/utm-vms", "UTM VMs", "Win11-UTM"],
    ["/setup", "Setup", "ready"]
  ])("renders %s from page payload", async (path, heading, visibleText) => {
    mockFetch();
    renderPath(path);

    expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(new RegExp(visibleText, "u"))).toBeInTheDocument();
    });
  });
});
