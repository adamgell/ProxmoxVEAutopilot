import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { TenantStep } from "./TenantStep";
import { initialState } from "../types";

afterEach(cleanup);

function tenantState(overrides?: {
  persona?: "lab" | "msp" | "corp";
  identityMode?: "workgroup" | "ad";
  skipped?: boolean;
}) {
  const base = initialState();
  return {
    ...base,
    answers: {
      ...base.answers,
      persona: overrides?.persona ?? "corp",
      identity: {
        ...base.answers.identity,
        mode: overrides?.identityMode ?? "ad",
      },
      tenant: {
        ...base.answers.tenant,
        skipped: overrides?.skipped ?? false,
        tenantId: "12345678-1234-1234-1234-123456789abc",
        tenantDomain: "contoso.onmicrosoft.com",
      },
    },
  };
}

describe("TenantStep runProbe error handling", () => {
  beforeEach(() => {
    (global as Record<string, unknown>).fetch = undefined;
  });

  it("non-2xx response surfaces an error without leaving the button locked", async () => {
    global.fetch = vi.fn(async () => new Response("", { status: 429 })) as typeof fetch;
    const onPatch = vi.fn();
    render(<TenantStep state={tenantState()} onPatch={onPatch} />);

    fireEvent.click(screen.getByRole("button", { name: /Test this now/i }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Test this now/i })).toBeEnabled());
    expect(screen.getByText(/Tenant invalid:.*HTTP 429/)).toBeInTheDocument();
  });

  it("fetch rejection surfaces an error without leaving the button locked", async () => {
    global.fetch = vi.fn(async () => { throw new Error("network down"); }) as typeof fetch;
    const onPatch = vi.fn();
    render(<TenantStep state={tenantState()} onPatch={onPatch} />);

    fireEvent.click(screen.getByRole("button", { name: /Test this now/i }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Test this now/i })).toBeEnabled());
    expect(screen.getByText(/Tenant invalid:.*network down/)).toBeInTheDocument();
  });
});

describe("TenantStep optional gating", () => {
  it("fieldset is disabled when isOptional is true and skipped is checked", () => {
    const state = tenantState({ persona: "lab", identityMode: "workgroup", skipped: true });
    const onPatch = vi.fn();
    render(<TenantStep state={state} onPatch={onPatch} />);

    const fieldset = screen.getByRole("group", { name: /AutopilotConfigurationFile.json/i });
    expect(fieldset).toBeDisabled();
  });

  it("fieldset is enabled when not optional even though skipped flag would be ignored", () => {
    const state = tenantState({ persona: "corp", identityMode: "ad", skipped: false });
    const onPatch = vi.fn();
    render(<TenantStep state={state} onPatch={onPatch} />);

    const fieldset = screen.getByRole("group", { name: /AutopilotConfigurationFile.json/i });
    expect(fieldset).not.toBeDisabled();
  });
});
