import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { OsDeployCachePanel } from "./OsDeployCachePanel";

const FACTORY_ENTRY = {
  id: "entry-2022-dc",
  entry_type: "server_image",
  windows_version: "Windows Server 2022",
  edition: "Datacenter",
  status: "discovered",
  size_bytes: null,
  source_url: "manual://microsoft-volume-licensing-or-eval-center",
  error: null,
  metadata: { factory: "OSDeploy/OSDBuilder", content_role: "source_media" }
} as const;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" }
  });
}

function requestMeta(input: RequestInfo | URL, init?: RequestInit) {
  const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
  return { url, method: init?.method ?? "GET" };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("warms a factory server_image entry through the build-host agent", async () => {
  let status = "discovered";
  const warmCalls: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const { url, method } = requestMeta(input, init);
    if (url.endsWith("/api/osdeploy/v1/cache") && method === "GET") {
      return Promise.resolve(jsonResponse({ entries: [{ ...FACTORY_ENTRY, status }] }));
    }
    if (url.endsWith(`/api/osdeploy/v1/cache/${FACTORY_ENTRY.id}/warm`) && method === "POST") {
      warmCalls.push(url);
      status = "warming";
      return Promise.resolve(jsonResponse({ ok: true, kind: "build_osdeploy", work_item_id: "wi-1", status: "pending" }));
    }
    return Promise.resolve(jsonResponse("not found", 404));
  });

  render(<OsDeployCachePanel />);

  fireEvent.click(await screen.findByRole("button", { name: "Warm" }));

  await waitFor(() => { expect(warmCalls).toHaveLength(1); });
  expect(await screen.findByText(/Build dispatched to the build-host agent/u)).toBeInTheDocument();
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Warming..." })).toBeDisabled();
  });
});

test("surfaces a 409 hint when the build-host agent is not ready", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const { url, method } = requestMeta(input, init);
    if (url.endsWith("/api/osdeploy/v1/cache") && method === "GET") {
      return Promise.resolve(jsonResponse({ entries: [FACTORY_ENTRY] }));
    }
    if (method === "POST") {
      return Promise.resolve(jsonResponse({ detail: "preflight blocked" }, 409));
    }
    return Promise.resolve(jsonResponse("not found", 404));
  });

  render(<OsDeployCachePanel />);
  fireEvent.click(await screen.findByRole("button", { name: "Warm" }));

  expect(await screen.findByText(/Build-host agent is not ready/u)).toBeInTheDocument();
});

test("does not offer Warm for non-factory entries", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const { url, method } = requestMeta(input, init);
    if (url.endsWith("/api/osdeploy/v1/cache") && method === "GET") {
      return Promise.resolve(jsonResponse({
        entries: [{
          ...FACTORY_ENTRY,
          id: "http-entry",
          source_url: "https://example.test/server.wim",
          metadata: {}
        }]
      }));
    }
    return Promise.resolve(jsonResponse("not found", 404));
  });

  render(<OsDeployCachePanel />);
  expect(await screen.findByText(/Windows Server 2022 Datacenter/u)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Warm" })).not.toBeInTheDocument();
});
