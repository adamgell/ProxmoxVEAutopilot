export type Persona = "lab" | "msp" | "corp";

export type WizardStep = "welcome" | "identity" | "tenant" | "artifact" | "review";

export type PersistedStatus =
  | "pending"
  | "in_progress"
  | "launched"
  | "complete"
  | "aborted";

export interface Identity {
  readonly mode: "workgroup" | "ad";
  readonly adDomain: string | null;
  readonly adJoinAccount: string | null;
  readonly adJoinPasswordRef: { readonly ref: string | null; readonly isSet: boolean };
  readonly localAdminPasswordRef: { readonly ref: string | null; readonly isSet: boolean };
}

export interface Tenant {
  readonly skipped: boolean;
  readonly tenantId: string | null;
  readonly tenantDomain: string | null;
  readonly commentFile: string | null;
}

export interface Artifact {
  readonly kind: "cloudosd" | "osdeploy";
  readonly source: "existing" | "build";
  readonly existingArtifactId: string | null;
  readonly buildJobId: string | null;
}

export interface Trial {
  readonly vmName: string;
  readonly targetNode: string;
  readonly osEdition: "win11-pro" | "win11-ent" | "win10-pro";
}

export interface ProbeResult {
  readonly at: string;
  readonly ok: boolean;
  readonly detail: string;
}

export interface Answers {
  readonly schemaVersion: 1;
  readonly persona: Persona | null;
  readonly identity: Identity;
  readonly tenant: Tenant;
  readonly artifact: Artifact;
  readonly trial: Trial;
  readonly probeResults: {
    readonly ad: ProbeResult | null;
    readonly tenant: ProbeResult | null;
    readonly artifact: ProbeResult | null;
  };
}

export interface WizardState {
  readonly status: PersistedStatus;
  readonly currentStep: WizardStep;
  readonly answers: Answers;
  readonly launchedRunId: string | null;
  readonly etag: string | null;
}

export type WizardEvent =
  | { readonly type: "hydrate"; readonly state: WizardState }
  | { readonly type: "pickPersona"; readonly persona: Persona }
  | { readonly type: "patchAnswers"; readonly patch: Partial<Answers> }
  | { readonly type: "advance" }
  | { readonly type: "jumpTo"; readonly step: WizardStep }
  | { readonly type: "markLaunched"; readonly runId: string }
  | { readonly type: "markComplete" }
  | { readonly type: "discard" };

export const STEP_ORDER: readonly WizardStep[] = [
  "welcome",
  "identity",
  "tenant",
  "artifact",
  "review",
];

export function initialState(): WizardState {
  return {
    status: "in_progress",
    currentStep: "welcome",
    answers: {
      schemaVersion: 1,
      persona: null,
      identity: {
        mode: "workgroup",
        adDomain: null,
        adJoinAccount: null,
        adJoinPasswordRef: { ref: null, isSet: false },
        localAdminPasswordRef: { ref: null, isSet: false },
      },
      tenant: { skipped: true, tenantId: null, tenantDomain: null, commentFile: null },
      artifact: {
        kind: "cloudosd",
        source: "existing",
        existingArtifactId: null,
        buildJobId: null,
      },
      trial: { vmName: "", targetNode: "", osEdition: "win11-pro" },
      probeResults: { ad: null, tenant: null, artifact: null },
    },
    launchedRunId: null,
    etag: null,
  };
}
