import { useCallback, useRef, useState } from "react";

import { fetchJson, postForm } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, SettingsField, SettingsResponse } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { secretState, textValue } from "../utilityModels";

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
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<SettingsResponse>("/api/settings"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load settings");
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

  const sections = payload?.sections ?? [];
  const fieldCount = sections.reduce((sum, section) => sum + section.fields.length, 0);

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Settings"
      section="Settings"
      path="/react/settings"
      action={<a className="action-link" href="/legacy/settings">Legacy</a>}
    >
      {message ? <p className="notice" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="Settings metrics">
        <Metric label="Backend" value={payload?.hypervisor_type ?? "-"} />
        <Metric label="Sections" value={String(sections.length)} />
        <Metric label="Fields" value={String(fieldCount)} />
        <Metric label="Proxmox SSH" value={textValue(payload?.proxmox_bootstrap["root_password_set"] ? "Set" : "Not set")} />
      </section>
      <form ref={formRef} className="utility-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <div className="utility-form-actions">
          <button className="utility-button" type="submit">Save settings</button>
        </div>
        <section className="utility-settings-grid">
          {sections.map((section) => (
            <Panel key={section.section} title={section.section}>
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
          ))}
        </section>
      </form>
      <Panel title="Proxmox bootstrap">
        <dl className="utility-definition-grid">
          <div><dt>Host</dt><dd>{textValue(payload?.proxmox_bootstrap["host"])}</dd></div>
          <div><dt>Disk storage</dt><dd>{textValue(payload?.proxmox_bootstrap["disk_storage"])}</dd></div>
          <div><dt>ISO storage</dt><dd>{textValue(payload?.proxmox_bootstrap["iso_storage"])}</dd></div>
          <div><dt>Token</dt><dd>{textValue(payload?.proxmox_bootstrap["default_token_id"])}</dd></div>
        </dl>
      </Panel>
    </PageFrame>
  );
}
