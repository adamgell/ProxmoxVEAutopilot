import { useEffect, useState } from "react";
import type { AppBootstrap } from "../contracts";

interface Phase {
  readonly item_id: string;
  readonly label: string;
  readonly status: "pending" | "running" | "ready" | "blocked" | "failed" | "skipped";
  readonly detail: string;
  readonly sort_order: number;
}

interface Snapshot {
  readonly run_id: string;
  readonly phases: Phase[];
}

const STATUS_LABEL: Record<Phase["status"], string> = {
  pending: "Waiting",
  running: "Running",
  ready: "Done",
  blocked: "Blocked",
  failed: "Failed",
  skipped: "Skipped",
};

interface Props {
  readonly bootstrap: AppBootstrap;
}

export function OnboardingSetupPage(_props: Props) {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [errors, setErrors] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let intervalId: number | null = null;

    async function tick() {
      try {
        const r = await fetch("/api/onboarding/setup-status", { credentials: "include" });
        if (r.status === 404) {
          if (!cancelled) setErrors("No launched onboarding run found.");
          return;
        }
        if (!r.ok) {
          if (!cancelled) setErrors(`Status fetch failed: ${r.status}`);
          return;
        }
        const body = (await r.json()) as Snapshot;
        if (!cancelled) setSnap(body);
      } catch (e) {
        if (!cancelled) setErrors((e as Error).message);
      }
    }

    function resetInterval() {
      if (intervalId !== null) window.clearInterval(intervalId);
      const ms = document.visibilityState === "visible" ? 2000 : 10000;
      intervalId = window.setInterval(() => void tick(), ms);
    }

    void tick();
    resetInterval();
    document.addEventListener("visibilitychange", resetInterval);

    return () => {
      cancelled = true;
      if (intervalId !== null) window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", resetInterval);
    };
  }, []);

  const allReady = snap?.phases.every((p) => p.status === "ready" || p.status === "skipped");
  const failed = snap?.phases.find((p) => p.status === "failed");

  return (
    <main className="onboarding-setup-page">
      <header>
        <h1>Setting up your first deployment</h1>
        {snap ? <p>Run id: <code>{snap.run_id}</code></p> : null}
      </header>
      {errors ? <p role="alert">{errors}</p> : null}
      {snap ? (
        <ol className="phase-rail" aria-live="polite" aria-label="Setup phases">
          {snap.phases.map((p, idx) => (
            <li key={p.item_id} className={`phase phase-${p.status}`}>
              <span className="phase-number">Phase {idx + 1} of {snap.phases.length}</span>
              <strong>{p.label}</strong>
              <span className="phase-status-badge">{STATUS_LABEL[p.status]}</span>
              {p.status === "failed" ? (
                <details open>
                  <summary>What if it fails</summary>
                  <p>{p.detail}</p>
                </details>
              ) : null}
            </li>
          ))}
        </ol>
      ) : !errors ? <p>Loading setup status...</p> : null}
      {allReady ? (
        <section className="onboarding-complete-card" role="status">
          <h2>Setup complete</h2>
          <p>Your trial VM is up. Open <a href="/react/vms">/react/vms</a> to see it.</p>
        </section>
      ) : null}
      {failed ? (
        <section className="onboarding-failed-card" role="alert">
          <h2>Setup hit a snag</h2>
          <p>Phase "{failed.label}" failed. {failed.detail}</p>
          <a href="/react/onboarding">Back to wizard</a>
        </section>
      ) : null}
    </main>
  );
}
