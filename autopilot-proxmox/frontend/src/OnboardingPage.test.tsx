import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { OnboardingPage } from "./pages/OnboardingPage";
import type { AppBootstrap } from "./contracts";

const BOOT: AppBootstrap = {
  userName: "Tester",
  userEmail: "tester@example.com",
  onboarding: { status: "in_progress", currentStep: "welcome" },
};

beforeEach(() => {
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (url.endsWith("/api/onboarding/state")) {
      return new Response("", { status: 404 });
    }
    return new Response(JSON.stringify({}), { status: 200 });
  }) as typeof fetch;
});

afterEach(() => {
  cleanup();
});

describe("OnboardingPage", () => {
  it("renders the step rail with five steps", async () => {
    render(<OnboardingPage bootstrap={BOOT} />);
    await waitFor(() => {
      expect(screen.getByRole("navigation", { name: /onboarding steps/i })).toBeInTheDocument();
    });
    const nav = screen.getByRole("navigation", { name: /onboarding steps/i });
    expect(within(nav).getAllByRole("listitem")).toHaveLength(5);
  });

  it("welcome step asks for a persona", async () => {
    render(<OnboardingPage bootstrap={BOOT} />);
    await waitFor(() => {
      expect(screen.getByRole("radio", { name: /lab/i })).toBeInTheDocument();
      expect(screen.getByRole("radio", { name: /msp/i })).toBeInTheDocument();
      expect(screen.getByRole("radio", { name: /corp/i })).toBeInTheDocument();
    });
  });

  it("picking a persona enables the Next button", async () => {
    render(<OnboardingPage bootstrap={BOOT} />);
    await waitFor(() => screen.getByRole("radio", { name: /lab/i }));
    const next = screen.getByRole("button", { name: /next/i });
    expect(next).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: /lab/i }));
    await waitFor(() => expect(next).toBeEnabled());
  });

  it("renders the Discard onboarding link in the footer", async () => {
    render(<OnboardingPage bootstrap={BOOT} />);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /discard onboarding/i })).toBeInTheDocument();
    });
  });

  it("clicking Discard onboarding opens a confirmation dialog", async () => {
    render(<OnboardingPage bootstrap={BOOT} />);
    await waitFor(() => screen.getByRole("button", { name: /discard onboarding/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /discard onboarding/i }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-labelledby");
  });

  it("the welcome step button is marked aria-current=step", async () => {
    render(<OnboardingPage bootstrap={BOOT} />);
    const nav = await screen.findByRole("navigation", { name: /onboarding steps/i });
    const welcomeBtn = within(nav).getByRole("button", { name: /welcome/i });
    expect(welcomeBtn).toHaveAttribute("aria-current", "step");
  });
});
