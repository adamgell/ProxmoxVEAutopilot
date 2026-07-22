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

  it("advance progresses through STEP_ORDER (gates satisfied step by step)", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "lab" });
    // welcome -> identity (gate: persona set)
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("identity");
    // identity default mode=workgroup, so its gate passes immediately
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("tenant");
    // skip tenant so its gate passes
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    s = reduce(s, { type: "patchAnswers", patch: { tenant: { ...s.answers.tenant, skipped: true } } as any });
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("artifact");
    // pick an artifact so its gate passes
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    s = reduce(s, { type: "patchAnswers", patch: { artifact: { ...s.answers.artifact, existingArtifactId: "x-1" } } as any });
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("review");
  });

  it("advance on the last step is a no-op (launch is a separate event)", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "msp" });
    // welcome -> identity
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("identity");
    // identity -> tenant (workgroup mode passes immediately)
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("tenant");
    // skip tenant so its gate passes
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    s = reduce(s, { type: "patchAnswers", patch: { tenant: { ...s.answers.tenant, skipped: true } } as any });
    // tenant -> artifact
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("artifact");
    // pick an artifact so its gate passes
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    s = reduce(s, { type: "patchAnswers", patch: { artifact: { ...s.answers.artifact, existingArtifactId: "x-2" } } as any });
    // artifact -> review
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("review");
    // review advance is a no-op
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
