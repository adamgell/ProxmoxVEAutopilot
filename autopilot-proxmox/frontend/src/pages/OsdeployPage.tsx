import { useCallback, useMemo, useState } from "react";

import { fetchJson, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { networkTargetOptions, type NetworkTargetOption } from "../networkTargets";
import { textValue } from "../utilityModels";
import { formatShortDateTime } from "../viewModels";

type OsdeployView = "overview" | "builder" | "cache" | "artifacts";
type ActionState = "idle" | "working" | "ready" | "failed";

interface OsdeployPayload {
  readonly artifacts: readonly OsdeployArtifact[];
  readonly ready_artifacts: readonly OsdeployArtifact[];
  readonly runs: readonly OsdeployRun[];
  readonly active_runs: readonly OsdeployRun[];
  readonly stale_failed_runs: readonly OsdeployRun[];
  readonly osdeploy_cache: OsdeployCache;
  readonly catalog: CatalogPayload;
  readonly proxmox_options: ProxmoxOptionsPayload;
  readonly osdeploy_build_defaults: BuildDefaults;
  readonly osdeploy_credentials: readonly CredentialOption[];
  readonly view: OsdeployView;
}

interface OsdeployArtifact {
  readonly id?: string;
  readonly architecture?: string;
  readonly osdeploy_module_version?: string;
  readonly osdbuilder_module_version?: string;
  readonly adk_version?: string;
  readonly build_sha?: string;
  readonly readiness?: string;
  readonly ready?: boolean;
  readonly source_media?: string;
  readonly image_name?: string;
  readonly image_index?: string | number;
  readonly os_version?: string;
  readonly os_edition?: string;
  readonly os_language?: string;
  readonly iso_path?: string;
  readonly wim_path?: string;
  readonly manifest_path?: string;
  readonly proxmox_volid?: string;
  readonly build_job_url?: string;
  readonly build_log_url?: string;
  readonly publish_job_url?: string;
  readonly publish_log_url?: string;
}

interface OsdeployRun {
  readonly run_id?: string;
  readonly artifact_id?: string;
  readonly requested_vm_name?: string;
  readonly expected_computer_name?: string;
  readonly vm_name?: string;
  readonly vmid?: string | number;
  readonly state?: string;
  readonly server_role?: string;
  readonly os_version?: string;
  readonly os_edition?: string;
  readonly created_at?: string;
  readonly first_heartbeat_at?: string;
  readonly archived?: boolean;
}

interface OsdeployCache {
  readonly storage?: {
    readonly root?: string;
    readonly ready?: boolean;
    readonly free_bytes?: number;
  };
  readonly summary?: {
    readonly ready?: number;
    readonly total?: number;
    readonly warming?: number;
    readonly failed?: number;
  };
  readonly entries?: readonly CacheEntry[];
}

interface CacheEntry {
  readonly id?: string;
  readonly entry_type?: string;
  readonly status?: string;
  readonly windows_version?: string;
  readonly architecture?: string;
  readonly edition?: string;
  readonly language?: string;
  readonly file_name?: string;
  readonly size_bytes?: number;
  readonly expected_size_bytes?: number;
  readonly sha256?: string;
  readonly expected_sha256?: string;
  readonly verified_at?: string;
  readonly last_served_at?: string;
  readonly served_count?: number;
  readonly error?: string;
}

interface CatalogPayload {
  readonly os_versions?: readonly string[];
  readonly os_editions?: readonly string[];
  readonly os_languages?: readonly string[];
  readonly server_roles?: readonly string[];
  readonly role_catalog?: Readonly<Record<string, RoleMetadata>>;
  readonly defaults?: Readonly<Record<string, unknown>>;
}

interface RoleMetadata {
  readonly name?: string;
  readonly readiness_status?: string;
  readonly readiness_status_name?: string;
  readonly required_fields?: readonly string[];
  readonly credential_fields?: readonly string[];
  readonly step_kinds?: readonly string[];
}

interface CredentialOption {
  readonly id?: string | number;
  readonly name?: string;
  readonly type?: string;
}

interface ProxmoxOptionsPayload {
  readonly nodes?: readonly string[];
  readonly bridges?: readonly string[];
  readonly network_targets?: readonly NetworkTargetOption[];
  readonly storages?: {
    readonly iso?: readonly string[];
    readonly disk?: readonly string[];
  };
  readonly defaults?: {
    readonly node?: string;
    readonly iso_storage?: string;
    readonly disk_storage?: string;
    readonly bridge?: string;
  };
}

interface BuildDefaults {
  readonly remote?: string;
  readonly remote_root?: string;
  readonly ssh_key_path?: string;
  readonly ssh_key_exists?: boolean;
  readonly ssh_public_key?: string;
}

interface PreflightResult {
  readonly launch_allowed: boolean;
  readonly target?: {
    readonly computer_name?: string;
  };
  readonly blocking_checks?: readonly CheckItem[];
  readonly warnings?: readonly CheckItem[];
}

interface CheckItem {
  readonly id?: string;
  readonly label?: string;
  readonly message?: string;
  readonly detail?: string;
}

const emptyPayload: OsdeployPayload = {
  artifacts: [],
  ready_artifacts: [],
  runs: [],
  active_runs: [],
  stale_failed_runs: [],
  osdeploy_cache: {},
  catalog: {},
  proxmox_options: {},
  osdeploy_build_defaults: {},
  osdeploy_credentials: [],
  view: "overview"
};

function asRecord(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Readonly<Record<string, unknown>> : {};
}

function normalizeView(value: unknown): OsdeployView {
  return value === "builder" || value === "cache" || value === "artifacts" ? value : "overview";
}

function normalizePayload(value: unknown): OsdeployPayload {
  const record = asRecord(value);
  return {
    artifacts: Array.isArray(record.artifacts) ? record.artifacts as readonly OsdeployArtifact[] : [],
    ready_artifacts: Array.isArray(record.ready_artifacts) ? record.ready_artifacts as readonly OsdeployArtifact[] : [],
    runs: Array.isArray(record.runs) ? record.runs as readonly OsdeployRun[] : [],
    active_runs: Array.isArray(record.active_runs) ? record.active_runs as readonly OsdeployRun[] : [],
    stale_failed_runs: Array.isArray(record.stale_failed_runs) ? record.stale_failed_runs as readonly OsdeployRun[] : [],
    osdeploy_cache: asRecord(record.osdeploy_cache),
    catalog: asRecord(record.catalog),
    proxmox_options: asRecord(record.proxmox_options),
    osdeploy_build_defaults: asRecord(record.osdeploy_build_defaults),
    osdeploy_credentials: Array.isArray(record.osdeploy_credentials) ? record.osdeploy_credentials as readonly CredentialOption[] : [],
    view: normalizeView(record.view)
  };
}

function numberText(value: unknown, fallback = "0"): string {
  return typeof value === "number" ? String(value) : textValue(value, fallback);
}

function optionsFrom(values: readonly string[] | undefined, fallback?: string): readonly string[] {
  const clean = (values ?? []).filter((value) => value.trim().length > 0);
  if (clean.length) {
    return clean;
  }
  return fallback ? [fallback] : [];
}

function artifactLabel(artifact: OsdeployArtifact): string {
  return [artifact.os_version, artifact.os_edition, artifact.os_language, artifact.build_sha].filter(Boolean).join(" / ") || textValue(artifact.id);
}

function artifactForRun(run: OsdeployRun, artifacts: readonly OsdeployArtifact[]): OsdeployArtifact | undefined {
  return artifacts.find((artifact) => artifact.id === run.artifact_id);
}

function badgeTone(status: string): string {
  const lower = status.toLowerCase();
  if (lower.includes("ready") || lower.includes("complete") || lower.includes("done")) {
    return "ready";
  }
  if (lower.includes("fail") || lower.includes("block")) {
    return "block";
  }
  return "warn";
}

function StatusBadge({ value }: { readonly value: string }) {
  return <span className={`cloudosd-badge ${badgeTone(value)}`}>{value}</span>;
}

function Field({
  label,
  children,
  help
}: {
  readonly label: string;
  readonly children: React.ReactNode;
  readonly help?: string;
}) {
  return (
    <label className="cloudosd-field">
      <span>{label}</span>
      {children}
      {help ? <small>{help}</small> : null}
    </label>
  );
}

function SelectField({
  label,
  name,
  options,
  defaultValue
}: {
  readonly label: string;
  readonly name: string;
  readonly options: readonly string[];
  readonly defaultValue?: string;
}) {
  return (
    <Field label={label}>
      <select name={name} aria-label={label} defaultValue={defaultValue ?? options[0] ?? ""}>
        {options.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    </Field>
  );
}

function NetworkTargetField({
  options,
  defaultValue
}: {
  readonly options: ProxmoxOptionsPayload;
  readonly defaultValue?: string;
}) {
  const targets = networkTargetOptions(options);
  return (
    <Field label="Network target">
      <select name="network_bridge" aria-label="Network target" defaultValue={defaultValue ?? targets[0]?.value ?? ""}>
        {targets.map((target) => (
          <option key={`${target.value}-${target.label}`} value={target.value}>{target.label}</option>
        ))}
      </select>
    </Field>
  );
}

function Subnav({ view }: { readonly view: OsdeployView }) {
  const links: readonly { readonly label: string; readonly view: OsdeployView; readonly href: string }[] = [
    { label: "Overview", view: "overview", href: "/react/osdeploy" },
    { label: "Builder", view: "builder", href: "/react/osdeploy?view=builder" },
    { label: "Cache", view: "cache", href: "/react/osdeploy?view=cache" },
    { label: "Artifacts", view: "artifacts", href: "/react/osdeploy?view=artifacts" }
  ];
  return (
    <nav className="cloudosd-subnav" aria-label="OSDeploy pages">
      <span>OSDeploy pages</span>
      {links.map((link) => (
        <a key={link.view} href={link.href} aria-current={view === link.view ? "page" : undefined}>{link.label}</a>
      ))}
    </nav>
  );
}

function OperatorFlow({ payload }: { readonly payload: OsdeployPayload }) {
  const cacheSummary = payload.osdeploy_cache.summary ?? {};
  const cards = [
    {
      href: "/react/osdeploy?view=builder",
      title: "1. Build a server run",
      body: "Select a ready artifact, server role, Proxmox placement, sizing, and run preflight before launch.",
      value: String(payload.active_runs.length),
      note: "active runs"
    },
    {
      href: "/react/osdeploy?view=cache",
      title: "2. Warm Server cache",
      body: "Keep Windows Server image and update content warm before build or provision flows consume it.",
      value: `${numberText(cacheSummary.ready)}/${numberText(cacheSummary.total)}`,
      note: "ready cache entries"
    },
    {
      href: "/react/osdeploy?view=artifacts",
      title: "3. Manage artifacts",
      body: "Build, inspect, publish, and repair OSDeploy PE artifacts and build-host agents.",
      value: String(payload.ready_artifacts.length),
      note: "ready artifacts"
    }
  ] as const;
  return (
    <Panel title="Operator Flow" action={<a className="utility-button" href="/react/provision">Batch provision</a>}>
      <div className="cloudosd-flow">
        {cards.map((card) => (
          <a className="cloudosd-flow-card" href={card.href} key={card.href}>
            <strong>{card.title}</strong>
            <span>{card.body}</span>
            <b>{card.value}</b>
            <span>{card.note}</span>
          </a>
        ))}
      </div>
    </Panel>
  );
}

function Overview({ payload, actionMessage, onArchiveAction }: {
  readonly payload: OsdeployPayload;
  readonly actionMessage: string;
  readonly onArchiveAction: (endpoint: string) => void;
}) {
  const defaults = payload.catalog.defaults ?? {};
  return (
    <>
      <section className="metric-strip" aria-label="OSDeploy status metrics">
        <Metric label="Module pin" value={textValue(defaults.osdeploy_module_version)} />
        <Metric label="Ready artifacts" value={String(payload.ready_artifacts.length)} />
        <Metric label="Minimum RAM" value={`${numberText(defaults.minimum_vm_memory_mb)} MB`} />
        <Metric label="Active runs" value={String(payload.active_runs.length)} />
      </section>
      <Panel
        title="OSDeploy Run History"
        action={(
          <div className="button-row">
            <a className="utility-button" href="/react/osdeploy">Default history</a>
            <a className="utility-button" href="/react/osdeploy?archived=1">Show archived</a>
            <button type="button" onClick={() => { onArchiveAction("/api/osdeploy/v1/runs/archive-stale-failed"); }}>Archive stale failed</button>
            <button type="button" onClick={() => { onArchiveAction("/api/osdeploy/v1/runs/archive-completed-old"); }}>Hide completed old</button>
          </div>
        )}
      >
        {actionMessage ? <p className="notice">{actionMessage}</p> : null}
        <div className="cloudosd-run-strip">
          <RunStrip title="Active Runs" runs={payload.active_runs} />
          <RunStrip title="Stale Failed Runs" runs={payload.stale_failed_runs} archiveAction={onArchiveAction} />
        </div>
        <RunsTable payload={payload} archiveAction={onArchiveAction} />
      </Panel>
    </>
  );
}

function RunStrip({
  title,
  runs,
  archiveAction
}: {
  readonly title: string;
  readonly runs: readonly OsdeployRun[];
  readonly archiveAction?: (endpoint: string) => void;
}) {
  return (
    <article className="cloudosd-strip-card">
      <h3>{title}</h3>
      <div className="cloudosd-run-list">
        {runs.length ? runs.map((run) => {
          const runId = textValue(run.run_id);
          return (
            <div className="cloudosd-run-line" key={runId}>
              <a href={`/react/osdeploy/runs/${encodeURIComponent(runId)}`}>{textValue(run.requested_vm_name, "run")}</a>
              {archiveAction ? (
                <button type="button" onClick={() => { archiveAction(`/api/osdeploy/v1/runs/${encodeURIComponent(runId)}/archive`); }}>Archive</button>
              ) : <span>{textValue(run.state)}</span>}
            </div>
          );
        }) : <span className="empty">None.</span>}
      </div>
    </article>
  );
}

function RunsTable({ payload, archiveAction }: {
  readonly payload: OsdeployPayload;
  readonly archiveAction: (endpoint: string) => void;
}) {
  if (!payload.runs.length) {
    return <p className="empty">No OSDeploy runs yet.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="jobs-table cloudosd-table" aria-label="OSDeploy runs">
        <thead>
          <tr>
            <th scope="col">State</th>
            <th scope="col">Requested</th>
            <th scope="col">VMID</th>
            <th scope="col">Server role</th>
            <th scope="col">OS</th>
            <th scope="col">Created</th>
            <th scope="col">Heartbeat</th>
            <th scope="col">Artifact</th>
            <th scope="col">Run</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {payload.runs.map((run) => {
            const artifact = artifactForRun(run, payload.artifacts);
            const runId = textValue(run.run_id);
            return (
              <tr key={runId}>
                <td><StatusBadge value={textValue(run.state)} /></td>
                <td>{textValue(run.requested_vm_name ?? run.vm_name ?? run.expected_computer_name)}<br /><small>{textValue(run.expected_computer_name)}</small></td>
                <td>{textValue(run.vmid)}</td>
                <td>{textValue(run.server_role).replaceAll("_", " ")}</td>
                <td>{[run.os_version, run.os_edition].filter(Boolean).join(" ") || "-"}</td>
                <td>{formatShortDateTime(run.created_at)}</td>
                <td>{formatShortDateTime(run.first_heartbeat_at)}</td>
                <td><code>{textValue(artifact?.build_sha ?? run.artifact_id)}</code></td>
                <td><a href={`/react/osdeploy/runs/${encodeURIComponent(runId)}`}>Open</a></td>
                <td>
                  <button
                    type="button"
                    onClick={() => { archiveAction(`/api/osdeploy/v1/runs/${encodeURIComponent(runId)}/${run.archived ? "unarchive" : "archive"}`); }}
                  >
                    {run.archived ? "Restore" : "Archive"}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function csvList(value: string): readonly string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function fieldValue(form: HTMLFormElement, name: string): string {
  const control = form.elements.namedItem(name);
  return control instanceof HTMLInputElement || control instanceof HTMLSelectElement || control instanceof HTMLTextAreaElement ? control.value : "";
}

function roleOptionsPayload(form: HTMLFormElement, role: string): Readonly<Record<string, unknown>> {
  if (role === "file_server") {
    return {
      share_name: fieldValue(form, "role_file_share_name"),
      share_path: fieldValue(form, "role_file_share_path"),
      full_access_principals: csvList(fieldValue(form, "role_file_full_access")),
      change_access_principals: csvList(fieldValue(form, "role_file_change_access")),
      read_access_principals: csvList(fieldValue(form, "role_file_read_access"))
    };
  }
  if (role === "isolated_domain_controller") {
    return {
      forest_fqdn: fieldValue(form, "role_dc_forest_fqdn"),
      netbios_name: fieldValue(form, "role_dc_netbios_name"),
      forest_admin_credential_id: Number(fieldValue(form, "role_dc_forest_admin_credential_id") || 0),
      dsrm_credential_id: Number(fieldValue(form, "role_dc_dsrm_credential_id") || 0)
    };
  }
  if (role === "mecm_prereq") {
    return {
      prereq_profile: fieldValue(form, "role_mecm_prereq_profile"),
      content_root: fieldValue(form, "role_mecm_content_root")
    };
  }
  if (role === "lab_in_a_box") {
    let children: unknown;
    try {
      children = JSON.parse(fieldValue(form, "role_lab_children_json") || "[]");
    } catch {
      children = [];
    }
    return {
      bundle_name: fieldValue(form, "role_lab_bundle_name"),
      domain_join_credential_id: Number(fieldValue(form, "role_lab_domain_join_credential_id") || 0),
      children
    };
  }
  return {};
}

function resolveNameTemplate(template: string, role: string): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  const tokens: Readonly<Record<string, string>> = {
    role: role === "isolated_domain_controller" ? "DC" : role === "file_server" ? "FS" : role === "mecm_prereq" ? "MECM" : role === "lab_in_a_box" ? "LAB" : "BASE",
    date: `${String(now.getFullYear()).slice(-2)}${pad(now.getMonth() + 1)}${pad(now.getDate())}`,
    time: `${pad(now.getHours())}${pad(now.getMinutes())}`,
    rand: "0000"
  };
  const resolved = template
    .replace(/\{([A-Za-z0-9_]+)\}/gu, (_match, key: string) => tokens[key] ?? "")
    .replace(/\s+/gu, "-")
    .replace(/[^A-Za-z0-9_.-]/gu, "-")
    .replace(/-+/gu, "-")
    .replace(/^-+|-+$/gu, "");
  return resolved || "OSDEPLOY-SRV-001";
}

function formPayload(form: HTMLFormElement): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  new FormData(form).forEach((value, key) => {
    if (value !== "") {
      payload[key] = value;
    }
  });
  const role = textValue(payload.server_role, "base");
  payload.vm_name = resolveNameTemplate(textValue(payload.vm_name, "OSDEPLOY-{role}-{date}-{rand}"), role);
  for (const key of ["vm_cores", "vm_memory_mb", "vm_disk_size_gb", "vmid"]) {
    if (payload[key] !== undefined) {
      payload[key] = Number(payload[key]);
    }
  }
  payload.secure_boot = form.elements.namedItem("secure_boot") instanceof HTMLInputElement
    ? (form.elements.namedItem("secure_boot") as HTMLInputElement).checked
    : false;
  payload.architecture = "amd64";
  payload.outbound_policy = {};
  payload.role_options = roleOptionsPayload(form, role);
  return payload;
}

function credentialOptions(credentials: readonly CredentialOption[]) {
  return (
    <>
      <option value="">select credential</option>
      {credentials.map((credential) => (
        <option key={textValue(credential.id)} value={textValue(credential.id)}>
          {[credential.name, credential.type].filter(Boolean).join(" / ")}
        </option>
      ))}
    </>
  );
}

function RoleVariables({ role, metadata, credentials }: {
  readonly role: string;
  readonly metadata: RoleMetadata;
  readonly credentials: readonly CredentialOption[];
}) {
  const requiredFields = metadata.required_fields ?? [];
  return (
    <Panel title="Role variables">
      <dl className="utility-definition-grid">
        <MetricTermText label="Role" value={metadata.name ?? role.replaceAll("_", " ")} />
        <MetricTermText label="Readiness" value={metadata.readiness_status_name ?? metadata.readiness_status ?? `${role}_ready`} />
        <MetricTermText label="Required fields" value={requiredFields.length ? requiredFields.join(", ") : "none"} />
        <MetricTermText label="Generated steps" value={(metadata.step_kinds ?? []).join(", ") || "base only"} />
      </dl>
      {role === "base" ? <p className="empty">No additional variables are required for the base server role.</p> : null}
      {role === "file_server" ? (
        <div className="cloudosd-dense-grid">
          <Field label="Share name"><input name="role_file_share_name" defaultValue="Shared" /></Field>
          <Field label="Share path"><input name="role_file_share_path" defaultValue="C:\\Shares\\Shared" /></Field>
          <Field label="Full access"><input name="role_file_full_access" defaultValue="HOME\\Domain Admins" /></Field>
          <Field label="Change access"><input name="role_file_change_access" defaultValue="HOME\\Domain Users" /></Field>
          <Field label="Read access"><input name="role_file_read_access" /></Field>
        </div>
      ) : null}
      {role === "isolated_domain_controller" ? (
        <div className="cloudosd-dense-grid">
          <Field label="Domain / forest FQDN"><input name="role_dc_forest_fqdn" defaultValue="lab.gell.one" /></Field>
          <Field label="NetBIOS name"><input name="role_dc_netbios_name" defaultValue="LAB" /></Field>
          <Field label="Forest admin credential"><select name="role_dc_forest_admin_credential_id">{credentialOptions(credentials)}</select></Field>
          <Field label="DSRM credential"><select name="role_dc_dsrm_credential_id">{credentialOptions(credentials)}</select></Field>
        </div>
      ) : null}
      {role === "mecm_prereq" ? (
        <div className="cloudosd-dense-grid">
          <Field label="Prereq profile"><select name="role_mecm_prereq_profile"><option value="site_server_foundation">site_server_foundation</option></select></Field>
          <Field label="Content root"><input name="role_mecm_content_root" defaultValue="C:\\MECMContent" /></Field>
        </div>
      ) : null}
      {role === "lab_in_a_box" ? (
        <div className="cloudosd-dense-grid">
          <Field label="Bundle name"><input name="role_lab_bundle_name" defaultValue="Lab Bundle 01" /></Field>
          <Field label="Domain join credential"><select name="role_lab_domain_join_credential_id">{credentialOptions(credentials)}</select></Field>
          <Field label="Child VM config JSON">
            <textarea name="role_lab_children_json" defaultValue={'[\n  {"role":"isolated_domain_controller","vm_name":"LAB-DC01","role_options":{"forest_fqdn":"lab.gell.one","netbios_name":"LAB","forest_admin_credential_id":0,"dsrm_credential_id":0}}\n]'} />
          </Field>
        </div>
      ) : null}
    </Panel>
  );
}

function Builder({ payload }: { readonly payload: OsdeployPayload }) {
  const [preflight, setPreflight] = useState<PreflightResult | null>(null);
  const [status, setStatus] = useState("Waiting for input.");
  const [error, setError] = useState("");
  const [role, setRole] = useState("base");
  const [actionState, setActionState] = useState<ActionState>("idle");
  const defaults = payload.catalog.defaults ?? {};
  const target = payload.proxmox_options;
  const selectedArtifact = payload.ready_artifacts[0];
  const roleCatalog = payload.catalog.role_catalog ?? {};

  const runPreflight = useCallback(async () => {
    const form = document.querySelector<HTMLFormElement>("[data-osdeploy-run-form]");
    if (!form) {
      return;
    }
    setActionState("working");
    setStatus("Checking");
    setError("");
    try {
      const result = await postJson<PreflightResult>("/api/osdeploy/v1/preflight", formPayload(form));
      setPreflight(result);
      setStatus(result.launch_allowed ? "Launch allowed" : "Launch blocked");
      setActionState(result.launch_allowed ? "ready" : "idle");
    } catch (err) {
      setPreflight(null);
      setStatus("Preflight failed");
      setError(err instanceof Error ? err.message : "Preflight failed");
      setActionState("failed");
    }
  }, []);

  const launch = useCallback(async () => {
    const form = document.querySelector<HTMLFormElement>("[data-osdeploy-run-form]");
    if (!form || !preflight?.launch_allowed) {
      return;
    }
    setActionState("working");
    setError("");
    try {
      const body = formPayload(form);
      if (textValue(body.server_role) === "lab_in_a_box") {
        const bundle = await postJson<{ readonly children?: readonly { readonly child_run_id?: string }[] }>("/api/osdeploy/v1/bundles", body);
        const firstRun = bundle.children?.[0]?.child_run_id;
        if (firstRun) {
          window.location.assign(`/react/osdeploy/runs/${encodeURIComponent(firstRun)}`);
        }
        return;
      }
      const created = await postJson<{ readonly run: { readonly run_id: string } }>("/api/osdeploy/v1/runs", body);
      await postJson(`/api/osdeploy/v1/runs/${encodeURIComponent(created.run.run_id)}/provision`);
      window.location.assign(`/react/osdeploy/runs/${encodeURIComponent(created.run.run_id)}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Launch failed");
      setActionState("failed");
    }
  }, [preflight]);

  return (
    <form className="cloudosd-layout" data-osdeploy-run-form>
      <div className="cloudosd-stack">
        <Panel title="Server Deployment">
          <h3>Target</h3>
          <div className="cloudosd-dense-grid">
            <Field label="Deployable OS" help="Ready artifacts include hashes and a Proxmox ISO volid.">
              <select name="artifact_id" aria-label="Deployable OS" defaultValue={selectedArtifact?.id ?? ""} disabled={!payload.ready_artifacts.length} required>
                {payload.ready_artifacts.map((artifact) => <option key={textValue(artifact.id)} value={textValue(artifact.id)}>{artifactLabel(artifact)}</option>)}
              </select>
            </Field>
            <Field label="Requested VM name"><input name="vm_name" aria-label="Requested VM name" defaultValue="OSDEPLOY-{role}-{date}-{rand}" required /></Field>
            <Field label="Optional VMID"><input name="vmid" aria-label="Optional VMID" type="number" min="1" placeholder="auto" /></Field>
            <Field label="Server role">
              <select name="server_role" aria-label="Server role" value={role} onChange={(event) => { setRole(event.currentTarget.value); }}>
                {optionsFrom(payload.catalog.server_roles, "base").map((serverRole) => <option key={serverRole} value={serverRole}>{serverRole.replaceAll("_", " ")}</option>)}
              </select>
            </Field>
          </div>
          <h3>Placement</h3>
          <div className="cloudosd-dense-grid">
            <SelectField label="Node" name="node" defaultValue={target.defaults?.node ?? ""} options={optionsFrom(target.nodes, target.defaults?.node)} />
            <SelectField label="ISO storage" name="iso_storage" defaultValue={target.defaults?.iso_storage ?? ""} options={optionsFrom(target.storages?.iso, target.defaults?.iso_storage)} />
            <SelectField label="VM disk storage" name="storage" defaultValue={target.defaults?.disk_storage ?? ""} options={optionsFrom(target.storages?.disk, target.defaults?.disk_storage)} />
            <NetworkTargetField options={target} defaultValue={target.defaults?.bridge ?? ""} />
          </div>
          <h3>Image</h3>
          <div className="cloudosd-dense-grid">
            <SelectField label="Version" name="os_version" defaultValue={textValue(selectedArtifact?.os_version ?? defaults.os_version, "")} options={optionsFrom(payload.catalog.os_versions, textValue(defaults.os_version, ""))} />
            <SelectField label="Edition" name="os_edition" defaultValue={textValue(selectedArtifact?.os_edition ?? defaults.os_edition, "")} options={optionsFrom(payload.catalog.os_editions, textValue(defaults.os_edition, ""))} />
            <SelectField label="Language" name="os_language" defaultValue={textValue(selectedArtifact?.os_language ?? defaults.os_language, "")} options={optionsFrom(payload.catalog.os_languages, textValue(defaults.os_language, ""))} />
            <Field label="vCPU"><input name="vm_cores" aria-label="vCPU" type="number" min="1" defaultValue={numberText(defaults.vm_cores, "4")} /></Field>
            <Field label="Memory MB"><input name="vm_memory_mb" aria-label="Memory MB" type="number" min={numberText(defaults.minimum_vm_memory_mb, "4096")} step="512" defaultValue={numberText(defaults.vm_memory_mb, "8192")} /></Field>
            <Field label="Disk GB"><input name="vm_disk_size_gb" aria-label="Disk GB" type="number" min={numberText(defaults.minimum_vm_disk_size_gb, "80")} defaultValue={numberText(defaults.vm_disk_size_gb, "100")} /></Field>
          </div>
          <div className="cloudosd-toggle-set">
            <label><input type="checkbox" name="secure_boot" /> Secure Boot</label>
          </div>
        </Panel>
        <RoleVariables role={role} metadata={roleCatalog[role] ?? {}} credentials={payload.osdeploy_credentials} />
        <Panel title="Preflight" action={<button type="button" onClick={() => { void runPreflight(); }}>Run Preflight</button>}>
          <p>{status}</p>
          <div className="cloudosd-checks">
            <CheckList title="Blocking Checks" checks={preflight?.blocking_checks ?? []} empty="No blocking checks." />
            <CheckList title="Warnings" checks={preflight?.warnings ?? []} empty="No warnings." />
          </div>
          {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
        </Panel>
      </div>
      <aside className="panel cloudosd-review" aria-label="Review and launch">
        <div className="panel__header"><h2>Review &amp; Launch</h2></div>
        <dl className="utility-definition-grid">
          <MetricTermText label="Artifact" value={textValue(selectedArtifact?.readiness, "not selected")} />
          <MetricTermText label="Build" value={selectedArtifact ? `${textValue(selectedArtifact.build_sha)} / ${textValue(selectedArtifact.osdeploy_module_version)}` : "-"} />
          <MetricTermText label="Computer name" value={textValue(preflight?.target?.computer_name)} />
          <MetricTermText label="Blocking count" value={String(preflight?.blocking_checks?.length ?? "-")} />
        </dl>
        <button type="button" disabled={!preflight?.launch_allowed || actionState === "working"} onClick={() => { void launch(); }}>Launch OSDeploy VM</button>
      </aside>
    </form>
  );
}

function MetricTermText({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function CheckList({ title, checks, empty }: { readonly title: string; readonly checks: readonly CheckItem[]; readonly empty: string }) {
  return (
    <div>
      <h3>{title}</h3>
      <ul className="cloudosd-check-list">
        {checks.length ? checks.map((check) => (
          <li key={`${textValue(check.id ?? check.label)}:${textValue(check.message ?? check.detail)}`}>
            {textValue(check.id ?? check.label)}: {textValue(check.message ?? check.detail)}
          </li>
        )) : <li>{empty}</li>}
      </ul>
    </div>
  );
}

function Cache({ payload, actionMessage, onCacheAction }: {
  readonly payload: OsdeployPayload;
  readonly actionMessage: string;
  readonly onCacheAction: (endpoint: string) => void;
}) {
  const storage = payload.osdeploy_cache.storage ?? {};
  const summary = payload.osdeploy_cache.summary ?? {};
  const entries = payload.osdeploy_cache.entries ?? [];
  return (
    <Panel
      title="OSDeploy Cache"
      action={<button type="button" onClick={() => { onCacheAction("/api/osdeploy/v1/cache/catalog/refresh"); }}>Refresh catalog</button>}
    >
      {actionMessage ? <p className="notice">{actionMessage}</p> : null}
      <section className="metric-strip" aria-label="OSDeploy cache summary">
        <Metric label="Cache root" value={textValue(storage.root, "/app/cache/osdeploy")} />
        <Metric label="Storage" value={storage.ready ? "Ready" : "Not ready"} />
        <Metric label="Ready entries" value={`${numberText(summary.ready)}/${numberText(summary.total)}`} />
        <Metric label="Warming" value={numberText(summary.warming)} />
        <Metric label="Failed" value={numberText(summary.failed)} />
      </section>
      {entries.length ? (
        <div className="table-wrap">
          <table className="jobs-table cloudosd-table" aria-label="OSDeploy cache entries">
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Type</th>
                <th scope="col">Windows</th>
                <th scope="col">Selection</th>
                <th scope="col">File</th>
                <th scope="col">Size/hash</th>
                <th scope="col">Verified</th>
                <th scope="col">Error</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => {
                const id = textValue(entry.id);
                return (
                  <tr key={id}>
                    <td><StatusBadge value={textValue(entry.status)} /></td>
                    <td>{textValue(entry.entry_type).replaceAll("_", " ")}</td>
                    <td>{textValue(entry.windows_version)}<br /><small>{textValue(entry.architecture)}</small></td>
                    <td>{textValue(entry.edition, "all")} / {textValue(entry.language, "neutral")}</td>
                    <td><code>{textValue(entry.file_name)}</code></td>
                    <td>{textValue(entry.size_bytes ?? entry.expected_size_bytes)}<br /><code>{textValue(entry.sha256 ?? entry.expected_sha256)}</code></td>
                    <td>{textValue(entry.verified_at)}<br /><small>{textValue(entry.last_served_at, "not served")}</small></td>
                    <td>{textValue(entry.error)}</td>
                    <td>
                      <div className="button-row">
                        <button type="button" onClick={() => { onCacheAction(`/api/osdeploy/v1/cache/${encodeURIComponent(id)}/warm`); }}>Warm</button>
                        <button type="button" onClick={() => { onCacheAction(`/api/osdeploy/v1/cache/${encodeURIComponent(id)}/verify`); }}>Verify</button>
                        <button type="button" onClick={() => { onCacheAction(`/api/osdeploy/v1/cache/${encodeURIComponent(id)}/delete`); }}>Delete</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : <p className="empty">No OSDeploy cache entries yet.</p>}
    </Panel>
  );
}

function Artifacts({ payload, actionMessage, onArtifactAction }: {
  readonly payload: OsdeployPayload;
  readonly actionMessage: string;
  readonly onArtifactAction: (endpoint: string) => void;
}) {
  const [status, setStatus] = useState("");
  const defaults = payload.osdeploy_build_defaults;
  const moduleVersion = textValue(payload.catalog.defaults?.osdeploy_module_version, "1.0.0");

  const submitBuild = useCallback(async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const body: Record<string, unknown> = {};
    new FormData(event.currentTarget).forEach((value, key) => {
      body[key] = value;
    });
    if (body.image_index !== undefined) {
      body.image_index = Number(body.image_index);
    }
    setStatus("Preflighting");
    try {
      const preflight = await postJson<{ readonly blocking_checks?: readonly CheckItem[] }>("/api/osdeploy/v1/artifacts/build/preflight", body);
      if (preflight.blocking_checks?.length) {
        setStatus(`Blocked: ${preflight.blocking_checks.map((check) => textValue(check.id ?? check.label)).join(", ")}`);
        return;
      }
      setStatus("Queueing");
      const result = await postJson<{ readonly job_id?: string; readonly work_item_id?: string }>("/api/osdeploy/v1/artifacts/build", body);
      setStatus(textValue(result.job_id ?? result.work_item_id, "Queued"));
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Build failed");
    }
  }, []);

  return (
    <Panel title="Artifacts">
      <form className="cloudosd-artifact-form" data-testid="osdeploy-build-form" onSubmit={(event) => { void submitBuild(event); }}>
        <select name="build_mode" aria-label="Build mode" defaultValue="auto">
          <option value="auto">Auto</option>
          <option value="build_host_agent">Build-host agent</option>
          <option value="ssh">Direct SSH</option>
        </select>
        <input name="remote" aria-label="Remote host" defaultValue={textValue(defaults.remote, "")} />
        <input name="remote_root" aria-label="Remote root" defaultValue={textValue(defaults.remote_root, "")} />
        <input name="build_host_agent_id" aria-label="Build-host agent ID" placeholder="Build-host agent ID" />
        <input name="architecture" aria-label="Architecture" defaultValue="amd64" readOnly />
        <input name="source_media_path" aria-label="Source media path" placeholder="Source media path" />
        <input name="image_name" aria-label="Image name" defaultValue="Windows Server 2022 Datacenter (Desktop Experience)" />
        <input name="image_index" aria-label="Image index" defaultValue="4" inputMode="numeric" />
        <input name="os_version" aria-label="OS version" defaultValue="Windows Server 2022" />
        <select name="os_edition" aria-label="OS edition" defaultValue="Datacenter">
          <option value="Datacenter">Datacenter</option>
          <option value="Standard">Standard</option>
        </select>
        <input name="os_language" aria-label="OS language" defaultValue="en-us" />
        <input name="osdeploy_module_version" aria-label="OSDeploy module" defaultValue={moduleVersion} />
        <button type="submit">Build Artifact</button>
        {status ? <span>{status}</span> : null}
      </form>
      <p className="cloudosd-field-help">SSH key: <code>{textValue(defaults.ssh_key_path)}</code> {defaults.ssh_key_exists ? "ready" : "missing"}</p>
      {defaults.ssh_public_key ? <textarea className="cloudosd-public-key" readOnly aria-label="OSDeploy build host public key" value={defaults.ssh_public_key} /> : null}
      {actionMessage ? <p className="notice">{actionMessage}</p> : null}
      {payload.artifacts.length ? (
        <div className="table-wrap">
          <table className="jobs-table cloudosd-table" aria-label="OSDeploy artifacts">
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Architecture</th>
                <th scope="col">OSDeploy</th>
                <th scope="col">OSDBuilder</th>
                <th scope="col">ADK</th>
                <th scope="col">Build SHA</th>
                <th scope="col">Image</th>
                <th scope="col">Paths</th>
                <th scope="col">Proxmox volid</th>
                <th scope="col">Publish</th>
              </tr>
            </thead>
            <tbody>
              {payload.artifacts.map((artifact) => {
                const id = textValue(artifact.id);
                return (
                  <tr key={id}>
                    <td><StatusBadge value={textValue(artifact.readiness)} /></td>
                    <td><code>{textValue(artifact.architecture)}</code></td>
                    <td><code>{textValue(artifact.osdeploy_module_version)}</code></td>
                    <td><code>{textValue(artifact.osdbuilder_module_version)}</code></td>
                    <td><code>{textValue(artifact.adk_version)}</code></td>
                    <td><code>{textValue(artifact.build_sha)}</code></td>
                    <td>{textValue(artifact.image_name)}<br /><small>index {textValue(artifact.image_index)}</small></td>
                    <td><code>{textValue(artifact.iso_path)}</code><br /><code>{textValue(artifact.wim_path)}</code></td>
                    <td><code>{textValue(artifact.proxmox_volid, "not uploaded")}</code></td>
                    <td><button type="button" onClick={() => { onArtifactAction(`/api/osdeploy/v1/artifacts/${encodeURIComponent(id)}/publish`); }}>Publish</button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : <p className="empty">No OSDeploy artifacts yet.</p>}
    </Panel>
  );
}

export function OsdeployPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<OsdeployPayload>(emptyPayload);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionMessage, setActionMessage] = useState("");

  const requestedView = useMemo(() => normalizeView(new URLSearchParams(window.location.search).get("view")), []);

  const load = useCallback(async () => {
    try {
      const search = window.location.search || (requestedView === "overview" ? "" : `?view=${requestedView}`);
      setPayload(normalizePayload(await fetchJson<unknown>(`/api/osdeploy/page${search}`)));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load OSDeploy");
    } finally {
      setLoading(false);
    }
  }, [requestedView]);

  usePolling(load);

  const postAction = useCallback(async (endpoint: string) => {
    setActionMessage("Working");
    try {
      const result = await postJson<{ readonly job_id?: string; readonly archived_count?: number }>(endpoint);
      if (result.job_id) {
        setActionMessage(`Queued ${result.job_id}`);
      } else if (typeof result.archived_count === "number") {
        setActionMessage(`Archived ${String(result.archived_count)}`);
      } else {
        setActionMessage("Done");
      }
      await load();
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : "Action failed");
    }
  }, [load]);

  return (
    <PageFrame bootstrap={bootstrap} title="OSDeploy Server" section="Deploy" path="/react/osdeploy">
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading OSDeploy</span>
          <div className="load-strip__track" role="progressbar" aria-label="OSDeploy loading"><span /></div>
        </div>
      ) : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <div className="cloudosd-cockpit">
        <Subnav view={payload.view} />
        <OperatorFlow payload={payload} />
        {payload.view === "overview" ? <Overview payload={payload} actionMessage={actionMessage} onArchiveAction={(endpoint) => { void postAction(endpoint); }} /> : null}
        {payload.view === "builder" ? <Builder payload={payload} /> : null}
        {payload.view === "cache" ? <Cache payload={payload} actionMessage={actionMessage} onCacheAction={(endpoint) => { void postAction(endpoint); }} /> : null}
        {payload.view === "artifacts" ? <Artifacts payload={payload} actionMessage={actionMessage} onArtifactAction={(endpoint) => { void postAction(endpoint); }} /> : null}
      </div>
    </PageFrame>
  );
}
