import { useCallback, useMemo, useRef, useState } from "react";

import { deleteJson, fetchJson, postForm } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, CredentialSummary } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { lowerText, shortTypeLabel, textValue } from "../utilityModels";

const credentialTypes = ["domain_join", "local_admin", "odj_blob", "mde_onboarding"] as const;
type CredentialType = typeof credentialTypes[number];

function credentialTypeFrom(value: string): CredentialType {
  return credentialTypes.find((type) => type === value) ?? "domain_join";
}

function credentialIdFromPath(path: string): number | null {
  const match = /^\/react\/credentials\/(\d+)\/edit$/u.exec(path);
  return match ? Number(match[1]) : null;
}

function CredentialPayloadFields({ type, isEdit }: { readonly type: CredentialType; readonly isEdit: boolean }) {
  if (type === "domain_join") {
    return (
      <>
        <label className="utility-field"><span>Domain FQDN</span><input name="domain_fqdn" /></label>
        <label className="utility-field"><span>Username</span><input name="username" /></label>
        <label className="utility-field"><span>Password</span><input name="password" type="password" placeholder={isEdit ? "Leave blank to keep" : ""} /></label>
        <label className="utility-field utility-field--wide"><span>OU hint</span><input name="ou_hint" /></label>
      </>
    );
  }
  if (type === "local_admin") {
    return (
      <>
        <label className="utility-field"><span>Username</span><input name="la_username" /></label>
        <label className="utility-field"><span>Password</span><input name="la_password" type="password" placeholder={isEdit ? "Leave blank to keep" : ""} /></label>
      </>
    );
  }
  if (type === "odj_blob") {
    return <label className="utility-field utility-field--wide"><span>ODJ blob</span><input name="odj_file" type="file" /></label>;
  }
  return <label className="utility-field utility-field--wide"><span>MDE onboarding file</span><input name="onboarding_file" type="file" accept=".py,.cmd,.ps1,.zip,text/plain" /></label>;
}

export function CredentialsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const path = window.location.pathname;
  const editId = credentialIdFromPath(path);
  const isNew = path === "/react/credentials/new";
  const isEdit = editId !== null;
  const formRef = useRef<HTMLFormElement | null>(null);
  const [rows, setRows] = useState<readonly CredentialSummary[]>([]);
  const [filter, setFilter] = useState("");
  const [type, setType] = useState<CredentialType>("domain_join");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setRows(await fetchJson<readonly CredentialSummary[]>("/api/credentials"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load credentials");
    }
  }, []);

  usePolling(load);

  const editing = useMemo(() => rows.find((row) => row.id === editId) ?? null, [editId, rows]);
  const query = filter.trim().toLowerCase();
  const filtered = useMemo(() => rows.filter((row) => [row.name, row.type].some((value) => lowerText(value).includes(query))), [query, rows]);

  const submit = async () => {
    if (!formRef.current) {
      return;
    }
    const form = new FormData(formRef.current);
    if (!isEdit) {
      form.set("type", type);
    }
    try {
      if (editId !== null) {
        await postForm<{ readonly ok: boolean }>(`/api/credentials/${String(editId)}`, form);
        setMessage("Credential updated");
      } else {
        await postForm<{ readonly id: number }>("/api/credentials", form);
        setMessage("Credential created");
      }
      formRef.current.reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Credential save failed");
    }
  };

  const removeCredential = async (row: CredentialSummary) => {
    if (!window.confirm(`Delete credential ${row.name}?`)) {
      return;
    }
    try {
      await deleteJson<{ readonly ok: boolean }>(`/api/credentials/${String(row.id)}`);
      setMessage("Credential deleted");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Credential delete failed");
    }
  };

  const activeType = isEdit ? credentialTypeFrom(editing?.type ?? "domain_join") : type;
  const title = isNew ? "New Credential" : isEdit ? "Edit Credential" : "Credentials";

  return (
    <PageFrame
      bootstrap={bootstrap}
      title={title}
      section="Settings"
      path={path}
      action={<a className="action-link" href="/legacy/credentials">Legacy</a>}
    >
      {message ? <p className="notice" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="Credential metrics">
        <Metric label="Credentials" value={String(rows.length)} />
        <Metric label="Domain" value={String(rows.filter((row) => row.type === "domain_join").length)} />
        <Metric label="Local admin" value={String(rows.filter((row) => row.type === "local_admin").length)} />
        <Metric label="Files" value={String(rows.filter((row) => row.type === "odj_blob" || row.type === "mde_onboarding").length)} />
      </section>
      {(isNew || isEdit) ? (
        <Panel title={isEdit ? "Edit credential" : "Create credential"}>
          <form ref={formRef} className="utility-form" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
            <div className="utility-field-grid">
              <label className="utility-field">
                <span>Name</span>
                <input name="name" defaultValue={editing?.name ?? ""} required />
              </label>
              <label className="utility-field">
                <span>Type</span>
                <select
                  name="type"
                  value={activeType}
                  disabled={isEdit}
                  onChange={(event) => { setType(credentialTypeFrom(event.target.value)); }}
                >
                  {credentialTypes.map((item) => <option key={item} value={item}>{shortTypeLabel(item)}</option>)}
                </select>
              </label>
              <CredentialPayloadFields type={activeType} isEdit={isEdit} />
            </div>
            <div className="utility-form-actions">
              <button className="utility-button" type="submit">{isEdit ? "Update" : "Create"}</button>
              <a className="action-link" href="/react/credentials">Cancel</a>
            </div>
          </form>
        </Panel>
      ) : null}
      <Panel title="Credential records" action={<a className="action-link" href="/react/credentials/new">New</a>}>
        <section className="filter-row" aria-label="Credential filters">
          <div className="filter-row__top">
            <label className="filter">
              <span>Search credentials</span>
              <input value={filter} onChange={(event) => { setFilter(event.target.value); }} placeholder="Name or type" />
            </label>
            <span className="result-count">{String(filtered.length)} of {String(rows.length)}</span>
          </div>
        </section>
        <div className="table-wrap">
          <table className="jobs-table utility-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Updated</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.id}>
                  <td>{row.name}</td>
                  <td>{shortTypeLabel(row.type)}</td>
                  <td>{textValue(row.updated_at)}</td>
                  <td>
                    <div className="utility-row-actions">
                      <a className="action-link" href={`/react/credentials/${String(row.id)}/edit`}>Edit</a>
                      <button className="utility-button utility-button--danger" type="button" onClick={() => { void removeCredential(row); }}>Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!filtered.length ? <p className="empty">No credentials.</p> : null}
      </Panel>
    </PageFrame>
  );
}
