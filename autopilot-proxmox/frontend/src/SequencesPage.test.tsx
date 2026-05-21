import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const sequence = {
  id: 1,
  name: "Legacy baseline",
  description: "Windows OOBE sequence",
  target_os: "windows",
  step_count: 1,
  steps: [{ kind: "rename_computer", phase: "oobe" }],
  is_default: true,
  produces_autopilot_hash: true,
  hash_capture_phase: "oobe",
  winpe_action_kinds: ["partition_disk"],
  updated_at: "2026-05-20T12:00:00-04:00"
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
    if (url.pathname === "/api/sequences/page") {
      return Promise.resolve(response({ sequences: [sequence], error: "" }));
    }
    if (url.pathname === "/api/sequences/new/page") {
      return Promise.resolve(response({ sequence: null, seq: null, oem_profiles: [] }));
    }
    if (url.pathname === "/api/sequences/1/edit/page") {
      return Promise.resolve(response({ sequence, seq: sequence, oem_profiles: [] }));
    }
    if (url.pathname === "/api/sequences" && init?.method === "POST") {
      return Promise.resolve(response({ id: 2 }));
    }
    if (url.pathname === "/api/sequences/1" && init?.method === "PUT") {
      return Promise.resolve(response({ ok: true }));
    }
    if (url.pathname === "/api/sequences/1/duplicate" && init?.method === "POST") {
      return Promise.resolve(response({ id: 3 }));
    }
    if (url.pathname === "/api/sequences/1" && init?.method === "DELETE") {
      return Promise.resolve(response({ ok: true }));
    }
    return Promise.resolve(response({}));
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

describe("SequencesPage", () => {
  test("renders legacy sequence list with filter and duplicate action", async () => {
    const fetchMock = mockFetch();
    renderPath("/react/sequences");

    expect(await screen.findByRole("heading", { name: "Task Sequences" })).toBeInTheDocument();
    expect((await screen.findAllByText("Legacy baseline")).length).toBeGreaterThan(0);
    expect(screen.getByRole("table", { name: "Task sequences" })).toHaveTextContent("partition_disk");

    fireEvent.click(screen.getByRole("button", { name: /Duplicate/u }));
    await waitFor(() => expect(screen.getByText("sequence duplicated")).toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([input, init]) => input === "/api/sequences/1/duplicate" && init?.method === "POST")).toBe(true);
  });

  test("renders new sequence form and posts JSON", async () => {
    const fetchMock = mockFetch();
    renderPath("/react/sequences/new");

    expect(await screen.findByRole("heading", { name: "New Sequence" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "New baseline" } });
    fireEvent.click(screen.getByRole("button", { name: /Save/u }));

    await waitFor(() => expect(screen.getByText("created 2")).toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([input, init]) => input === "/api/sequences" && init?.method === "POST")).toBe(true);
  });

  test("renders edit sequence form and updates JSON", async () => {
    const fetchMock = mockFetch();
    renderPath("/react/sequences/1/edit");

    expect(await screen.findByRole("heading", { name: "Edit Sequence 1" })).toBeInTheDocument();
    expect(await screen.findByDisplayValue("Legacy baseline")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Sequence step preview" })).toHaveTextContent("rename_computer");

    fireEvent.click(screen.getByRole("button", { name: /Save/u }));
    await waitFor(() => expect(screen.getByText("saved 1")).toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([input, init]) => input === "/api/sequences/1" && init?.method === "PUT")).toBe(true);
  });
});
