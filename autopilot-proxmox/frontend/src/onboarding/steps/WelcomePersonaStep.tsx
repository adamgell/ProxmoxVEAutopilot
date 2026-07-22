import type { Persona, WizardState } from "../types";

interface Props {
  readonly state: WizardState;
  readonly onPickPersona: (persona: Persona) => void;
}

export function WelcomePersonaStep({ state, onPickPersona }: Props) {
  const choices: { value: Persona; label: string; help: string }[] = [
    {
      value: "lab",
      label: "Lab hobbyist",
      help: "Homelab. Workgroup defaults, build CloudOSD locally, controller's own node for the trial.",
    },
    {
      value: "msp",
      label: "MSP technician",
      help: "Onboarding a customer. AD-joined defaults, tenant required, reuse existing artifact, auto-pick first node.",
    },
    {
      value: "corp",
      label: "Corporate IT",
      help: "First-time setup at scale. AD-joined defaults, tenant required, reuse existing artifact, explicit node prompt.",
    },
  ];
  return (
    <section className="onboarding-step">
      <h1>Welcome</h1>
      <p>Pick the lane closest to how you'll use this controller. We'll pre-fill sensible defaults; every field stays editable.</p>
      <fieldset>
        <legend>Which lane describes you?</legend>
        {choices.map((c) => (
          <label key={c.value}>
            <input
              type="radio"
              name="persona"
              value={c.value}
              checked={state.answers.persona === c.value}
              onChange={() => onPickPersona(c.value)}
            />
            <strong>{c.label}</strong>
            <span className="help">{c.help}</span>
          </label>
        ))}
      </fieldset>
    </section>
  );
}
