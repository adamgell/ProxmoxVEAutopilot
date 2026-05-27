import { useEffect, useReducer, useState } from "react";
import type { AppBootstrap } from "../contracts";
import { reduce } from "../onboarding/machine";
import {
  initialState,
  STEP_ORDER,
  type WizardEvent,
  type WizardState,
  type WizardStep,
  type Persona,
} from "../onboarding/types";
import { StepRail } from "../onboarding/StepRail";
import { AlreadyConfiguredCard } from "../onboarding/AlreadyConfiguredCard";
import { WelcomePersonaStep } from "../onboarding/steps/WelcomePersonaStep";
import {
  fetchState,
  putState,
  deleteState,
  PreconditionFailedError,
  PreconditionRequiredError,
} from "../onboarding/persistence";

function reducer(state: WizardState, event: WizardEvent): WizardState {
  return reduce(state, event);
}

interface Props {
  readonly bootstrap: AppBootstrap;
}

export function OnboardingPage({ bootstrap: _bootstrap }: Props) {
  const [state, dispatch] = useReducer(reducer, initialState());
  const [hydrated, setHydrated] = useState(false);
  const [discardConfirmOpen, setDiscardConfirmOpen] = useState(false);

  // Hydrate on mount.
  useEffect(() => {
    void (async () => {
      const result = await fetchState();
      if (result) {
        dispatch({
          type: "hydrate",
          state: {
            status: result.row.status,
            currentStep: result.row.current_step,
            answers: { ...initialState().answers, ...result.row.answers, persona: result.row.persona },
            launchedRunId: result.row.launched_run_id,
            etag: result.etag,
          },
        });
      }
      setHydrated(true);
    })();
  }, []);

  // Redirect once launched/complete.
  useEffect(() => {
    if (!hydrated) return;
    if (state.status === "launched") {
      window.location.href = "/react/onboarding/setup";
    }
    if (state.status === "complete") {
      window.location.href = "/react-shell";
    }
  }, [hydrated, state.status]);

  const optionalSteps: ReadonlySet<WizardStep> = new Set(
    state.answers.persona === "lab" && state.answers.identity.mode === "workgroup" ? ["tenant" as WizardStep] : [],
  );

  async function persist(patch: Record<string, unknown>) {
    try {
      const result = await putState({ patch }, state.etag);
      dispatch({
        type: "hydrate",
        state: {
          ...state,
          ...(result.row as Partial<WizardState>),
          etag: result.etag,
          answers: { ...state.answers, ...(result.row.answers as object) },
        },
      });
    } catch (e) {
      if (e instanceof PreconditionFailedError || e instanceof PreconditionRequiredError) {
        const fresh = await fetchState();
        if (fresh) {
          dispatch({
            type: "hydrate",
            state: {
              ...state,
              ...(fresh.row as Partial<WizardState>),
              etag: fresh.etag,
              answers: { ...state.answers, ...(fresh.row.answers as object) },
            },
          });
        }
      }
      // Otherwise swallow; banner UX comes in a later refinement.
    }
  }

  function onPickPersona(persona: Persona) {
    dispatch({ type: "pickPersona", persona });
    void persist({ persona });
  }

  function onAdvance() {
    // If the current step has any locally-held secrets (IdentityStep, for now),
    // give it a chance to flush them into the next state PUT.
    (window as unknown as { __onboardingIdentityFlush?: () => void }).__onboardingIdentityFlush?.();
    const nextIdx = STEP_ORDER.indexOf(state.currentStep) + 1;
    const next = STEP_ORDER[nextIdx];
    dispatch({ type: "advance" });
    if (next) {
      void persist({ current_step: next });
    }
  }

  function onJump(step: WizardStep) {
    dispatch({ type: "jumpTo", step });
    void persist({ current_step: step });
  }

  async function onDiscard() {
    await deleteState();
    window.location.href = "/react-shell";
  }

  return (
    <main className="onboarding-page">
      <StepRail steps={STEP_ORDER} current={state.currentStep} optional={optionalSteps} onJump={onJump} />
      {state.currentStep === "welcome" ? (
        <>
          <AlreadyConfiguredCard />
          <WelcomePersonaStep state={state} onPickPersona={onPickPersona} />
        </>
      ) : (
        // Subsequent step components land in Tasks 7-10.
        <section><p>Step {state.currentStep} pending implementation.</p></section>
      )}
      <footer className="onboarding-footer">
        <button type="button" onClick={onAdvance} disabled={!canAdvanceLocal(state)}>
          Next
        </button>
        <button
          type="button"
          className="onboarding-discard"
          onClick={() => setDiscardConfirmOpen(true)}
          disabled={state.status === "launched"}
          title={state.status === "launched" ? "Cannot discard mid-launch. Abort the run from /react/jobs first." : undefined}
        >
          Discard onboarding
        </button>
      </footer>
      {discardConfirmOpen ? (
        <div role="dialog" aria-modal="true" aria-labelledby="discard-confirm-h">
          <h2 id="discard-confirm-h">Discard your onboarding progress?</h2>
          <p>This wipes your wizard answers. Any setup run already kicked off keeps running; abort it from /react/jobs if needed.</p>
          <button onClick={() => void onDiscard()}>Yes, discard</button>
          <button onClick={() => setDiscardConfirmOpen(false)}>Cancel</button>
        </div>
      ) : null}
    </main>
  );
}

function canAdvanceLocal(state: WizardState): boolean {
  // Inlined import to avoid circular-name concerns; same logic as machine.canAdvance.
  if (state.currentStep === "welcome") {
    return state.answers.persona !== null;
  }
  return true;
}
