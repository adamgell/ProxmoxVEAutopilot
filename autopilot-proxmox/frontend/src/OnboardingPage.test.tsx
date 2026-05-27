import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { OnboardingPage } from "./pages/OnboardingPage";
import type { AppBootstrap } from "./contracts";

const BOOT: AppBootstrap = {
  userName: "Tester",
  userEmail: "tester@example.com",
  onboarding: { status: "in_progress", currentStep: "welcome" },
};

beforeEach(() => {
  global.fetch = vi.fn(async (url: string) => {
    if (url.endsWith("/api/onboarding/state")) {
      return new Response("", { status: 404 });
    }
    return new Response(JSON.stringify({}), { status: 200 });
  }) as any;
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
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
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
});
