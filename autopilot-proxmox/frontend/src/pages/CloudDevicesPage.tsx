import { useCallback, useMemo, useState } from "react";

import { fetchJson, postForm } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, CloudDeviceGroup, CloudDevicesResponse } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { lowerText, textValue } from "../utilityModels";

function groupName(group: CloudDeviceGroup): string {
  return textValue(group.intune?.display_name ?? group.autopilot?.display_name ?? group.entra?.display_name ?? group.display_name ?? group.serial);
}

function groupMatches(group: CloudDeviceGroup, query: string): boolean {
  if (!query) {
    return true;
  }
  return [
    group.serial,
    groupName(group),
    group.intune?.profile,
    group.autopilot?.group_tag,
    group.entra?.display_name,
    group.pve?.["name"],
    group.pve?.["vmid"]
  ].some((value) => lowerText(value).includes(query));
}

function targetValue(source: string, objectId: unknown): string | null {
  const id = textValue(objectId, "");
  return id ? `${source}:${id}` : null;
}

export function CloudDevicesPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<CloudDevicesResponse | null>(null);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<readonly string[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<CloudDevicesResponse>("/api/cloud/devices"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load cloud devices");
    }
  }, []);

  usePolling(load);

  const groups = useMemo(() => payload?.groups ?? [], [payload]);
  const query = filter.trim().toLowerCase();
  const filtered = useMemo(() => groups.filter((group) => groupMatches(group, query)), [groups, query]);
  const pveCount = groups.filter((group) => group.pve).length;
  const intuneCount = groups.filter((group) => group.intune).length;
  const autopilotCount = groups.filter((group) => group.autopilot).length;

  const runSync = async () => {
    try {
      const result = await postForm<{ readonly synced?: string }>("/api/cloud/sync", new FormData());
      setMessage(result.synced ? `Synced ${result.synced}` : "Sync queued");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Cloud sync failed");
    }
  };

  const deleteSelected = async () => {
    if (!selected.length || !window.confirm(`Delete ${String(selected.length)} selected cloud records?`)) {
      return;
    }
    const form = new FormData();
    selected.forEach((value) => {
      form.append("targets", value);
    });
    try {
      const result = await postForm<{ readonly job_id?: string; readonly count?: number }>("/api/cloud/delete", form);
      setMessage(`Delete job ${textValue(result.job_id)} started for ${String(result.count ?? selected.length)} records`);
      setSelected([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    }
  };

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Cloud Devices"
      section="Fleet"
      path="/react/devices"
    >
      {message ? <p className="notice" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="Cloud device metrics">
        <Metric label="Records" value={String(groups.length)} />
        <Metric label="Intune" value={String(intuneCount)} tone={intuneCount ? "good" : "neutral"} />
        <Metric label="Autopilot" value={String(autopilotCount)} tone={autopilotCount ? "good" : "neutral"} />
        <Metric label="PVE links" value={String(pveCount)} tone={pveCount ? "active" : "neutral"} />
      </section>
      <section className="filter-row" aria-label="Cloud device controls">
        <div className="filter-row__top">
          <label className="filter">
            <span>Search devices</span>
            <input value={filter} onChange={(event) => { setFilter(event.target.value); }} placeholder="Serial, name, VMID, tag" />
          </label>
          <span className="result-count">{String(filtered.length)} of {String(groups.length)}</span>
        </div>
        <div className="action-cluster">
          <button className="utility-button" type="button" onClick={() => { void runSync(); }}>Sync Graph</button>
          <button className="utility-button utility-button--danger" type="button" onClick={() => { void deleteSelected(); }} disabled={!selected.length}>Delete selected</button>
        </div>
      </section>
      <Panel title="Device records">
        <div className="table-wrap">
          <table className="jobs-table utility-table">
            <thead>
              <tr>
                <th scope="col">Select</th>
                <th scope="col">Device</th>
                <th scope="col">Serial</th>
                <th scope="col">Intune</th>
                <th scope="col">Autopilot</th>
                <th scope="col">Entra</th>
                <th scope="col">VMID</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((group) => {
                const intuneTarget = targetValue("intune", group.intune?.id ?? group.intune?.object_id);
                const autopilotTarget = targetValue("autopilot", group.autopilot?.id ?? group.autopilot?.object_id);
                const entraTarget = targetValue("entra", group.entra?.id ?? group.entra?.object_id);
                const targets = [intuneTarget, autopilotTarget, entraTarget].filter((value): value is string => Boolean(value));
                const checked = targets.length > 0 && targets.every((value) => selected.includes(value));
                return (
                  <tr key={group.serial}>
                    <td>
                      <input
                        aria-label={`Select ${group.serial}`}
                        type="checkbox"
                        checked={checked}
                        onChange={(event) => {
                          setSelected((current) => {
                            const without = current.filter((value) => !targets.includes(value));
                            return event.target.checked ? [...without, ...targets] : without;
                          });
                        }}
                      />
                    </td>
                    <td><a href={group.pve?.["vmid"] ? `/react/vms/${textValue(group.pve["vmid"])}` : "/react/devices"}>{groupName(group)}</a></td>
                    <td>{group.serial}</td>
                    <td>{textValue(group.intune?.display_name ?? group.intune?.last_contact)}</td>
                    <td>{textValue(group.autopilot?.profile ?? group.autopilot?.group_tag)}</td>
                    <td>{textValue(group.entra?.display_name)}</td>
                    <td>{textValue(group.pve?.["vmid"])}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {!filtered.length ? <p className="empty">No cloud device records.</p> : null}
      </Panel>
      <Panel title="Delete history">
        <ul className="utility-list">
          {(payload?.deletions ?? []).slice(0, 8).map((row, index) => (
            <li key={`${textValue(row.object_id, "row")}-${String(index)}`}>
              <strong>{textValue(row.source)}</strong>
              <span>{textValue(row.display_name ?? row.serial)}</span>
              <small>{textValue(row.status ?? row.message)}</small>
            </li>
          ))}
        </ul>
      </Panel>
    </PageFrame>
  );
}
