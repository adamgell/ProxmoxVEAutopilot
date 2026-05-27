import { useEffect, useState } from "react";

interface CardRow {
  readonly label: string;
  readonly ok: boolean;
  readonly summary: string;
}

interface AlreadyConfiguredResponse {
  readonly proxmox: { ok: boolean; summary: string };
  readonly storage: { ok: boolean; summary: string };
  readonly network: { ok: boolean; summary: string };
  readonly ad_vault: { ok: boolean; summary: string };
}

export function AlreadyConfiguredCard() {
  const [rows, setRows] = useState<CardRow[]>([]);
  useEffect(() => {
    void (async () => {
      try {
        const r = await fetch("/api/onboarding/already-configured", { credentials: "include" });
        if (!r.ok) {
          setRows([{ label: "Status", ok: false, summary: `Couldn't reach controller (HTTP ${r.status})` }]);
          return;
        }
        const body = (await r.json()) as Partial<AlreadyConfiguredResponse>;
        const next: CardRow[] = [];
        if (body.proxmox) next.push({ label: "Proxmox", ok: body.proxmox.ok, summary: body.proxmox.summary });
        if (body.storage) next.push({ label: "Storage", ok: body.storage.ok, summary: body.storage.summary });
        if (body.network) next.push({ label: "Network", ok: body.network.ok, summary: body.network.summary });
        if (body.ad_vault) next.push({ label: "AD vault", ok: body.ad_vault.ok, summary: body.ad_vault.summary });
        setRows(next);
      } catch (e) {
        setRows([{ label: "Status", ok: false, summary: (e as Error).message }]);
      }
    })();
  }, []);
  return (
    <aside className="already-configured" aria-label="Already configured">
      <h3>Already configured by the controller</h3>
      <ul>
        {rows.map((row) => (
          <li key={row.label} className={row.ok ? "ok" : "warn"}>
            <strong>{row.label}:</strong> {row.summary}
          </li>
        ))}
      </ul>
      <p className="subtitle">
        You did not need to enter any of this. If a row is yellow, open <a href="/react/settings">Settings</a> to fix it.
      </p>
    </aside>
  );
}
