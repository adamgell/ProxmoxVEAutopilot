import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { OnboardingSetupPage } from "./pages/OnboardingSetupPage";
import type { AppBootstrap } from "./contracts";

afterEach(cleanup);

const BOOT: AppBootstrap = {
  userName: "Tester",
  onboarding: { status: "launched", currentStep: "review" },
};

beforeEach(() => {
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.endsWith("/api/onboarding/setup-status")) {
      return new Response(JSON.stringify({
        run_id: "onboarding-tester-1",
        phases: [
          { item_id: "validate", label: "Validate inputs", status: "ready", detail: "", sort_order: 10 },
          { item_id: "clone-template", label: "Clone template", status: "running", detail: "", sort_order: 30 },
          { item_id: "provision", label: "Provision", status: "pending", detail: "", sort_order: 50 },
        ],
      }), { status: 200 });
    }
    return new Response("", { status: 404 });
  }) as typeof fetch;
});

describe("OnboardingSetupPage", () => {
  it("renders the phase rail with status badges", async () => {
    render(<OnboardingSetupPage bootstrap={BOOT} />);
    await waitFor(() => {
      expect(screen.getByText("Validate inputs")).toBeInTheDocument();
      expect(screen.getByText("Clone template")).toBeInTheDocument();
      expect(screen.getByText("Provision")).toBeInTheDocument();
    });
  });

  it("shows the error message when the API returns 404", async () => {
    global.fetch = vi.fn(async () => new Response("", { status: 404 })) as typeof fetch;
    render(<OnboardingSetupPage bootstrap={BOOT} />);
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/no launched onboarding run found/i);
    });
  });

  it("renders the Setup complete card when every phase is ready or skipped", async () => {
    global.fetch = vi.fn(async () => new Response(JSON.stringify({
      run_id: "onboarding-tester-2",
      phases: [
        { item_id: "validate", label: "Validate inputs", status: "ready", detail: "", sort_order: 10 },
        { item_id: "provision", label: "Provision", status: "skipped", detail: "", sort_order: 30 },
      ],
    }), { status: 200 })) as typeof fetch;
    render(<OnboardingSetupPage bootstrap={BOOT} />);
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(/setup complete/i);
    });
  });

  it("renders the failed card with details for a failed phase", async () => {
    global.fetch = vi.fn(async () => new Response(JSON.stringify({
      run_id: "onboarding-tester-3",
      phases: [
        { item_id: "validate", label: "Validate inputs", status: "ready", detail: "", sort_order: 10 },
        { item_id: "clone-template", label: "Clone template", status: "failed", detail: "Template not found", sort_order: 30 },
      ],
    }), { status: 200 })) as typeof fetch;
    render(<OnboardingSetupPage bootstrap={BOOT} />);
    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent(/setup hit a snag/i);
      expect(alert).toHaveTextContent(/Template not found/);
    });
  });
});
