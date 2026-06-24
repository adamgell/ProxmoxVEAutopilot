import { useCallback, useMemo, useRef, useState } from "react";

import { fetchJson, postForm } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type {
  AppBootstrap,
  CredentialSummary,
  SettingsField,
  SettingsResponse,
  SettingsSection
} from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { lowerText, secretState, shortTypeLabel, textValue } from "../utilityModels";

function sectionAnchor(section: string): string {
  const slug = section.toLowerCase().replace(/[^a-z0-9]+/gu, "-").replace(/^-|-$/gu, "");
  return `settings-section-${slug || "section"}`;
}

function fieldMatchesQuery(section: SettingsSection, field: SettingsField, query: string): boolean {
  if (!query) {
    return true;
  }
  return [
    section.section,
    section.source,
    field.key,
    field.label,
    field.type,
    field.source,
    field.help,
    field.value
  ].some((value) => lowerText(value).includes(query));
}

function FieldInput({ field }: { readonly field: SettingsField }) {
  const name = field.key;
  const common = {
    name,
    disabled: Boolean(field.readonly),
    "aria-label": field.label ?? name
  };
  if (field.type === "bool") {
    return (
      <input
        {...common}
        type="checkbox"
        defaultChecked={Boolean(field.value)}
        value="1"
      />
    );
  }
  if (field.options?.length) {
    return (
      <select {...common} defaultValue={textValue(field.value, "")}>
        <option value="">-</option>
        {field.options.map((option) => (
          <option key={option} value={option}>{field.labels?.[option] ?? option}</option>
        ))}
      </select>
    );
  }
  return (
    <input
      {...common}
      type={field.type === "number" ? "number" : field.source === "vault" ? "password" : "text"}
      defaultValue={field.source === "vault" ? "" : textValue(field.value, "")}
      placeholder={field.source === "vault" ? secretState(field.is_set) : undefined}
    />
  );
}

