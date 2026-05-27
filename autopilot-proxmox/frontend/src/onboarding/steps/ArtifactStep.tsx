import { useEffect, useState } from "react";
import type { WizardState } from "../types";

interface ArtifactSummary {
  readonly id: string;
  readonly label?: string;
  readonly built_at?: string;
  readonly [key: string]: unknown;
}

interface ProbeResponse {
  readonly ok: boolean;
  readonly detail: string;
  readonly cloudosd: ArtifactSummary[];
  readonly osdeploy: ArtifactSummary[];
}

interface Props {
  readonly state: WizardState;
  readonly onPatch: (patch: Partial<WizardState["answers"]>) => void;
}

type BuildResumeState =
  | { readonly kind: "idle" }
  | { readonly kind: "polling" }
  | { readonly kind: "running"; readonly status: string }
  | { readonly kind: "done" }
  | { readonly kind: "failed"; readonly detail: string }
  | { readonly kind: "missing" };

// Build endpoints. CloudOSD lives under /api/cloudosd, OSDeploy under
// /api/osdeploy/v1 (note the v1). Both return {"ok": true, "job_id": "..."};
// OSDeploy may also include "job_type" but we only care about job_id here.
const BUILD_ENDPOINT: Readonly<Record<"cloudosd" | "osdeploy", string>> = {
  cloudosd: "/api/cloudosd/artifacts/build",
  osdeploy: "/api/osdeploy/v1/artifacts/build",
};

