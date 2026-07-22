import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { ReviewLaunchStep } from "./ReviewLaunchStep";
import { initialState, type WizardState } from "../types";

afterEach(cleanup);

function populated(): WizardState {
  const base = initialState();
  return {
    ...base,
    answers: {
      ...base.answers,
      persona: "corp",
      identity: {
        ...base.answers.identity,
        mode: "ad",
        adDomain: "home.gell.one",
        adJoinAccount: "joiner",
      },
      tenant: {
        ...base.answers.tenant,
        skipped: false,
        tenantId: "00000000-0000-0000-0000-000000000001",
        tenantDomain: "contoso.onmicrosoft.com",
      },
      artifact: {
        ...base.answers.artifact,
        kind: "cloudosd",
        source: "existing",
        existingArtifactId: "cosd-1",
      },
      trial: {
        vmName: "autopilot-trial-9001",
        targetNode: "pve2",
        osEdition: "win11-pro",
      },
      probeResults: {
        ...base.answers.probeResults,
        ad: { at: "2026-05-27T00:00:00Z", ok: true, detail: "bound ok" },
      },
    },
  };
}

describe("ReviewLaunchStep render", () => {
  it("renders the review dl with the right values for a fully-populated state", () => {
    render(
      <ReviewLaunchStep
        state={populated()}
        onPatch={vi.fn()}
        onJump={vi.fn()}
        onLaunch={vi.fn(async () => undefined)}
      />,
    );
    expect(screen.getByText("corp")).toBeInTheDocument();
    expect(screen.getByText(/AD-joined to home.gell.one/)).toBeInTheDocument();
    expect(screen.getByText("00000000-0000-0000-0000-000000000001")).toBeInTheDocument();
    expect(screen.getByText(/Use cloudosd cosd-1/)).toBeInTheDocument();
  });
});

describe("ReviewLaunchStep preconditions", () => {
  it("disables Start setup when preconditions fail", () => {
    const base = initialState();
    // persona is null, so preconditions should flag it.
    render(
      <ReviewLaunchStep
        state={base}
        onPatch={vi.fn()}
        onJump={vi.fn()}
        onLaunch={vi.fn(async () => undefined)}
      />,
    );
    const btn = screen.getByRole("button", { name: /Start setup/i });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/Pick a persona on the Welcome step/)).toBeInTheDocument();
  });

  it("enables Start setup when fully populated and calls onLaunch on click", async () => {
    const onLaunch = vi.fn(async () => undefined);
    render(
      <ReviewLaunchStep
        state={populated()}
        onPatch={vi.fn()}
        onJump={vi.fn()}
        onLaunch={onLaunch}
      />,
    );
    const btn = screen.getByRole("button", { name: /Start setup/i });
    expect(btn).toBeEnabled();
    fireEvent.click(btn);
    await waitFor(() => expect(onLaunch).toHaveBeenCalledTimes(1));
  });
});

describe("ReviewLaunchStep launch error surfacing", () => {
  it("renders launch error in role=alert when onLaunch rejects", async () => {
    const onLaunch = vi.fn(async () => {
      throw new Error("Launch failed (HTTP 500)");
    });
    render(
      <ReviewLaunchStep
        state={populated()}
        onPatch={vi.fn()}
        onJump={vi.fn()}
        onLaunch={onLaunch}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Start setup/i }));
    await waitFor(() => {
      expect(screen.getByText(/Launch failed \(HTTP 500\)/)).toBeInTheDocument();
    });
    // Button should be re-enabled after failure.
    expect(screen.getByRole("button", { name: /Start setup/i })).toBeEnabled();
  });
});
