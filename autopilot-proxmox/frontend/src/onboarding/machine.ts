import {
  STEP_ORDER,
  initialState,
  type Answers,
  type WizardEvent,
  type WizardState,
  type WizardStep,
} from "./types";

function nextStep(current: WizardStep): WizardStep {
  const idx = STEP_ORDER.indexOf(current);
  if (idx < 0 || idx >= STEP_ORDER.length - 1) {
    return current;
  }
  return STEP_ORDER[idx + 1]!;
}

function mergeAnswers(prev: Answers, patch: Partial<Answers>): Answers {
  return {
    ...prev,
    ...patch,
    identity: { ...prev.identity, ...(patch.identity ?? {}) },
    tenant: { ...prev.tenant, ...(patch.tenant ?? {}) },
    artifact: { ...prev.artifact, ...(patch.artifact ?? {}) },
    trial: { ...prev.trial, ...(patch.trial ?? {}) },
    probeResults: { ...prev.probeResults, ...(patch.probeResults ?? {}) },
  };
}

export function canAdvance(state: WizardState): boolean {
  if (state.status !== "in_progress" && state.status !== "pending") {
    return false;
  }
  switch (state.currentStep) {
    case "welcome":
      return state.answers.persona !== null;
    case "identity":
      if (state.answers.identity.mode === "workgroup") {
        return true;
      }
      return state.answers.probeResults.ad?.ok === true;
    case "tenant":
      return state.answers.tenant.skipped || state.answers.tenant.tenantId !== null;
    case "artifact":
      return (
        state.answers.artifact.source === "existing"
          ? state.answers.artifact.existingArtifactId !== null
          : state.answers.artifact.buildJobId !== null
      );
    case "review":
      return false;
    default:
      return false;
  }
}

function stepIndex(step: WizardStep): number {
  return STEP_ORDER.indexOf(step);
}

export function reduce(state: WizardState, event: WizardEvent): WizardState {
  // Once launched/complete/aborted, only hydrate is allowed.
  if (state.status === "launched" || state.status === "complete" || state.status === "aborted") {
    if (event.type === "hydrate") {
      return event.state;
    }
    if (event.type === "markComplete" && state.status === "launched") {
      return { ...state, status: "complete" };
    }
    if (event.type === "discard") {
      return { ...initialState(), status: "aborted" };
    }
    return state;
  }
  switch (event.type) {
    case "hydrate":
      return event.state;
    case "pickPersona":
      return { ...state, answers: { ...state.answers, persona: event.persona } };
    case "patchAnswers":
      return { ...state, answers: mergeAnswers(state.answers, event.patch) };
    case "advance":
      return canAdvance(state)
        ? { ...state, currentStep: nextStep(state.currentStep) }
        : state;
    case "jumpTo": {
      const targetIdx = stepIndex(event.step);
      const currentIdx = stepIndex(state.currentStep);
      if (targetIdx < 0 || targetIdx > currentIdx) {
        return state;
      }
      return { ...state, currentStep: event.step };
    }
    case "markLaunched":
      return { ...state, status: "launched", launchedRunId: event.runId };
    case "markComplete":
      return { ...state, status: "complete" };
    case "discard":
      return { ...initialState(), status: "aborted" };
  }
}
