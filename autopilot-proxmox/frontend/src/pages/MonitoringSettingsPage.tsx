import { useCallback, useRef, useState } from "react";

import { deleteJson, fetchJson, postJson, putJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, MonitoringSettingsFullResponse } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";

export function MonitoringSettingsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const ouFormRef = useRef<HTMLFormElement | null>(null);
  const [payload, setPayload] = useState<MonitoringSettingsFullResponse | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<MonitoringSettingsFullResponse>("/api/monitoring/settings/full"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load monitoring settings");
    }
  }, []);

  usePolling(load);

  const saveSettings = async (formData: FormData) => {
    try {
      await putJson("/api/monitoring/settings", {
        enabled: formData.get("enabled") === "on",
        interval_seconds: Number(formData.get("interval_seconds") || 300),
        ad_credential_id: Number(formData.get("ad_credential_id") || 0)
      });
      setMessage("Monitoring settings saved");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Monitoring save failed");
    }
  };

  const addOu = async () => {
    if (!ouFormRef.current) {
      return;
    }
    const form = new FormData(ouFormRef.current);
    try {
      await postJson("/api/monitoring/search-ous", {
        dn: textValue(form.get("dn"), ""),
        label: textValue(form.get("label"), ""),
        enabled: true
      });
      ouFormRef.current.reset();
      setMessage("Search OU added");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "OU add failed");
    }
  };

  const deleteOu = async (id: number) => {
    if (!window.confirm("Delete this search OU?")) {
      return;
    }
    try {
      await deleteJson(`/api/monitoring/search-ous/${String(id)}`);
      setMessage("Search OU deleted");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "OU delete failed");
    }
  };

  const refreshKeytab = async () => {
    try {
      await postJson("/api/monitoring/keytab/refresh-now");
      setMessage("Keytab refresh complete");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Keytab refresh failed");
    }
  };

  const settings = payload?.settings;
  const enabledOus = payload?.search_ous.filter((ou) => ou.enabled).length ?? 0;

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Monitoring Settings"
      section="Settings"
      path="/react/monitoring/settings"
    >
      {message ? <p className="notice" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="Monitoring settings metrics">
        <Metric label="Monitor" value={settings?.enabled ? "On" : "Off"} tone={settings?.enabled ? "good" : "neutral"} />
        <Metric label="Interval" value={`${String(settings?.interval_seconds ?? "-")}s`} />
        <Metric label="Search OUs" value={String(payload?.search_ous.length ?? 0)} />
        <Metric label="Enabled OUs" value={String(enabledOus)} />
      </section>
      <section className="section-grid section-grid--wide">
        <Panel title="Settings">
          <form className="utility-form" onSubmit={(event) => { event.preventDefault(); void saveSettings(new FormData(event.currentTarget)); }}>
            <div className="utility-field-grid">
              <label className="utility-field"><span>Enabled</span><input name="enabled" type="checkbox" defaultChecked={Boolean(settings?.enabled)} /></label>
              <label className="utility-field"><span>Interval seconds</span><input name="interval_seconds" type="number" min="60" defaultValue={settings?.interval_seconds ?? 300} /></label>
              <label className="utility-field">
                <span>AD credential</span>
                <select name="ad_credential_id" defaultValue={settings?.ad_credential_id ?? 0}>
                  <option value="0">-</option>
                  {(payload?.domain_creds ?? []).map((cred) => <option key={cred.id} value={cred.id}>{cred.name}</option>)}
                </select>
              </label>
            </div>
            <div className="utility-form-actions">
              <button className="utility-button" type="submit">Save</button>
            </div>
          </form>
        </Panel>
        <Panel title="Keytab">
          <dl className="utility-definition-grid">
            <div><dt>Status</dt><dd>{textValue(payload?.keytab["status"] ?? payload?.keytab["last_probe_status"])}</dd></div>
            <div><dt>Checked</dt><dd>{textValue(payload?.keytab["checked_at"] ?? payload?.keytab["last_probe_at"])}</dd></div>
            <div><dt>Message</dt><dd>{textValue(payload?.keytab["message"] ?? payload?.keytab["detail"] ?? payload?.keytab["last_probe_message"])}</dd></div>
          </dl>
          <button className="utility-button" type="button" onClick={() => { void refreshKeytab(); }}>Refresh keytab</button>
        </Panel>
      </section>
      <Panel title="Search OUs">
        <form ref={ouFormRef} className="utility-upload-row" onSubmit={(event) => { event.preventDefault(); void addOu(); }}>
          <input name="dn" placeholder="OU=Workstations,DC=example,DC=com" aria-label="Search OU distinguished name" required />
          <input name="label" placeholder="Label" aria-label="Search OU label" />
          <button className="utility-button" type="submit">Add OU</button>
        </form>
        <div className="table-wrap">
          <table className="jobs-table utility-table">
            <thead>
              <tr>
                <th scope="col">Label</th>
                <th scope="col">DN</th>
                <th scope="col">Enabled</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(payload?.search_ous ?? []).map((ou) => (
                <tr key={ou.id}>
                  <td>{textValue(ou.label)}</td>
                  <td>{ou.dn}</td>
                  <td>{ou.enabled ? "Yes" : "No"}</td>
                  <td><button className="utility-button utility-button--danger" type="button" onClick={() => { void deleteOu(ou.id); }}>Delete</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </PageFrame>
  );
}
