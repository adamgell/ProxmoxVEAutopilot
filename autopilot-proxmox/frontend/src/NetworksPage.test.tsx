import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { NetworksPage } from "./pages/NetworksPage";

function jsonRequestBody(init: RequestInit | undefined): unknown {
  if (typeof init?.body !== "string") {
    throw new Error("Expected a JSON request body");
  }
  return JSON.parse(init.body) as unknown;
}

describe("NetworksPage", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  test("shows SDN tabs, explicit apply, and open outbound egress default", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          sdn: {
            zones: [{ id: "lab-simple", type: "simple" }],
            vnets: [{ id: "lab101", zone: "lab-simple" }],
            subnets_by_vnet: { lab101: [{ subnet: "10.60.10.0/24", snat: true }] },
            controllers: [],
            ipams: [{ id: "pve", type: "pve" }],
            dns: [],
            fabrics: []
          },
          firewall: { cluster: { options: {}, rules: [] }, nodes: {}, vnets: {}, vms: {} },
          labs: []
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    render(<NetworksPage bootstrap={{ buildSha: "test" }} path="/react/networks" />);

    await waitFor(() => {
      expect(screen.getAllByText("lab-simple").length).toBeGreaterThan(0);
    });
    expect(screen.getByText("Pending Apply")).toBeInTheDocument();
    expect(screen.getByText("Outbound egress open by default")).toBeInTheDocument();
    expect(screen.getByText("Firewall")).toBeInTheDocument();
  });

  test("one-click apply hits /api/sdn/apply-pending without exposing a lock token", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (input === "/api/sdn/apply-pending") {
        expect(init?.method).toBe("POST");
        return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(
        JSON.stringify({
          sdn: { zones: [], vnets: [], subnets_by_vnet: {}, controllers: [], ipams: [], dns: [], fabrics: [] },
          firewall: { cluster: { options: {}, rules: [] }, nodes: {}, vnets: {}, vms: {} },
          labs: []
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      ));
    });

    render(<NetworksPage bootstrap={{ buildSha: "test" }} path="/react/networks" />);

    // Default flow: no token field, just one button.
    fireEvent.click(await screen.findByRole("button", { name: "Apply pending SDN changes" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/sdn/apply-pending", expect.objectContaining({ method: "POST" }));
    });
    expect(await screen.findByText("SDN apply complete. Inventory is refreshing.")).toBeInTheDocument();
  });

  test("creates an isolated lab with open outbound egress defaults", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (input === "/api/sdn/labs/preflight") {
        expect(jsonRequestBody(init)).toMatchObject({
          name: "Lab 101",
          zone: "lab-simple",
          vnet: "lab101",
          subnet: "10.60.10.0/24",
          egress_policy: "open",
          snat_enabled: true,
          firewall_profile: "isolated_open_egress"
        });
        return Promise.resolve(new Response(JSON.stringify({ ok: true, blocking: [], warnings: [] }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (input === "/api/sdn/labs") {
        expect(init?.method).toBe("POST");
        expect(jsonRequestBody(init)).toMatchObject({
          egress_policy: "open",
          snat_enabled: true,
          firewall_profile: "isolated_open_egress"
        });
        return Promise.resolve(new Response(JSON.stringify({ bubble: { id: "bubble-1" }, binding: { egress_policy: "open" } }), { status: 201, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(
        JSON.stringify({
          sdn: {
            zones: [{ id: "lab-simple", type: "simple" }],
            vnets: [{ id: "lab101", zone: "lab-simple" }],
            subnets_by_vnet: { lab101: [{ subnet: "10.60.10.0/24", snat: true }] },
            controllers: [],
            ipams: [{ id: "pve", type: "pve" }],
            dns: [],
            fabrics: []
          },
          firewall: { cluster: { options: {}, rules: [] }, nodes: {}, vnets: {}, vms: {} },
          labs: []
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      ));
    });

    render(<NetworksPage bootstrap={{ buildSha: "test" }} path="/react/networks" />);

    await waitFor(() => {
      expect(screen.getAllByText("lab-simple").length).toBeGreaterThan(0);
    });
    fireEvent.change(screen.getByLabelText("Lab name"), { target: { value: "Lab 101" } });
    fireEvent.click(screen.getByRole("button", { name: "Create isolated lab" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/sdn/labs", expect.objectContaining({ method: "POST" }));
    });
    expect(await screen.findByText("Lab saved with outbound egress open by default.")).toBeInTheDocument();
  });
});
