import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "./App";

const taskEnginePayload = {
  sequences: [
    {
      id: "seq-1",
      name: "CloudOSD deployment",
      description: "Desktop client baseline",
      enabled: true,
      target_os: "windows",
      step_count: 2,
      current_version_id: "ver-1",
      updated_at: "2026-05-20T12:30:00-04:00",
      steps: [
        { kind: "cloudosd_preflight", name: "Preflight", phase: "pe", content_refs: [] },
        { kind: "wait_agent_heartbeat", name: "Agent heartbeat", phase: "full_os", content_refs: ["agent-msi"] }
      ]
    }
  ],
  runs: [
    {
      id: "run-1",
      sequence_name: "CloudOSD deployment",
      sequence_version: 1,
      state: "running",
      phase: "full_os",
      vmid: 105,
      done_count: 1,
      running_count: 1,
      failed_count: 0,
      step_count: 2,
      manifest_count: 1,
      started_at: "2026-05-20T12:45:00-04:00"
    }
  ],
  cloudosd_runs: [
    {
      id: "run-1",
      sequence_name: "CloudOSD deployment",
      sequence_version: 1,
      state: "running",
      vmid: 105,
      computer_name: "Gell-EC41E7EB",
      serial_number: "EC41E7EB",
      done_count: 1,
      failed_count: 0,
      step_count: 2,
      steps: [
        { state: "done", name: "PE registered", kind: "pe_registered", phase: "pe" },
        { state: "running", name: "Agent heartbeat", kind: "wait_agent_heartbeat", phase: "full_os" }
      ]
    }
  ],
  content_items: [
    {
      id: "content-1",
      name: "AutopilotAgent",
      description: "Agent MSI",
      content_type: "msi",
      enabled: true,
      latest_version: {
        version: "0.1.10.0",
        source_uri: "/files/AutopilotAgent.msi"
      }
    }
  ],
  manifest_items: [
    {
      run_id: "run-1",
      sequence_name: "CloudOSD deployment",
      logical_name: "agent-msi",
      content_type: "msi",
      required_phase: "full_os",
      status: "resolved",
      source_uri: "/files/AutopilotAgent.msi"
    }
  ],
  flow_templates: [
    {
      id: "cloudosd-desktop",
      name: "OSDCloud Desktop Client",
      target_os: "windows",
      path: "OSDCloud",
      status: "primary desktop path",
      description: "Deploy a Windows desktop client.",
      step_count: 2,
      read_only: true,
      notes: ["Use this as the default Windows desktop client baseline."],
      nodes: [
        { name: "OSDCloud preflight", kind: "cloudosd_preflight", phase: "pe", retry_count: 0, retry_delay_seconds: 10, content_refs: [] },
        { name: "Wait agent heartbeat", kind: "wait_agent_heartbeat", phase: "full_os", retry_count: 60, retry_delay_seconds: 10, content_refs: ["agent-msi"] }
      ]
    }
  ]
};

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" }
  });
}

function parseJsonBody(body: BodyInit | null | undefined): Readonly<Record<string, unknown>> {
  expect(typeof body).toBe("string");
  const parsed = JSON.parse(body as string) as unknown;
  expect(parsed).toEqual(expect.any(Object));
  return parsed as Readonly<Record<string, unknown>>;
}

function recordValue(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Readonly<Record<string, unknown>> : {};
}

