import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { IdentityStep } from "./IdentityStep";
import { initialState } from "../types";

afterEach(cleanup);

function adState() {
  const base = initialState();
  return {
    ...base,
    answers: {
      ...base.answers,
      identity: {
        ...base.answers.identity,
        mode: "ad" as const,
        adDomain: "home.gell.one",
        adJoinAccount: "svc-autopilot",
      },
    },
  };
}

describe("IdentityStep runProbe error handling", () => {
  beforeEach(() => {
    (global as Record<string, unknown>).fetch = undefined;
  });

  it("non-2xx response surfaces an error without leaving the button locked", async () => {
    global.fetch = vi.fn(async () => new Response("", { status: 429 })) as typeof fetch;
    const onPatch = vi.fn();
    render(<IdentityStep state={adState()} onPatch={onPatch} />);

    fireEvent.change(screen.getByLabelText(/Join password/i), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: /Test this now/i }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Test this now/i })).toBeEnabled());
    expect(screen.getByText(/Probe failed:.*HTTP 429/)).toBeInTheDocument();
  });

  it("fetch rejection surfaces an error without leaving the button locked", async () => {
    global.fetch = vi.fn(async () => { throw new Error("network down"); }) as typeof fetch;
    const onPatch = vi.fn();
    render(<IdentityStep state={adState()} onPatch={onPatch} />);

    fireEvent.change(screen.getByLabelText(/Join password/i), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: /Test this now/i }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Test this now/i })).toBeEnabled());
    expect(screen.getByText(/Probe failed:.*network down/)).toBeInTheDocument();
  });
});
