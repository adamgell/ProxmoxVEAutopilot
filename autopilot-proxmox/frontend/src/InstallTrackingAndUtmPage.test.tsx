import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const responses: Readonly<Record<string, unknown>> = {
  "/api/install-tracking/page": {
    tracking: {
      run: { run_id: "install-1", name: "Clean install", target: "pve2" },
      runs: [{ run_id: "install-1", name: "Clean install" }],
      summary: { complete: 1, running: 1, blockers: 0, total: 2, percent: 50 },
      items: [
        { item_id: "media", label: "Media staged", category: "controller", description: "ISO ready", source: "setup", status: "ready", target: "pve2", detail: "ready" },
        { item_id: "agent", label: "Agent reachable", category: "agent", description: "Heartbeat", source: "agent", status: "running", target: "VM 105", detail: "waiting" }
      ],
      events: [{ created_at: "2026-05-20T12:00:00-04:00", item_id: "media", status: "ready", detail: "ready" }]
    }
  },
  "/api/utm-vms/page": {
    vms: [{ name: "Win11-UTM", uuid: "uuid-utm", status: "started", ips: ["192.168.64.10"] }],
    host_summary: { status: "available" },
    isos: [{ name: "Win11.iso", path: "/Users/Adam/UTM-ISOs/Win11.iso" }],
    utmctl_path: "/opt/homebrew/bin/utmctl",
    library_path: "/Users/Adam/Library/Containers/com.utmapp.UTM/Data/Documents"
  }
};

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" }
  });
}

function mockFetch() {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/api/utm/vms/Win11-UTM/stop" && init?.method === "POST") {
      return Promise.resolve(response({ ok: true }));
    }
    return Promise.resolve(response(responses[url.pathname] ?? {}));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPath(path: string) {
  window.history.pushState({}, "", path);
  render(<App bootstrap={{ buildSha: "testsha", buildTime: "2026-05-20T12:00:00-04:00" }} />);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("InstallTracking and UTM pages", () => {
  test("renders deployment readiness metrics, gates, updates, and filter", async () => {
    mockFetch();
    renderPath("/react/install-tracking");

    expect(await screen.findByRole("heading", { name: "Deployment Readiness" })).toBeInTheDocument();
    expect(await screen.findByText("Media staged")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Readiness gates" })).toHaveTextContent("Agent reachable");
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter install gates" }), { target: { value: "media" } });
    expect(screen.getByRole("table", { name: "Readiness gates" })).not.toHaveTextContent("Agent reachable");
    expect(screen.getByRole("table", { name: "Install tracking updates" })).toHaveTextContent("media");
  });

  test("renders UTM VMs, host paths, ISO library, and power action", async () => {
    const fetchMock = mockFetch();
    renderPath("/react/utm-vms");

    expect(await screen.findByRole("heading", { name: "UTM Virtual Machines" })).toBeInTheDocument();
    expect(await screen.findByText("Win11-UTM")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "UTM ISOs" })).toHaveTextContent("Win11.iso");

    fireEvent.click(screen.getByRole("button", { name: /Stop/u }));
    await waitFor(() => expect(screen.getByText("stop requested for Win11-UTM")).toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([input, init]) => input === "/api/utm/vms/Win11-UTM/stop" && init?.method === "POST")).toBe(true);
  });
});
