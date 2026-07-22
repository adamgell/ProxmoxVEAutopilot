import type { WizardStep } from "./types";

const LABELS: Record<WizardStep, string> = {
  welcome: "Welcome",
  identity: "Identity",
  tenant: "Tenant",
  artifact: "Artifact",
  review: "Review",
};

interface StepRailProps {
  readonly steps: readonly WizardStep[];
  readonly current: WizardStep;
  readonly optional: ReadonlySet<WizardStep>;
  readonly onJump: (step: WizardStep) => void;
}

export function StepRail({ steps, current, optional, onJump }: StepRailProps) {
  return (
    <nav aria-label="Onboarding steps" className="onboarding-step-rail">
      <ol>
        {steps.map((step, idx) => {
          const isCurrent = step === current;
          return (
            <li key={step}>
              <button
                type="button"
                className={isCurrent ? "step-current" : "step-other"}
                aria-current={isCurrent ? "step" : undefined}
                onClick={() => onJump(step)}
              >
                <span className="step-number">{idx + 1}</span>
                <span className="step-label">{LABELS[step]}</span>
                {optional.has(step) ? <span className="step-optional">optional</span> : null}
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