function mockFetch() {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/api/task-engine/page") {
      return Promise.resolve(response(taskEnginePayload));
    }
    if (url.pathname === "/api/task-engine/sequences/list/page") {
      return Promise.resolve(response({ ...taskEnginePayload, target_os_filter: url.searchParams.get("target_os") || "" }));
    }
    if (url.pathname === "/api/task-engine/sequences/templates/cloudosd-desktop/page") {
      return Promise.resolve(response({ template: taskEnginePayload.flow_templates[0] }));
    }
    if (url.pathname === "/api/task-engine/sequences/new/page") {
      return Promise.resolve(response({
        sequence: {
          id: null,
          name: "OSDCloud Desktop Client copy",
          description: "Deploy a Windows desktop client.",
          target_os: "windows",
          enabled: true
        },
        nodes: taskEnginePayload.flow_templates[0]?.nodes ?? [],
        step_templates: [
          {
            kind: "capture_autopilot_hash",
            label: "Capture Autopilot hardware hash",
            phase: "full_os",
            category: "Autopilot",
            description: "Capture hash",
            retry_count: 2,
            retry_delay_seconds: 20
          },
          {
            kind: "wait_agent_heartbeat",
            label: "Wait for AutopilotAgent heartbeat",
            phase: "full_os",
            category: "Autopilot",
            description: "Wait for agent"
          }
        ],
        flow_templates: taskEnginePayload.flow_templates,
        template_source: taskEnginePayload.flow_templates[0]
      }));
    }
    if (url.pathname === "/api/task-engine/sequences/seq-1/edit/page") {
      return Promise.resolve(response({
        sequence: taskEnginePayload.sequences[0],
        nodes: taskEnginePayload.sequences[0]?.steps ?? [],
        step_templates: [
          {
            kind: "capture_autopilot_hash",
            label: "Capture Autopilot hardware hash",
            phase: "full_os",
            category: "Autopilot",
            description: "Capture hash"
          }
        ],
        flow_templates: taskEnginePayload.flow_templates,
        template_source: null
      }));
    }
    if (url.pathname === "/api/osd/v2/builder/sequences" && init?.method === "POST") {
      return Promise.resolve(response({ id: "seq-new", current_version_id: "ver-new" }));
    }
    if (url.pathname === "/api/osd/v2/builder/sequences/seq-1" && init?.method === "PUT") {
      return Promise.resolve(response({ ok: true, id: "seq-1", current_version_id: "ver-2" }));
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

describe("TaskEnginePage", () => {
  test("renders the v2 task sequences overview with templates, runs, content, and no v1 controls", async () => {
    mockFetch();
    renderPath("/react/task-engine");

    expect(await screen.findByRole("heading", { name: "Task Sequences" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Library" })).toHaveAttribute("href", "/react/task-engine/sequences/list");
    expect(screen.getByRole("link", { name: "New sequence" })).toHaveAttribute("href", "/react/task-engine/sequences/new");
    expect(screen.queryByText("Import v1")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create v2 copy" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Read-only Flow Templates" })).toBeInTheDocument();
    expect(await screen.findByText("OSDCloud Desktop Client")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "V2 task sequences" })).toHaveTextContent("CloudOSD deployment");
    expect(screen.getByRole("table", { name: "V2 runs" })).toHaveTextContent("run-1");
    expect(screen.getByRole("table", { name: "Content library" })).toHaveTextContent("AutopilotAgent");
    expect(screen.getByRole("table", { name: "Content manifest" })).toHaveTextContent("agent-msi");
  });

  test("renders the sequence library with filter and read-only template actions", async () => {
    mockFetch();
    renderPath("/react/task-engine/sequences/list?target_os=windows");

    expect(await screen.findByRole("heading", { name: "V2 Sequence Library" })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Filter sequences and templates" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Target OS" })).toHaveValue("windows");
    expect(await screen.findByRole("table", { name: "Editable V2 sequences" })).toHaveTextContent("CloudOSD deployment");
    expect(screen.getByRole("heading", { name: "Read-only Flow Templates" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Inspect OSDCloud Desktop Client" })).toHaveAttribute("href", "/react/task-engine/sequences/templates/cloudosd-desktop");
    expect(screen.getByRole("link", { name: "Clone OSDCloud Desktop Client" })).toHaveAttribute("href", "/react/task-engine/sequences/new?template_id=cloudosd-desktop");
  });

  test("renders read-only template detail with notes and full step plan", async () => {
    mockFetch();
    renderPath("/react/task-engine/sequences/templates/cloudosd-desktop");

    expect(await screen.findByRole("heading", { name: "OSDCloud Desktop Client" })).toBeInTheDocument();
    expect(screen.getByText("primary desktop path")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Clone into builder" })).toHaveAttribute("href", "/react/task-engine/sequences/new?template_id=cloudosd-desktop");
    expect(screen.getByRole("table", { name: "Read-only step plan" })).toHaveTextContent("wait_agent_heartbeat");
    expect(within(screen.getByRole("list", { name: "Operator notes" })).getByText(/default Windows desktop/u)).toBeInTheDocument();
  });

  test("renders the new sequence builder and saves a compiled v2 sequence", async () => {
    const fetchMock = mockFetch();
    renderPath("/react/task-engine/sequences/new?template_id=cloudosd-desktop");

    expect(await screen.findByRole("heading", { name: "New v2 task sequence" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("OSDCloud Desktop Client copy");
    expect(screen.getByRole("heading", { name: "Phase Timeline" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Step Palette" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Capture Autopilot hardware hash/u })).toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "Desktop baseline copy" } });
    fireEvent.click(screen.getByRole("button", { name: /Save and compile/u }));

    await waitFor(() => expect(screen.getByText(/saved seq-new/u)).toBeInTheDocument());
    const saveCall = fetchMock.mock.calls.find(([input]) => input === "/api/osd/v2/builder/sequences");
    expect(saveCall?.[1]?.method).toBe("POST");
    expect(parseJsonBody(saveCall?.[1]?.body)).toEqual(expect.objectContaining({
      name: "Desktop baseline copy",
      target_os: "windows"
    }));
  });

  test("renders the edit sequence builder and updates an existing sequence", async () => {
    const fetchMock = mockFetch();
    renderPath("/react/task-engine/sequences/seq-1/edit");

    expect(await screen.findByRole("heading", { name: "Edit v2 sequence: CloudOSD deployment" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Agent heartbeat/u })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Capture Autopilot hardware hash/u }));
    expect(screen.getByRole("textbox", { name: "Step name" })).toHaveValue("Capture Autopilot hardware hash");

    fireEvent.change(screen.getByRole("spinbutton", { name: "Retry count" }), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: /Save and compile/u }));

    await waitFor(() => expect(screen.getByText(/saved seq-1/u)).toBeInTheDocument());
    const saveCall = fetchMock.mock.calls.find(([input]) => input === "/api/osd/v2/builder/sequences/seq-1");
    expect(saveCall?.[1]?.method).toBe("PUT");
    const saveBody = parseJsonBody(saveCall?.[1]?.body);
    expect(saveBody.name).toBe("CloudOSD deployment");
    expect(Array.isArray(saveBody.nodes)).toBe(true);
    const saveNodes = Array.isArray(saveBody.nodes) ? saveBody.nodes : [];
    expect(saveNodes.some((node) => recordValue(node).retry_count === 4)).toBe(true);
  });
});
