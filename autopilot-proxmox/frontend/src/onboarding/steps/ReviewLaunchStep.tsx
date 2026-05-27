import { useState } from "react";
import type { WizardState, WizardStep } from "../types";

interface Props {
  readonly state: WizardState;
  readonly onPatch: (patch: Partial<WizardState["answers"]>) => void;
  readonly onJump: (step: WizardStep) => void;
  readonly onLaunch: () => Promise<void>;
}

function preconditions(state: WizardState): string[] {
  const errs: string[] = [];
  if (state.answers.persona === null) errs.push("Pick a persona on the Welcome step.");
  if (
    state.answers.identity.mode === "ad" &&
    state.answers.probeResults.ad?.ok !== true
  ) {
    errs.push("Identity says AD-joined but no successful AD probe is on record.");
  }
  if (
    !(state.answers.persona === "lab" && state.answers.identity.mode === "workgroup") &&
    !state.answers.tenant.skipped &&
    !state.answers.tenant.tenantId
  ) {
    errs.push("Tenant is required for your persona / identity combination.");
  }
  if (
    state.answers.artifact.source === "existing" &&
    !state.answers.artifact.existingArtifactId
  ) {
    errs.push("Pick an existing artifact on the Artifact step.");
  }
  if (
    state.answers.artifact.source === "build" &&
    !state.answers.artifact.buildJobId
  ) {
    errs.push("Kick a build on the Artifact step.");
  }
  if (!state.answers.trial.targetNode) {
    errs.push("Pick a target node for the trial VM.");
  }
  return errs;
}

export function ReviewLaunchStep({ state, onPatch, onJump, onLaunch }: Props) {
  const errs = preconditions(state);
  const trial = state.answers.trial;
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState<string | null>(null);
  return (
    <section className="onboarding-step" aria-labelledby="review-h">
      <h1 id="review-h">Review and launch</h1>
      <p>Last chance to fix anything before we touch the live cluster.</p>

      <fieldset>
        <legend>Trial VM</legend>
        <label>
          VM name
          <input
            type="text"
            value={trial.vmName}
            onChange={(e) => onPatch({ trial: { ...trial, vmName: e.target.value } })}
            placeholder="autopilot-trial-<vmid>"
          />
        </label>
        <label>
          Target node
          <input
            type="text"
            value={trial.targetNode}
            onChange={(e) => onPatch({ trial: { ...trial, targetNode: e.target.value } })}
            placeholder="pve2"
          />
        </label>
        <label>
          OS edition
          <select
            value={trial.osEdition}
            onChange={(e) =>
              onPatch({
                trial: {
                  ...trial,
                  osEdition: e.target.value as WizardState["answers"]["trial"]["osEdition"],
                },
              })
            }
          >
            <option value="win11-pro">Windows 11 Pro</option>
            <option value="win11-ent">Windows 11 Enterprise</option>
            <option value="win10-pro">Windows 10 Pro</option>
          </select>
        </label>
      </fieldset>

      <dl className="onboarding-review">
        <dt>Persona</dt>
        <dd>
          {state.answers.persona ?? "(not picked)"}{" "}
          <button type="button" onClick={() => onJump("welcome")}>edit</button>
        </dd>
        <dt>Identity</dt>
        <dd>
          {state.answers.identity.mode === "workgroup"
            ? "Workgroup"
            : `AD-joined to ${state.answers.identity.adDomain ?? "(no domain)"}`}{" "}
          <button type="button" onClick={() => onJump("identity")}>edit</button>
        </dd>
        <dt>Tenant</dt>
        <dd>
          {state.answers.tenant.skipped ? "Skipped" : state.answers.tenant.tenantId ?? "(unset)"}{" "}
          <button type="button" onClick={() => onJump("tenant")}>edit</button>
        </dd>
        <dt>Artifact</dt>
        <dd>
          {state.answers.artifact.source === "existing"
            ? `Use ${state.answers.artifact.kind} ${state.answers.artifact.existingArtifactId ?? "(unset)"}`
            : `Build ${state.answers.artifact.kind} (job ${state.answers.artifact.buildJobId ?? "pending"})`}{" "}
          <button type="button" onClick={() => onJump("artifact")}>edit</button>
        </dd>
      </dl>

      <button
        type="button"
        disabled={errs.length > 0 || launching}
        title={errs.length > 0 ? errs.join(" / ") : undefined}
        onClick={async () => {
          setLaunching(true);
          setLaunchError(null);
          try {
            await onLaunch();
          } catch (e) {
            setLaunchError((e as Error).message);
          } finally {
            setLaunching(false);
          }
        }}
      >
        {launching ? "Launching..." : "Start setup"}
      </button>
      {launchError !== null ? <p role="alert">{launchError}</p> : null}
      {errs.length > 0 ? (
        <ul role="alert">
          {errs.map((e) => <li key={e}>{e}</li>)}
        </ul>
      ) : null}
    </section>
  );
}
