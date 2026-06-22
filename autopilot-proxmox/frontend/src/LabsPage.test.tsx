import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AppBootstrap } from "./contracts";
import { LabsPage } from "./pages/LabsPage";

const bootstrap: AppBootstrap = {
  buildSha: "dev",
  buildTime: "2026-06-21T00:00:00Z",
  userName: "Adam",
  userEmail: "adam@example.test"
};

describe("LabsPage", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders lab state, findings, fixes, and timeline", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (path.includes("/api/labs/page")) {
        return Promise.resolve(new Response(JSON.stringify({
          labs: [{ id: "lab-1", name: "NTT Lab", short_code: "ntt01", group_tag: "NTT-Lab", status: "blocked", network_cidr: "10.50.20.0/24" }],
          selected_lab: { id: "lab-1", name: "NTT Lab", short_code: "ntt01", group_tag: "NTT-Lab", status: "blocked", network_cidr: "10.50.20.0/24" },
          findings: [{ id: "finding-1", finding_type: "sdn_zone_missing", severity: "fixable", detail: "SDN zone lab-ntt01 is missing." }],
          fix_actions: [{ id: "fix-1", action_type: "create_sdn_zone", status: "pending", detail: "Create SDN zone lab-ntt01." }],
          events: [{ id: "1", event_type: "lab_created", detail: "Created lab NTT Lab", created_at: "2026-06-21T00:00:00Z" }],
          boundaries: [{
            id: "boundary-1",
            provider: "proxmox",
            kind: "network",
            name: "NTT SDN",
            ownership: "managed",
            source: "created",
            desired_state: { zone: "lab-ntt01", vnet: "ntt01-vnet" },
            actual_state: { zone: "lab-ntt01", vnet: "ntt01-vnet" }
          }],
          boundary_objects: [{
            id: "boundary-object-1",
            boundary_id: "boundary-1",
            provider: "proxmox",
            kind: "sdn_zone",
            name: "lab-ntt01",
            ownership: "managed",
            source: "created",
            provider_ids: { zone: "lab-ntt01" },
            desired_state: { type: "simple", zone: "lab-ntt01" },
            actual_state: { type: "simple", zone: "lab-ntt01" }
          }],
          reservations: [],
          reconcile_runs: []
        }), { status: 200, headers: { "content-type": "application/json" } }));
      }
      return Promise.resolve(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    });

    render(<LabsPage bootstrap={bootstrap} />);

    expect(await screen.findByRole("heading", { name: "Labs" })).toBeVisible();
    expect(screen.getAllByText("NTT Lab").length).toBeGreaterThan(0);
    expect(screen.getByText("SDN zone lab-ntt01 is missing.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Run pending fixes" })).toBeVisible();
    expect(screen.getByText("Created lab NTT Lab")).toBeVisible();
    expect(screen.getByRole("table", { name: "Boundary current state" })).toHaveTextContent("NTT SDN");
    expect(screen.getByRole("table", { name: "Boundary object current state" })).toHaveTextContent("lab-ntt01");
  });

  it("selects a non-default lab from the roster", async () => {
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      calls.push(path);
      const selectedLabId = path.includes("selected_lab_id=lab-2") ? "lab-2" : "lab-1";
      const labs = [
        { id: "lab-1", name: "Default Lab", short_code: "def01", group_tag: "DEF-Lab", status: "blocked", network_cidr: "10.50.20.0/24" },
        { id: "lab-2", name: "Other Lab", short_code: "oth01", group_tag: "OTH-Lab", status: "ready", network_cidr: "10.51.20.0/24" }
      ];
      return Promise.resolve(new Response(JSON.stringify({
        labs,
        selected_lab: labs.find((lab) => lab.id === selectedLabId),
        findings: [],
        fix_actions: [],
        events: [],
        boundaries: [],
        boundary_objects: [],
        reservations: [],
        reconcile_runs: []
      }), { status: 200, headers: { "content-type": "application/json" } }));
    });

    render(<LabsPage bootstrap={bootstrap} />);

    fireEvent.click(await screen.findByRole("button", { name: "Select Other Lab" }));

    await waitFor(() => {
      expect(calls.some((call) => call.endsWith("/api/labs/page?selected_lab_id=lab-2"))).toBe(true);
    });
    expect(screen.getAllByText("OTH-Lab").length).toBeGreaterThan(0);
  });


  it("creates a lab with default naming and network fields", async () => {
    const calls: Array<{ url: string; body?: unknown }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      calls.push({ url, body: typeof init?.body === "string" ? JSON.parse(init.body) as unknown : undefined });
      if (url.includes("/api/labs/page")) {
        return Promise.resolve(new Response(JSON.stringify({
          labs: [{ id: "lab-1", name: "NTT Lab", short_code: "ntt01", group_tag: "NTT-Lab", status: "draft", network_cidr: "10.50.20.0/24" }],
          selected_lab: { id: "lab-1", name: "NTT Lab", short_code: "ntt01", group_tag: "NTT-Lab", status: "draft", network_cidr: "10.50.20.0/24" },
          findings: [],
          fix_actions: [],
          events: [],
          boundaries: [],
          boundary_objects: [],
          reservations: [],
          reconcile_runs: []
        }), { status: 200, headers: { "content-type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({ id: "lab-1", name: "NTT Lab" }), { status: 201, headers: { "content-type": "application/json" } }));
    });

    render(<LabsPage bootstrap={bootstrap} />);

    fireEvent.change(await screen.findByLabelText("Lab name"), { target: { value: "NTT Lab" } });
    fireEvent.change(screen.getByLabelText("Short code"), { target: { value: "ntt01" } });
    fireEvent.change(screen.getByLabelText("Group tag"), { target: { value: "NTT-Lab" } });
    fireEvent.change(screen.getByLabelText("Subnet CIDR"), { target: { value: "10.50.20.0/24" } });
    fireEvent.change(screen.getByLabelText("Gateway IP"), { target: { value: "10.50.20.1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create lab" }));

    await waitFor(() => {
      expect(calls.some((call) => call.url.endsWith("/api/labs"))).toBe(true);
    });
    expect(calls.find((call) => call.url.endsWith("/api/labs"))?.body).toMatchObject({
      name: "NTT Lab",
      short_code: "ntt01",
      group_tag: "NTT-Lab",
      network_cidr: "10.50.20.0/24",
      gateway_ip: "10.50.20.1"
    });
  });

  it("selects a non-default lab and refetches the page with selected_lab_id", async () => {
    window.history.pushState({}, "", "/react/labs");
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      calls.push(url);
      if (url.includes("selected_lab_id=lab-2")) {
        return Promise.resolve(new Response(JSON.stringify({
          labs: [
            { id: "lab-1", name: "Newest Lab", short_code: "new01", group_tag: "NEW-Lab", status: "ready", network_cidr: "10.60.10.0/24" },
            { id: "lab-2", name: "Older Lab", short_code: "old01", group_tag: "OLD-Lab", status: "blocked", network_cidr: "10.60.11.0/24" }
          ],
          selected_lab: { id: "lab-2", name: "Older Lab", short_code: "old01", group_tag: "OLD-Lab", status: "blocked", network_cidr: "10.60.11.0/24" },
          findings: [],
          fix_actions: [],
          events: [],
          boundaries: [],
          boundary_objects: [],
          reservations: [],
          reconcile_runs: []
        }), { status: 200, headers: { "content-type": "application/json" } }));
      }
      if (url.includes("/api/labs/page")) {
        return Promise.resolve(new Response(JSON.stringify({
          labs: [
            { id: "lab-1", name: "Newest Lab", short_code: "new01", group_tag: "NEW-Lab", status: "ready", network_cidr: "10.60.10.0/24" },
            { id: "lab-2", name: "Older Lab", short_code: "old01", group_tag: "OLD-Lab", status: "blocked", network_cidr: "10.60.11.0/24" }
          ],
          selected_lab: { id: "lab-1", name: "Newest Lab", short_code: "new01", group_tag: "NEW-Lab", status: "ready", network_cidr: "10.60.10.0/24" },
          findings: [],
          fix_actions: [],
          events: [],
          boundaries: [],
          boundary_objects: [],
          reservations: [],
          reconcile_runs: []
        }), { status: 200, headers: { "content-type": "application/json" } }));
      }
      return Promise.resolve(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    });

    render(<LabsPage bootstrap={bootstrap} />);

    expect(await screen.findByRole("heading", { name: "Labs" })).toBeVisible();
    fireEvent.click(await screen.findByRole("button", { name: "Select Older Lab" }));

    await waitFor(() => {
      expect(calls.some((url) => url.includes("/api/labs/page?selected_lab_id=lab-2"))).toBe(true);
    });
    expect(await screen.findByRole("heading", { name: "Older Lab" })).toBeVisible();
    expect(window.location.search).toContain("selected_lab_id=lab-2");
  });

});
