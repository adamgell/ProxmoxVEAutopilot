import { Power, RefreshCw, Square, Trash2 } from "lucide-react";
import { useCallback, useState } from "react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";
import { statusClass, statusLabel } from "../viewModels";

interface UtmVm {
  readonly name?: string;
  readonly uuid?: string;
  readonly status?: string;
  readonly ips?: readonly string[];
}

interface UtmIso {
  readonly name?: string;
  readonly path?: string;
}

interface UtmPayload {
  readonly vms: readonly UtmVm[];
  readonly isos: readonly UtmIso[];
  readonly host_summary?: Readonly<Record<string, unknown>>;
  readonly utmctl_path?: string;
  readonly library_path?: string;
  readonly error?: string;
}

function Badge({ status }: { readonly status: string | undefined }) {
  const normalized = textValue(status, "unknown");
  return <span className={statusClass(normalized)}>{statusLabel(normalized)}</span>;
}

export function UtmVmsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<UtmPayload>({ vms: [], isos: [], host_summary: {} });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionStatus, setActionStatus] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<UtmPayload>("/api/utm-vms/page"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load UTM VMs");
    } finally {
      setLoading(false);
    }
  }, []);

  usePolling(load);

  const vmAction = async (action: string, vmName: string) => {
    try {
      await fetchJson(`/api/utm/vms/${encodeURIComponent(vmName)}/${action}`, { method: "POST" });
      setActionStatus(`${action} requested for ${vmName}`);
      await load();
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : `${action} failed`);
    }
  };

  return (
    <PageFrame bootstrap={bootstrap} title="UTM Virtual Machines" section="Fleet" path="/react/utm-vms">
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading UTM VMs</span>
          <div className="load-strip__track" role="progressbar" aria-label="UTM VMs loading"><span /></div>
        </div>
      ) : null}
      {error || payload.error ? <p className="notice notice--bad" role="alert">{error || payload.error}</p> : null}
      {actionStatus ? <p className="notice" role="status">{actionStatus}</p> : null}

      <section className="metric-strip" aria-label="UTM metrics">
        <Metric label="VMs" value={String(payload.vms.length)} />
        <Metric label="Started" value={String(payload.vms.filter((vm) => vm.status === "started").length)} />
        <Metric label="ISOs" value={String(payload.isos.length)} />
        <Metric label="Host" value={textValue(payload.host_summary?.status ?? payload.host_summary?.state, "available")} />
      </section>

      <Panel title="Host Paths" action={<button className="utility-button" type="button" onClick={() => { void load(); }}><RefreshCw size={15} aria-hidden="true" /> Refresh</button>}>
        <dl className="utility-definition-grid">
          <div><dt>utmctl</dt><dd><code>{textValue(payload.utmctl_path)}</code></dd></div>
          <div><dt>Library</dt><dd><code>{textValue(payload.library_path)}</code></dd></div>
        </dl>
      </Panel>

      <Panel title="UTM VMs">
        {payload.vms.length ? (
          <div className="table-wrap">
            <table className="jobs-table utility-table" aria-label="UTM VMs">
              <thead><tr><th>Name</th><th>UUID</th><th>Status</th><th>IPs</th><th>Actions</th></tr></thead>
              <tbody>
                {payload.vms.map((vm, index) => {
                  const vmName = textValue(vm.name, `vm-${String(index)}`);
                  return (
                    <tr key={textValue(vm.uuid, vmName)}>
                      <td><strong>{vmName}</strong></td>
                      <td><code>{textValue(vm.uuid)}</code></td>
                      <td><Badge status={vm.status} /></td>
                      <td>{vm.ips?.length ? vm.ips.map((ip) => <code key={ip}>{ip}</code>) : "-"}</td>
                      <td>
                        <div className="utility-row-actions">
                          <button type="button" onClick={() => { void vmAction("start", vmName); }} disabled={vm.status === "started"}><Power size={14} aria-hidden="true" /> Start</button>
                          <button type="button" onClick={() => { void vmAction("stop", vmName); }} disabled={vm.status === "stopped"}><Square size={14} aria-hidden="true" /> Stop</button>
                          <button type="button" onClick={() => { void vmAction("delete", vmName); }} disabled={vm.status !== "stopped"}><Trash2 size={14} aria-hidden="true" /> Delete</button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : <p className="empty">No UTM VMs found.</p>}
      </Panel>

      <Panel title="ISO Library">
        {payload.isos.length ? (
          <div className="table-wrap">
            <table className="jobs-table utility-table" aria-label="UTM ISOs">
              <thead><tr><th>Name</th><th>Path</th></tr></thead>
              <tbody>{payload.isos.map((iso, index) => <tr key={textValue(iso.path ?? iso.name, String(index))}><td>{textValue(iso.name)}</td><td><code>{textValue(iso.path)}</code></td></tr>)}</tbody>
            </table>
          </div>
        ) : <p className="empty">No UTM ISOs found.</p>}
      </Panel>
    </PageFrame>
  );
}
