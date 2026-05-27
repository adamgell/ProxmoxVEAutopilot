import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { NetworksPage } from "./pages/NetworksPage";

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

  test("posts explicit SDN apply with the operator lock token", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((async (input, init) => {
      if (input === "/api/sdn/apply") {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({ lock_token: "digest-123" });
        return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(
        JSON.stringify({
          sdn: { zones: [], vnets: [], subnets_by_vnet: {}, controllers: [], ipams: [], dns: [], fabrics: [] },
          firewall: { cluster: { options: {}, rules: [] }, nodes: {}, vnets: {}, vms: {} },
          labs: []
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }) as typeof fetch);

    render(<NetworksPage bootstrap={{ buildSha: "test" }} path="/react/networks" />);

    fireEvent.change(await screen.findByLabelText("Lock token"), { target: { value: "digest-123" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply SDN" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/sdn/apply", expect.objectContaining({ method: "POST" }));
    });
    expect(await screen.findByText("SDN apply requested. Inventory is refreshing.")).toBeInTheDocument();
  });

  test("creates an isolated lab with open outbound egress defaults", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((async (input, init) => {
      if (input === "/api/sdn/labs/preflight") {
        expect(JSON.parse(String(init?.body))).toMatchObject({
          name: "Lab 101",
          zone: "lab-simple",
          vnet: "lab101",
          subnet: "10.60.10.0/24",
          egress_policy: "open",
          snat_enabled: true,
          firewall_profile: "isolated_open_egress"
        });
        return new Response(JSON.stringify({ ok: true, blocking: [], warnings: [] }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (input === "/api/sdn/labs") {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toMatchObject({
          egress_policy: "open",
          snat_enabled: true,
          firewall_profile: "isolated_open_egress"
        });
        return new Response(JSON.stringify({ bubble: { id: "bubble-1" }, binding: { egress_policy: "open" } }), { status: 201, headers: { "Content-Type": "application/json" } });
      }
      return new Response(
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
      );
    }) as typeof fetch);

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