export function SettingsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const formRef = useRef<HTMLFormElement | null>(null);
  const [payload, setPayload] = useState<SettingsResponse | null>(null);
  const [credentials, setCredentials] = useState<readonly CredentialSummary[]>([]);
  const [filter, setFilter] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [credentialError, setCredentialError] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<SettingsResponse>("/api/settings"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load settings");
    }

    try {
      setCredentials(await fetchJson<readonly CredentialSummary[]>("/api/credentials"));
      setCredentialError("");
    } catch (err) {
      setCredentialError(err instanceof Error ? err.message : "Failed to load credentials");
    }
  }, []);

  usePolling(load);

  const save = async () => {
    if (!formRef.current) {
      return;
    }
    const form = new FormData(formRef.current);
    form.append("_all_fields", "1");
    try {
      const result = await postForm<SettingsResponse>("/api/settings", form);
      setPayload(result);
      setMessage("Settings saved");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    }
  };

  const sections = useMemo(() => payload?.sections ?? [], [payload]);
  const fieldCount = sections.reduce((sum, section) => sum + section.fields.length, 0);
  const vaultFieldCount = sections.reduce(
    (sum, section) => sum + section.fields.filter((field) => field.source === "vault").length,
    0
  );
  const query = filter.trim().toLowerCase();
  const visibleSections = useMemo(
    () => sections
      .map((section) => ({
        ...section,
        fields: section.fields.filter((field) => fieldMatchesQuery(section, field, query))
      }))
      .filter((section) => section.fields.length > 0),
    [query, sections]
  );
  const credentialTypeCounts = useMemo(
    () => credentials.reduce<Record<string, number>>((counts, row) => {
      counts[row.type] = (counts[row.type] ?? 0) + 1;
      return counts;
    }, {}),
    [credentials]
  );

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Settings"
      section="Settings"
      path="/react/settings"
      action={(
        <div className="action-cluster">
          <a className="action-link" href="/react/credentials">Credentials</a>
          <a className="action-link" href="/react/credentials/new">New credential</a>
          <a className="action-link" href="/react/monitoring/settings">Monitoring settings</a>
        </div>
      )}
    >
      {message ? <p className="notice" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {credentialError ? <p className="notice notice--bad" role="alert">{credentialError}</p> : null}
      <section className="metric-strip" aria-label="Settings metrics">
        <Metric label="Backend" value={payload?.hypervisor_type ?? "-"} />
        <Metric label="Sections" value={String(sections.length)} />
        <Metric label="Fields" value={String(fieldCount)} />
        <Metric label="Credential records" value={String(credentials.length)} />
        <Metric label="Secret fields" value={String(vaultFieldCount)} />
        <Metric label="Proxmox SSH" value={textValue(payload?.proxmox_bootstrap["root_password_set"] ? "Set" : "Not set")} />
      </section>
      <section className="settings-overview-grid" aria-label="Settings shortcuts">
        <Panel title="Settings map">
          <nav className="settings-section-nav" aria-label="Settings sections">
            {sections.map((section) => (
              <a key={section.section} className="settings-section-link" href={`#${sectionAnchor(section.section)}`}>
                <span>{section.section}</span>
                <small>{String(section.fields.length)} field{section.fields.length === 1 ? "" : "s"}</small>
              </a>
            ))}
          </nav>
        </Panel>
        <Panel title="Credential access" action={<a className="action-link" href="/react/credentials/new">Add credential</a>}>
          <dl className="settings-credential-summary">
            <div><dt>Total</dt><dd>{String(credentials.length)}</dd></div>
            <div><dt>Domain join</dt><dd>{String(credentialTypeCounts["domain_join"] ?? 0)}</dd></div>
            <div><dt>Local admin</dt><dd>{String(credentialTypeCounts["local_admin"] ?? 0)}</dd></div>
            <div><dt>File-based</dt><dd>{String((credentialTypeCounts["odj_blob"] ?? 0) + (credentialTypeCounts["mde_onboarding"] ?? 0))}</dd></div>
          </dl>
          {credentials.length ? (
            <ul className="settings-credential-list">
              {credentials.slice(0, 4).map((row) => (
                <li key={row.id}>
                  <span>
                    <strong>{row.name}</strong>
                    <small>{shortTypeLabel(row.type)}</small>
                  </span>
                  <a className="action-link" href={`/react/credentials/${String(row.id)}/edit`} aria-label={`Edit ${row.name}`}>Edit</a>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">No credentials yet.</p>
          )}
        </Panel>
        <Panel title="Proxmox bootstrap">
          <dl className="utility-definition-grid">
            <div><dt>Host</dt><dd>{textValue(payload?.proxmox_bootstrap["host"])}</dd></div>
            <div><dt>Disk storage</dt><dd>{textValue(payload?.proxmox_bootstrap["disk_storage"])}</dd></div>
            <div><dt>ISO storage</dt><dd>{textValue(payload?.proxmox_bootstrap["iso_storage"])}</dd></div>
            <div><dt>Token</dt><dd>{textValue(payload?.proxmox_bootstrap["default_token_id"])}</dd></div>
          </dl>
        </Panel>
      </section>
      <form ref={formRef} className="utility-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <div className="settings-form-head">
          <label className="filter">
            <span>Filter settings</span>
            <input
              aria-label="Filter settings"
              type="search"
              value={filter}
              onChange={(event) => { setFilter(event.target.value); }}
              placeholder="Setting name, source, or value"
            />
          </label>
          <div className="utility-form-actions settings-save-actions">
            <span className="result-count">{String(visibleSections.length)} of {String(sections.length)} sections</span>
            <button className="utility-button" type="submit">Save settings</button>
          </div>
        </div>
        <section className="utility-settings-grid">
          {visibleSections.map((section) => (
            <div key={section.section} id={sectionAnchor(section.section)} className="settings-section-anchor">
              <Panel title={section.section} action={<span className="result-count">{String(section.fields.length)} fields</span>}>
                <div className="utility-field-grid">
                  {section.fields.map((field) => (
                    <label key={field.key} className="utility-field">
                      <span>{field.label ?? field.key}</span>
                      <FieldInput field={field} />
                      {field.source === "vault" ? <small>{secretState(field.is_set)}</small> : null}
                    </label>
                  ))}
                </div>
              </Panel>
            </div>
          ))}
        </section>
        {!visibleSections.length ? <p className="empty">No settings match that filter.</p> : null}
        <div className="utility-form-actions settings-save-actions settings-save-actions--bottom">
          <button className="utility-button" type="submit">Save settings</button>
        </div>
      </form>
    </PageFrame>
  );
}
