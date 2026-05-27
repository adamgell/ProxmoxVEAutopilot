import { useEffect, useReducer, useState } from "react";
import type { AppBootstrap } from "../contracts";
import { reduce, canAdvance } from "../onboarding/machine";
import {
  initialState,
  STEP_ORDER,
  type WizardStep,
  type Persona,
  type PersistedStatus,
  type Answers,
} from "../onboarding/types";
import { StepRail } from "../onboarding/StepRail";
import { AlreadyConfiguredCard } from "../onboarding/AlreadyConfiguredCard";
import { WelcomePersonaStep } from "../onboarding/steps/WelcomePersonaStep";
import { IdentityStep } from "../onboarding/steps/IdentityStep";
import { TenantStep } from "../onboarding/steps/TenantStep";
import { ArtifactStep } from "../onboarding/steps/ArtifactStep";
import {
  fetchState,
  putState,
  deleteState,
  PreconditionFailedError,
  PreconditionRequiredError,
} from "../onboarding/persistence";

type WireRow = {
  readonly status: PersistedStatus;
  readonly current_step: WizardStep;
  readonly persona: Persona | null;
  readonly launched_run_id: string | null;
  readonly answers: Partial<Answers>;
};

interface Props {
  readonly bootstrap: AppBootstrap;
}

export function OnboardingPage(_props: Props) {
  const [state, dispatch] = useReducer(reduce, initialState());
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
    } else if (state.status === "complete") {
      window.location.href = "/react-shell";
    }
  }, [hydrated, state.status]);

  const optionalSteps: ReadonlySet<WizardStep> = new Set(
    state.answers.persona === "lab" && state.answers.identity.mode === "workgroup" ? ["tenant"] : [],
  );

  async function persist(patch: Record<string, unknown>) {
    try {
      const result = await putState({ patch }, state.etag);
      const row = result.row as WireRow;
      dispatch({
        type: "hydrate",
        state: {
          status: row.status,
          currentStep: row.current_step,
          answers: { ...state.answers, ...row.answers, persona: row.persona },
          launchedRunId: row.launched_run_id,
          etag: result.etag,
        },
      });
    } catch (e) {
      if (e instanceof PreconditionFailedError || e instanceof PreconditionRequiredError) {
        const fresh = await fetchState();
        if (fresh) {
          const row = fresh.row as WireRow;
          dispatch({
            type: "hydrate",
            state: {
              status: row.status,
              currentStep: row.current_step,
              answers: { ...state.answers, ...row.answers, persona: row.persona },
              launchedRunId: row.launched_run_id,
              etag: fresh.etag,
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
      ) : state.currentStep === "identity" ? (
        <IdentityStep
          state={state}
          onPatch={(patch) => {
            dispatch({ type: "patchAnswers", patch });
            void persist({ answers: patch });
          }}
        />
      ) : state.currentStep === "tenant" ? (
        <TenantStep
          state={state}
          onPatch={(patch) => {
            dispatch({ type: "patchAnswers", patch });
            void persist({ answers: patch });
          }}
        />
      ) : state.currentStep === "artifact" ? (
        <ArtifactStep
          state={state}
          onPatch={(patch) => {
            dispatch({ type: "patchAnswers", patch });
            void persist({ answers: patch });
          }}
        />
      ) : (
        // Review step lands in Task 10.
        <section><p>Step {state.currentStep} pending implementation.</p></section>
      )}
      <footer className="onboarding-footer">
        <button type="button" onClick={onAdvance} disabled={!canAdvance(state)}>
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
