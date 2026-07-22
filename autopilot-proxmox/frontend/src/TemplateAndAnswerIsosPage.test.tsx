import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const templatePayload = {
  hypervisor_type: "proxmox",
  utm_iso_dir: "~/UTM-ISOs",
  profiles: {
    surface: {
      manufacturer: "Microsoft",
      product: "Surface Pro"
    },
    dell: {
      manufacturer: "Dell",
      product: "Latitude"
    }
  },
  ubuntu_sequences: [
    {
      id: 8,
      name: "Ubuntu Desktop",
      target_os: "ubuntu"
    }
  ]
};

const answerPayload = {
  rows: [
    {
      hash: "abc123",
      short_hash: "abc123",
      volid: "local:snippets/autopilot-unattend-abc123.img",
      compiled_at: "2026-05-20T11:12:00-04:00",
      last_used_at: "2026-05-20T11:30:00-04:00",
      in_use: true
    },
    {
      hash: "def456",
      short_hash: "def456",
      volid: "local:snippets/autopilot-unattend-def456.img",
      compiled_at: "2026-05-20T10:00:00-04:00",
      last_used_at: null,
      in_use: false
    }
  ],
  error: ""
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" }
  });
}

function mockFetch(payload = templatePayload) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/api/template/page") {
      return Promise.resolve(jsonResponse(payload));
    }
    if (url.pathname === "/api/answer-isos/page") {
      return Promise.resolve(jsonResponse(answerPayload));
    }
    if (url.pathname === "/api/answer-iso/rebuild" && init?.method === "POST") {
      return Promise.resolve(jsonResponse({ ok: true, path: "local:iso/answer.iso", bytes: 4096 }));
    }
    if (url.pathname === "/api/jobs/template" && init?.method === "POST") {
      return Promise.resolve(jsonResponse({ ok: true, job_id: "job-template" }));
    }
    if (url.pathname === "/api/ubuntu/rebuild-seed-iso" && init?.method === "POST") {
      expect(url.searchParams.get("sequence_id")).toBe("8");
      return Promise.resolve(jsonResponse({ ok: true, iso: "local:iso/ubuntu-seed.iso" }));
    }
    if (url.pathname === "/api/ubuntu/build-template" && init?.method === "POST") {
      expect(url.searchParams.get("sequence_id")).toBe("8");
      return Promise.resolve(jsonResponse({ ok: true, job_id: "job-ubuntu" }));
    }
    if (url.pathname === "/api/answer-isos/prune" && init?.method === "POST") {
      return Promise.resolve(jsonResponse({ removed: ["def456"] }));
    }
    return Promise.resolve(new Response("not found", { status: 404 }));
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

describe("TemplatePage", () => {
  test("renders Windows and Ubuntu template builder controls instead of a generic payload table", async () => {
    const fetchMock = mockFetch();
    renderPath("/react/template");

    expect(await screen.findByRole("heading", { name: "Build Template" })).toBeInTheDocument();
    const windowsModeButton = await screen.findByRole("button", { name: "Windows" });
    expect(windowsModeButton).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Ubuntu" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "OEM Profile" })).toHaveValue("surface");
    expect(screen.getByRole("checkbox", { name: "Pause before sysprep" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rebuild Answer ISO" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Build Template" })).toBeInTheDocument();
    expect(screen.queryByText("ubuntu_sequences")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Ubuntu" }));

    expect(screen.getByRole("button", { name: "Ubuntu" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("combobox", { name: "Sequence" })).toHaveValue("8");
    expect(screen.getByRole("button", { name: "Rebuild Ubuntu Seed ISO" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Build Ubuntu Template" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Rebuild Ubuntu Seed ISO" }));
    await waitFor(() => expect(screen.getByText(/local:iso\/ubuntu-seed.iso/u)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Build Ubuntu Template" }));
    await waitFor(() => expect(screen.getByText(/job-ubuntu/u)).toBeInTheDocument());

    const ubuntuCall = fetchMock.mock.calls.find(([input]) => typeof input === "string" && input.startsWith("/api/ubuntu/build-template"));
    expect(ubuntuCall?.[1]?.method).toBe("POST");
  });

  test("submits the Windows template form as FormData with JSON accept headers", async () => {
    const fetchMock = mockFetch();
    renderPath("/react/template");

    expect(await screen.findByRole("combobox", { name: "OEM Profile" })).toHaveValue("surface");
    fireEvent.click(screen.getByRole("checkbox", { name: "Pause before sysprep" }));
    fireEvent.submit(screen.getByTestId("windows-template-form"));

    await waitFor(() => expect(screen.getByText(/job-template/u)).toBeInTheDocument());
    const templateCall = fetchMock.mock.calls.find(([input]) => input === "/api/jobs/template");
    expect(templateCall?.[1]?.method).toBe("POST");
    expect(templateCall?.[1]?.body).toBeInstanceOf(FormData);
    expect(new Headers(templateCall?.[1]?.headers).get("accept")).toBe("application/json");
  });

  test("renders the UTM template path when the host is configured for UTM", async () => {
    mockFetch({
      ...templatePayload,
      hypervisor_type: "utm"
    });
    renderPath("/react/template");

    expect(await screen.findByRole("heading", { name: "UTM Template Builder" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "OS" })).toHaveValue("windows11");
    expect(screen.getByRole("textbox", { name: "Template Name" })).toHaveValue("win11-template");
    expect(screen.getByRole("spinbutton", { name: "CPU Cores" })).toHaveValue(4);
    expect(screen.getByRole("button", { name: "Build UTM Template" })).toBeInTheDocument();
  });
});

describe("AnswerIsosPage", () => {
  test("renders selectable answer floppy cache rows and prunes only unused selections", async () => {
    const fetchMock = mockFetch();
    renderPath("/react/answer-isos");

    expect(await screen.findByRole("heading", { name: "Answer ISO Cache" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Answer ISO cache" });
    expect(within(table).getByText("abc123")).toBeInTheDocument();
    expect(within(table).getByText("def456")).toBeInTheDocument();
    expect(within(table).getByRole("checkbox", { name: "Select def456" })).toBeEnabled();
    expect(within(table).getByRole("checkbox", { name: "Select abc123" })).toBeDisabled();

    fireEvent.click(within(table).getByRole("checkbox", { name: "Select def456" }));
    fireEvent.click(screen.getByRole("button", { name: "Prune selected" }));

    await waitFor(() => expect(screen.getByText(/removed 1/u)).toBeInTheDocument());
    const pruneCall = fetchMock.mock.calls.find(([input]) => input === "/api/answer-isos/prune");
    expect(pruneCall?.[1]?.method).toBe("POST");
    const body = pruneCall?.[1]?.body;
    expect(typeof body).toBe("string");
    expect(JSON.parse(typeof body === "string" ? body : "{}")).toEqual({ hashes: ["def456"] });
  });
});
