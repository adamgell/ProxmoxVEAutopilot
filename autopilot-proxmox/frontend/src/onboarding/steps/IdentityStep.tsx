import { useState } from "react";
import type { WizardState } from "../types";

interface Props {
  readonly state: WizardState;
  readonly onPatch: (patch: Partial<WizardState["answers"]>) => void;
}

export function IdentityStep({ state, onPatch }: Props) {
  const identity = state.answers.identity;
  // Passwords live in local component state, NOT in the wizard machine state.
  // This avoids PUTting partial passwords on every keystroke and keeps the
  // plaintext value out of any serialized state until the operator submits.
  // The values are flushed to the server (and vaulted) exactly twice:
  //   - on "Test this now" click (sent inline in the probe request body)
  //   - on "Next" click (included in the state PUT; _intake_secrets vaults it)
  const [adPassword, setAdPassword] = useState("");
  const [localAdminPassword, setLocalAdminPassword] = useState("");
  const [probing, setProbing] = useState(false);
  const [probeResult, setProbeResult] = useState<{ ok: boolean; detail: string } | null>(
    state.answers.probeResults.ad
  );

  async function runProbe() {
    if (!identity.adDomain || !identity.adJoinAccount || !adPassword) {
      return;
    }
    setProbing(true);
    try {
      const r = await fetch("/api/onboarding/probe/ad", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          domain: identity.adDomain,
          account: identity.adJoinAccount,
          password: adPassword,
        }),
      });
      if (!r.ok) {
        const detail = `Probe request failed (HTTP ${r.status})`;
        setProbeResult({ ok: false, detail });
        onPatch({
          probeResults: { ...state.answers.probeResults, ad: { at: new Date().toISOString(), ok: false, detail } },
        });
        return;
      }
      const body = await r.json();
      setProbeResult({ ok: body.ok, detail: body.detail });
      onPatch({
        probeResults: { ...state.answers.probeResults, ad: { at: new Date().toISOString(), ok: body.ok, detail: body.detail } },
      });
    } catch (e) {
      const detail = `Probe request failed: ${(e as Error).message}`;
      setProbeResult({ ok: false, detail });
      onPatch({
        probeResults: { ...state.answers.probeResults, ad: { at: new Date().toISOString(), ok: false, detail } },
      });
    } finally {
      setProbing(false);
    }
  }

  function flushPasswordsBeforeAdvance() {
    const flush: Record<string, unknown> = {};
    if (adPassword) flush.ad_join_password = adPassword;
    if (localAdminPassword) flush.local_admin_password = localAdminPassword;
    if (Object.keys(flush).length === 0) return;
    onPatch({ identity: { ...identity, ...flush } as any });
    setAdPassword("");
    setLocalAdminPassword("");
  }
  // Expose to the parent via a stable property on window for the simple wiring;
  // a cleaner version uses useImperativeHandle. The parent OnboardingPage calls
  // `(window as any).__onboardingIdentityFlush?.()` from the Next button handler
  // before calling persist.
  (window as any).__onboardingIdentityFlush = flushPasswordsBeforeAdvance;

  return (
    <section className="onboarding-step" aria-labelledby="identity-h">
      <h1 id="identity-h">Identity</h1>
      <p>Workgroup is faster to test. AD-joined requires a reachable domain controller.</p>
      <fieldset>
        <legend>Join mode</legend>
        <label>
          <input
            type="radio"
            checked={identity.mode === "workgroup"}
            onChange={() => onPatch({ identity: { ...identity, mode: "workgroup" } })}
          />
          Workgroup
        </label>
        <label>
          <input
            type="radio"
            checked={identity.mode === "ad"}
            onChange={() => onPatch({ identity: { ...identity, mode: "ad" } })}
          />
          AD-joined
        </label>
      </fieldset>
      {identity.mode === "ad" ? (
        <fieldset>
          <legend>Active Directory</legend>
          <label>
            Domain
            <input
              type="text"
              value={identity.adDomain ?? ""}
              onChange={(e) => onPatch({ identity: { ...identity, adDomain: e.target.value } })}
              placeholder="home.gell.one"
            />
          </label>
          <label>
            Join account
            <input
              type="text"
              value={identity.adJoinAccount ?? ""}
              onChange={(e) => onPatch({ identity: { ...identity, adJoinAccount: e.target.value } })}
              placeholder="svc-autopilot"
            />
          </label>
          <label>
            Join password
            <input
              type="password"
              value={adPassword}
              placeholder={identity.adJoinPasswordRef.isSet ? "(set; type to replace)" : ""}
              onChange={(e) => setAdPassword(e.target.value)}
            />
          </label>
          <label>
            Local admin password
            <input
              type="password"
              value={localAdminPassword}
              placeholder={identity.localAdminPasswordRef.isSet ? "(set; type to replace)" : ""}
              onChange={(e) => setLocalAdminPassword(e.target.value)}
            />
          </label>
          <button type="button" onClick={() => void runProbe()} disabled={probing}>
            {probing ? "Testing..." : "Test this now"}
          </button>
          {probeResult ? (
            <p role={probeResult.ok ? "status" : "alert"} aria-live="polite">
              {probeResult.ok ? "Probe succeeded: " : "Probe failed: "} {probeResult.detail}
            </p>
          ) : null}
          <details>
            <summary>What if it fails?</summary>
            <ul>
              <li>DNS does not resolve the domain. Open Settings &gt; DNS and confirm your forwarder includes a domain controller.</li>
              <li>LDAP bind refused. The join account exists but cannot read the directory. Grant it 'Account Operators' or equivalent in AD Users and Computers.</li>
              <li>ICMP blocked. Some networks drop ping but allow LDAP. If the next probe attempt succeeds you can ignore this.</li>
            </ul>
          </details>
        </fieldset>
      ) : null}
    </section>
  );
}