export function ArtifactStep({ state, onPatch }: Props) {
  const artifact = state.answers.artifact;
  const [inventory, setInventory] = useState<ProbeResponse | null>(null);
  const [inventoryError, setInventoryError] = useState<string | null>(null);
  const [inventoryLoading, setInventoryLoading] = useState(true);
  const [building, setBuilding] = useState(false);
  const [buildError, setBuildError] = useState<string | null>(null);
  const [resume, setResume] = useState<BuildResumeState>({ kind: "idle" });

  // Inventory fetch: post to /api/onboarding/probe/artifact on mount.
  useEffect(() => {
    const ac = new AbortController();
    (async () => {
      setInventoryLoading(true);
      setInventoryError(null);
      try {
        const r = await fetch("/api/onboarding/probe/artifact", {
          method: "POST",
          credentials: "include",
          signal: ac.signal,
        });
        if (!r.ok) {
          setInventoryError(`Could not load artifact inventory (HTTP ${r.status}).`);
          return;
        }
        const body = (await r.json()) as ProbeResponse;
        setInventory(body);
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setInventoryError(`Could not load artifact inventory: ${(e as Error).message}`);
      } finally {
        setInventoryLoading(false);
      }
    })();
    return () => ac.abort();
  }, []);

  // Build-resume: if there's a buildJobId, ask the jobs API for its status.
  // /api/jobs/{id} returns 200 with {"error": "not found"} for unknown ids
  // (it does NOT 404), so we have to inspect the body, not just the status.
  useEffect(() => {
    if (!artifact.buildJobId) {
      setResume({ kind: "idle" });
      return;
    }
    const jobId = artifact.buildJobId;
    const ac = new AbortController();
    setResume({ kind: "polling" });
    (async () => {
      try {
        const r = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
          method: "GET",
          credentials: "include",
          signal: ac.signal,
        });
        if (!r.ok) {
          setResume({ kind: "failed", detail: `Could not query the build job (HTTP ${r.status}).` });
          return;
        }
        const body = (await r.json()) as Record<string, unknown>;
        if (body.error === "not found") {
          // The job was pruned or never existed; clear the stale id.
          setResume({ kind: "missing" });
          onPatch({ artifact: { ...artifact, buildJobId: null } });
          return;
        }
        const status = String(body.status ?? body.state ?? "").toLowerCase();
        if (status === "success" || status === "succeeded" || status === "complete" || status === "completed") {
          setResume({ kind: "done" });
        } else if (status === "failed" || status === "error" || status === "killed" || status === "cancelled") {
          const detail = typeof body.error === "string" && body.error ? body.error : `Build job ended as ${status}.`;
          setResume({ kind: "failed", detail });
        } else {
          setResume({ kind: "running", status: status || "in progress" });
        }
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setResume({ kind: "failed", detail: `Could not query the build job: ${(e as Error).message}` });
      }
    })();
    return () => ac.abort();
  }, [artifact.buildJobId]);

  async function kickBuild() {
    if (building) return;
    setBuilding(true);
    setBuildError(null);
    try {
      const url = BUILD_ENDPOINT[artifact.kind];
      const r = await fetch(url, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        // Empty body: both ArtifactBuildBody models have defaults for every field.
        body: JSON.stringify({}),
      });
      if (r.status !== 200 && r.status !== 202) {
        const text = await r.text().catch(() => "");
        setBuildError(`Build request failed (HTTP ${r.status})${text ? `: ${text.slice(0, 200)}` : ""}`);
        return;
      }
      const body = (await r.json()) as Record<string, unknown>;
      const jobId = body.job_id;
      if (typeof jobId !== "string" || !jobId) {
        setBuildError("Build endpoint did not return a job_id.");
        return;
      }
      onPatch({ artifact: { ...artifact, buildJobId: jobId } });
    } catch (e) {
      setBuildError(`Build request failed: ${(e as Error).message}`);
    } finally {
      setBuilding(false);
    }
  }

  const available = artifact.kind === "cloudosd" ? inventory?.cloudosd ?? [] : inventory?.osdeploy ?? [];

  return (
    <section className="onboarding-step" aria-labelledby="artifact-h">
      <h1 id="artifact-h">Artifact</h1>
      <p>
        Pick a built CloudOSD or OSDeploy artifact, or kick a fresh build. Builds run on the configured remote
        build host and stream their log to /react/jobs.
      </p>
      <fieldset>
        <legend>Artifact kind</legend>
        <label>
          <input
            type="radio"
            checked={artifact.kind === "cloudosd"}
            onChange={() =>
              onPatch({ artifact: { ...artifact, kind: "cloudosd", existingArtifactId: null, buildJobId: null } })
            }
          />
          CloudOSD
        </label>
        <label>
          <input
            type="radio"
            checked={artifact.kind === "osdeploy"}
            onChange={() =>
              onPatch({ artifact: { ...artifact, kind: "osdeploy", existingArtifactId: null, buildJobId: null } })
            }
          />
          OSDeploy
        </label>
      </fieldset>
      <fieldset>
        <legend>Source</legend>
        <label>
          <input
            type="radio"
            checked={artifact.source === "existing"}
            onChange={() => onPatch({ artifact: { ...artifact, source: "existing" } })}
          />
          Pick an existing artifact
        </label>
        <label>
          <input
            type="radio"
            checked={artifact.source === "build"}
            onChange={() => onPatch({ artifact: { ...artifact, source: "build" } })}
          />
          Kick a build
        </label>
      </fieldset>
      {inventoryLoading ? (
        <p aria-live="polite">Loading artifact inventory...</p>
      ) : inventoryError ? (
        <p role="alert">{inventoryError}</p>
      ) : artifact.source === "existing" ? (
        <fieldset>
          <legend>Pick an artifact</legend>
          {available.length === 0 ? (
            <p>No {artifact.kind} artifacts yet. Switch to "Kick a build" or build one from the dedicated page.</p>
          ) : (
            <label>
              Artifact
              <select
                value={artifact.existingArtifactId ?? ""}
                onChange={(e) =>
                  onPatch({ artifact: { ...artifact, existingArtifactId: e.target.value || null } })
                }
              >
                <option value="">-- choose --</option>
                {available.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.label ?? a.id} {a.built_at ? `(built ${a.built_at})` : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
        </fieldset>
      ) : (
        <fieldset>
          <legend>Kick a build</legend>
          {artifact.buildJobId ? (
            <>
              <p>
                Build job <code>{artifact.buildJobId}</code>{" "}
                {resume.kind === "polling"
                  ? "is being looked up..."
                  : resume.kind === "running"
                  ? `is ${resume.status}.`
                  : resume.kind === "done"
                  ? "completed."
                  : resume.kind === "failed"
                  ? `failed: ${resume.detail}`
                  : resume.kind === "missing"
                  ? "was not found; it may have been pruned."
                  : ""}
              </p>
              {resume.kind === "missing" ? (
                <p role="alert">The previous build job is gone. Kick a new build to continue.</p>
              ) : null}
              <p>
                Watch the live log at{" "}
                <a href={`/jobs/${encodeURIComponent(artifact.buildJobId)}`} target="_blank" rel="noopener">
                  /jobs/{artifact.buildJobId}
                </a>
                .
              </p>
            </>
          ) : (
            <p>No build has been kicked yet for this onboarding session.</p>
          )}
          <button type="button" onClick={() => void kickBuild()} disabled={building}>
            {building ? "Kicking..." : artifact.buildJobId ? "Kick another build" : "Kick a build"}
          </button>
          {buildError ? (
            <p role="alert" aria-live="polite">
              {buildError}
            </p>
          ) : null}
        </fieldset>
      )}
      <details>
        <summary>What if it fails?</summary>
        <ul>
          <li>
            Inventory request failed. The probe at /api/onboarding/probe/artifact returns the same data the
            CloudOSD / OSDeploy pages use; check /react/cloudosd or /react/osdeploy directly.
          </li>
          <li>
            Build returned a non-2xx. Open the build page (/react/cloudosd or /react/osdeploy) which surfaces
            preflight failures with full detail; the wizard only kicks builds with defaults.
          </li>
          <li>
            Build job missing. The job log was pruned (jobs/ retention) or the run was never enqueued; kick a
            new build from this step.
          </li>
        </ul>
      </details>
    </section>
  );
}
