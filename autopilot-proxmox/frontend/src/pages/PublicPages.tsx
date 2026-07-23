import { useCallback, useState } from "react";
import type { ReactNode } from "react";

import { fetchJson } from "../apiClient";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";

function PublicShell({
  bootstrap,
  title,
  children
}: {
  readonly bootstrap: AppBootstrap;
  readonly title: string;
  readonly children: ReactNode;
}) {
  return (
    <main className="public-shell">
      <section className="public-shell__panel" aria-labelledby="public-title">
        <p className="public-shell__eyebrow">Proxmox VE Autopilot</p>
        <h1 id="public-title">{title}</h1>
        {children}
        <footer>
          {bootstrap.buildVersion
            ? `v${bootstrap.buildVersion} (${bootstrap.buildSha ?? "unknown"})`
            : `Build ${bootstrap.buildSha ?? "unknown"}`}
          {bootstrap.buildTime ? ` / ${bootstrap.buildTime}` : ""}
        </footer>
      </section>
    </main>
  );
}

export function LoginPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const params = new URLSearchParams(window.location.search);
  const next = params.get("next") || "/react/dashboard";
  const error = params.get("error") || "";
  return (
    <PublicShell bootstrap={bootstrap} title="Sign in">
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <div className="public-actions">
        <a className="utility-button" href={`/auth/login/start?next=${encodeURIComponent(next)}`}>Microsoft sign in</a>
        <form method="post" action={`/auth/local/start?next=${encodeURIComponent(next)}`}>
          <button className="utility-button utility-button--secondary" type="submit">Local operator</button>
        </form>
      </div>
      <a className="action-link" href="/setup">Setup</a>
    </PublicShell>
  );
}

export function SetupPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<Readonly<Record<string, unknown>>>({});
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<Readonly<Record<string, unknown>>>("/api/setup/v1/state"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load setup state");
    }
  }, []);

  usePolling(load);

  return (
    <PublicShell bootstrap={bootstrap} title="Setup">
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <dl className="utility-definition-grid">
        <div><dt>Status</dt><dd>{textValue(payload.status ?? payload.phase)}</dd></div>
        <div><dt>Ready</dt><dd>{payload.ready === true ? "yes" : "no"}</dd></div>
        {payload.detail && payload.detail !== payload.status ? (
          <div><dt>Detail</dt><dd>{textValue(payload.detail)}</dd></div>
        ) : null}
      </dl>
      <div className="public-actions">
        <a className="utility-button" href="/auth/login">Sign in</a>
        <a className="action-link" href="/api/setup/v1/source-bundle.zip">Source bundle</a>
      </div>
    </PublicShell>
  );
}
