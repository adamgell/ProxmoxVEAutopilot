import { useState } from "react";
import type { WizardState } from "../types";

interface Props {
  readonly state: WizardState;
  readonly onPatch: (patch: Partial<WizardState["answers"]>) => void;
}

export function TenantStep({ state, onPatch }: Props) {
  const tenant = state.answers.tenant;
  const isOptional =
    state.answers.persona === "lab" && state.answers.identity.mode === "workgroup";
  const [probing, setProbing] = useState(false);
  const [probeResult, setProbeResult] = useState<{ ok: boolean; detail: string } | null>(
    state.answers.probeResults.tenant
  );

  async function runProbe() {
    if (!tenant.tenantId || !tenant.tenantDomain) return;
    setProbing(true);
    try {
      const r = await fetch("/api/onboarding/probe/tenant", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tenant_id: tenant.tenantId,
          tenant_domain: tenant.tenantDomain,
          graph_check: true,
        }),
      });
      if (!r.ok) {
        const detail = `Probe request failed (HTTP ${r.status})`;
        setProbeResult({ ok: false, detail });
        onPatch({
          probeResults: { ...state.answers.probeResults, tenant: { at: new Date().toISOString(), ok: false, detail } },
        });
        return;
      }
      const body = await r.json();
      setProbeResult({ ok: body.ok, detail: body.detail });
      onPatch({
        probeResults: { ...state.answers.probeResults, tenant: { at: new Date().toISOString(), ok: body.ok, detail: body.detail } },
      });
    } catch (e) {
      const detail = `Probe request failed: ${(e as Error).message}`;
      setProbeResult({ ok: false, detail });
      onPatch({
        probeResults: { ...state.answers.probeResults, tenant: { at: new Date().toISOString(), ok: false, detail } },
      });
    } finally {
      setProbing(false);
    }
  }

  return (
    <section className="onboarding-step" aria-labelledby="tenant-h">
      <h1 id="tenant-h">Tenant {isOptional ? <small>(optional for lab + workgroup)</small> : null}</h1>
      <p>Without real tenant values the OOBE join phase will fail in production. Workgroup trials don't need this.</p>
      {isOptional ? (
        <label>
          <input
            type="checkbox"
            checked={tenant.skipped}
            onChange={(e) => onPatch({ tenant: { ...tenant, skipped: e.target.checked } })}
          />
          Skip tenant setup for now
        </label>
      ) : null}
      <fieldset disabled={tenant.skipped}>
        <legend>AutopilotConfigurationFile.json</legend>
        <label>
          CloudAssignedTenantId
          <input
            type="text"
            value={tenant.tenantId ?? ""}
            onChange={(e) => onPatch({ tenant: { ...tenant, tenantId: e.target.value } })}
            placeholder="12345678-1234-1234-1234-123456789abc"
          />
        </label>
        <label>
          CloudAssignedTenantDomain
          <input
            type="text"
            value={tenant.tenantDomain ?? ""}
            onChange={(e) => onPatch({ tenant: { ...tenant, tenantDomain: e.target.value } })}
            placeholder="contoso.onmicrosoft.com"
          />
        </label>
        <label>
          Comment_File
          <input
            type="text"
            value={tenant.commentFile ?? ""}
            onChange={(e) => onPatch({ tenant: { ...tenant, commentFile: e.target.value } })}
          />
        </label>
        <button type="button" onClick={() => void runProbe()} disabled={probing}>
          {probing ? "Testing..." : "Test this now"}
        </button>
        {probeResult ? (
          <p role={probeResult.ok ? "status" : "alert"} aria-live="polite">
            {probeResult.ok ? "Tenant validates: " : "Tenant invalid: "} {probeResult.detail}
          </p>
        ) : null}
      </fieldset>
      <details>
        <summary>What if it fails?</summary>
        <ul>
          <li>Graph creds missing. Open <a href="/react/settings">Settings &gt; Entra</a> and add an app secret with Directory.Read.All.</li>
          <li>Tenant id format invalid. Check the value in https://entra.microsoft.com under Overview.</li>
        </ul>
      </details>
    </section>
  );
}
