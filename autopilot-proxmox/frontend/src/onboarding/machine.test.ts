import { describe, it, expect } from "vitest";
import { reduce, canAdvance } from "./machine";
import { initialState, STEP_ORDER, type WizardState } from "./types";

describe("onboarding reducer", () => {
  it("starts at welcome step with no persona", () => {
    const s = initialState();
    expect(s.currentStep).toBe("welcome");
    expect(s.answers.persona).toBeNull();
  });

  it("pickPersona sets the persona", () => {
    const s = reduce(initialState(), { type: "pickPersona", persona: "lab" });
    expect(s.answers.persona).toBe("lab");
  });

  it("canAdvance(welcome) requires persona", () => {
    expect(canAdvance(initialState())).toBe(false);
    const withPersona = reduce(initialState(), { type: "pickPersona", persona: "lab" });
    expect(canAdvance(withPersona)).toBe(true);
  });

  it("advance progresses through STEP_ORDER", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "lab" });
    for (let i = 0; i < STEP_ORDER.length - 1; i++) {
      s = reduce(s, { type: "advance" });
      expect(s.currentStep).toBe(STEP_ORDER[i + 1]);
    }
  });

  it("advance on the last step is a no-op (launch is a separate event)", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "msp" });
    for (let i = 0; i < STEP_ORDER.length - 1; i++) {
      s = reduce(s, { type: "advance" });
    }
    expect(s.currentStep).toBe("review");
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("review");
  });

  it("jumpTo moves backward but not forward past current", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "lab" });
    s = reduce(s, { type: "advance" });
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("tenant");
    s = reduce(s, { type: "jumpTo", step: "identity" });
    expect(s.currentStep).toBe("identity");
    s = reduce(s, { type: "jumpTo", step: "review" });
    expect(s.currentStep).toBe("identity"); // cannot jump past current_step
  });

  it("markLaunched flips status and stores run id; current_step freezes", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "corp" });
    s = reduce(s, { type: "markLaunched", runId: "onboarding-x-1" });
    expect(s.status).toBe("launched");
    expect(s.launchedRunId).toBe("onboarding-x-1");
    const before = s.currentStep;
    s = reduce(s, { type: "advance" }); // no-op when launched
    expect(s.currentStep).toBe(before);
  });

  it("hydrate replaces the state wholesale", () => {
    const seed: WizardState = {
      ...initialState(),
      currentStep: "artifact",
      status: "in_progress",
      etag: 'W/"2026-05-27T00:00:00.000Z"',
    };
    const s = reduce(initialState(), { type: "hydrate", state: seed });
    expect(s.currentStep).toBe("artifact");
    expect(s.etag).toBe('W/"2026-05-27T00:00:00.000Z"');
  });

  it("identity step gate: workgroup advances; ad without bind probe blocks", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "corp" });
    s = reduce(s, { type: "advance" }); // -> identity
    expect(canAdvance(s)).toBe(true); // default mode is workgroup
    s = reduce(s, {
      type: "patchAnswers",
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      patch: { identity: { ...s.answers.identity, mode: "ad" } as any },
    });
    expect(canAdvance(s)).toBe(false);
    s = reduce(s, {
      type: "patchAnswers",
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      patch: {
        probeResults: { ...s.answers.probeResults, ad: { at: "now", ok: true, detail: "ok" } } as any,
      },
    });
    expect(canAdvance(s)).toBe(true);
  });
});
