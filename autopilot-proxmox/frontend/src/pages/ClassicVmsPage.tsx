import { useCallback, useMemo, useState } from "react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, VmsFleetResponse, VmFleetRow } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { lowerText, textValue } from "../utilityModels";

function vmMatches(row: VmFleetRow, query: string): boolean {
  if (!query) {
    return true;
  }
  return [
    row.name,
    row.hostname,
    row.serial,
    row.ip_address,
    row.vmid,
    row.status,
    row["agent_version"]
  ].some((value) => lowerText(value).includes(query));
}

export function ClassicVmsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<VmsFleetResponse | null>(null);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<VmsFleetResponse>("/api/vms/fleet"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load VM table");
    }
  }, []);

  usePolling(load);

  const rows = useMemo(() => payload?.vms ?? [], [payload]);
  const query = filter.trim().toLowerCase();
  const filtered = useMemo(() => rows.filter((row) => vmMatches(row, query)), [query, rows]);
  const running = rows.filter((row) => row.status === "running").length;
  const agents = payload?.agents.length ?? 0;
  const missing = payload?.missing_vms.length ?? 0;

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Classic VM Table"
      section="Fleet"
      path="/react/legacy-vms"
      action={<a className="action-link" href="/legacy/vms">Legacy</a>}
    >
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="Classic VM metrics">
        <Metric label="VMs" value={String(rows.length)} />
        <Metric label="Running" value={String(running)} tone={running ? "good" : "neutral"} />
        <Metric label="Agents" value={String(agents)} />
        <Metric label="Missing" value={String(missing)} tone={missing ? "active" : "neutral"} />
      </section>
      <section className="filter-row" aria-label="Classic VM filters">
        <div className="filter-row__top">
          <label className="filter">
            <span>Search VMs</span>
            <input value={filter} onChange={(event) => { setFilter(event.target.value); }} placeholder="Name, VMID, IP, serial" />
          </label>
          <span className="result-count">{String(filtered.length)} of {String(rows.length)}</span>
        </div>
      </section>
      <Panel title="VM table">
        <div className="table-wrap">
          <table className="jobs-table utility-table">
            <thead>
              <tr>
                <th scope="col">Device Name</th>
                <th scope="col">Heartbeat</th>
                <th scope="col">Managed By</th>
                <th scope="col">OS</th>
                <th scope="col">OS version</th>
                <th scope="col">VMID</th>
                <th scope="col">IP address</th>
                <th scope="col">Runtime</th>
                <th scope="col">Agent</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.vmid}>
                  <td><a href={`/react/vms/${String(row.vmid)}`}>{textValue(row.hostname ?? row.name)}</a></td>
                  <td>{textValue(row.monitor_checked_at ?? row["last_seen_human"] ?? row["last_seen"])}</td>
                  <td>{row.in_intune ? "Intune" : "None"}</td>
                  <td>{textValue(row.os_caption)}</td>
                  <td>{textValue(row["os_version"] ?? row.os_build)}</td>
                  <td>{textValue(row.vmid)}</td>
                  <td>{textValue(row.ip_address)}</td>
                  <td>{textValue(row.status)}</td>
                  <td>{textValue(row["agent_version"])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!filtered.length ? <p className="empty">No VMs found.</p> : null}
      </Panel>
    </PageFrame>
  );
}
