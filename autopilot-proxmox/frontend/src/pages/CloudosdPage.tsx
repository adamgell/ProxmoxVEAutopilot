import { useCallback, useMemo, useState } from "react";

import { fetchJson, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { networkTargetOptions, type NetworkTargetOption } from "../networkTargets";
import { textValue } from "../utilityModels";

type CloudosdView = "overview" | "builder" | "cache" | "artifacts";
type ActionState = "idle" | "working" | "ready" | "failed";

interface CloudosdPayload {
  readonly artifacts: readonly CloudosdArtifact[];
  readonly ready_artifacts: readonly CloudosdArtifact[];
  readonly runs: readonly CloudosdRun[];
  readonly active_runs: readonly CloudosdRun[];
  readonly stale_failed_runs: readonly CloudosdRun[];
  readonly cloudosd_cache: CloudosdCache;
  readonly catalog: CatalogPayload;
  readonly proxmox_options: ProxmoxOptionsPayload;
  readonly view: CloudosdView;
}

interface CloudosdArtifact {
  readonly id?: string;
  readonly architecture?: string;
  readonly osdcloud_module_version?: string;
  readonly build_sha?: string;
  readonly readiness?: string;
  readonly ready?: boolean;
  readonly iso_path?: string;
  readonly wim_path?: string;
  readonly manifest_path?: string;
  readonly iso_sha256?: string;
  readonly wim_sha256?: string;
  readonly proxmox_volid?: string;
  readonly built_by_host?: string;
  readonly built_at?: string;
  readonly build_job_url?: string;
  readonly build_log_url?: string;
}

interface CloudosdRun {
  readonly run_id?: string;
  readonly artifact_id?: string;
  readonly requested_vm_name?: string;
  readonly pve_vm_name?: string;
  readonly heartbeat_computer_name?: string;
  readonly vmid?: string | number;
  readonly state?: string;
  readonly os_version?: string;
  readonly os_edition?: string;
  readonly os_activation?: string;
  readonly created_at?: string;
  readonly first_heartbeat_at?: string;
  readonly archived?: boolean;
  readonly archived_at?: string;
  readonly name_comparison?: {
    readonly mismatch?: boolean;
    readonly requested_was_normalized?: boolean;
  };
  readonly intune_evidence?: Readonly<Record<string, { readonly status?: string }>>;
}

interface CloudosdCache {
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
  readonly activation?: string;
  readonly language?: string;
  readonly kb?: string;
  readonly title?: string;
  readonly file_name?: string;
  readonly size_bytes?: number;
  readonly expected_size_bytes?: number;
  readonly sha256?: string;
  readonly expected_sha256?: string;
  readonly sha1?: string;
  readonly expected_sha1?: string;
  readonly verified_at?: string;
  readonly last_served_at?: string;
  readonly served_count?: number;
  readonly error?: string;
}

interface CatalogPayload {
  readonly os_versions?: readonly string[];
  readonly os_editions?: readonly string[];
  readonly os_activations?: readonly string[];
  readonly os_languages?: readonly string[];
  readonly defaults?: Readonly<Record<string, unknown>>;
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

interface PreflightResult {
  readonly launch_allowed: boolean;
  readonly normalized_computer_name?: string;
  readonly blocking_checks?: readonly CheckItem[];
  readonly warnings?: readonly CheckItem[];
}

interface CheckItem {
  readonly label?: string;
  readonly detail?: string;
}

const emptyPayload: CloudosdPayload = {
  artifacts: [],
  ready_artifacts: [],
  runs: [],
  active_runs: [],
  stale_failed_runs: [],
  cloudosd_cache: {},
  catalog: {},
  proxmox_options: {},
  view: "overview"
};

function asRecord(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Readonly<Record<string, unknown>> : {};
}

function asArtifacts(value: unknown): readonly CloudosdArtifact[] {
  return Array.isArray(value) ? value as readonly CloudosdArtifact[] : [];
}

function asRuns(value: unknown): readonly CloudosdRun[] {
  return Array.isArray(value) ? value as readonly CloudosdRun[] : [];
}

function asCache(value: unknown): CloudosdCache {
  return asRecord(value);
}

function asCatalog(value: unknown): CatalogPayload {
  return asRecord(value);
}

function asProxmoxOptions(value: unknown): ProxmoxOptionsPayload {
  return asRecord(value);
}

function normalizeView(value: unknown): CloudosdView {
  return value === "builder" || value === "cache" || value === "artifacts" ? value : "overview";
}

function normalizePayload(value: unknown): CloudosdPayload {
  const record = asRecord(value);
  return {
    artifacts: asArtifacts(record.artifacts),
    ready_artifacts: asArtifacts(record.ready_artifacts),
    runs: asRuns(record.runs),
    active_runs: asRuns(record.active_runs),
    stale_failed_runs: asRuns(record.stale_failed_runs),
    cloudosd_cache: asCache(record.cloudosd_cache),
    catalog: asCatalog(record.catalog),
    proxmox_options: asProxmoxOptions(record.proxmox_options),
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

function artifactLabel(artifact: CloudosdArtifact): string {
  return [artifact.architecture, artifact.osdcloud_module_version, artifact.build_sha].filter(Boolean).join(" / ") || textValue(artifact.id);
}

function artifactForRun(run: CloudosdRun, artifacts: readonly CloudosdArtifact[]): CloudosdArtifact | undefined {
  return artifacts.find((artifact) => artifact.id === run.artifact_id);
}

function badgeTone(status: string): string {
  const lower = status.toLowerCase();
  if (lower.includes("ready") || lower.includes("complete") || lower.includes("done") || lower.includes("uploaded")) {
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

function MetricValue({ label, value, note }: { readonly label: string; readonly value: string; readonly note?: string }) {
  return (
    <div className="cloudosd-value">
      <span>{label}</span>
      <strong>{value}</strong>
      {note ? <small>{note}</small> : null}
    </div>
  );
}

function Subnav({ view }: { readonly view: CloudosdView }) {
  const links: readonly { readonly label: string; readonly view: CloudosdView; readonly href: string }[] = [
    { label: "Overview", view: "overview", href: "/react/cloudosd" },
    { label: "Builder", view: "builder", href: "/react/cloudosd?view=builder" },
    { label: "Cache", view: "cache", href: "/react/cloudosd?view=cache" },
    { label: "Artifacts", view: "artifacts", href: "/react/cloudosd?view=artifacts" }
  ];
  return (
    <nav className="cloudosd-subnav" aria-label="OSDCloud pages">
      <span>OSDCloud pages</span>
      {links.map((link) => (
        <a key={link.view} href={link.href} aria-current={view === link.view ? "page" : undefined}>{link.label}</a>
      ))}
      <a href="https://www.osdcloud.com/" target="_blank" rel="noreferrer">Docs</a>
    </nav>
  );
}

function OperatorFlow({ payload }: { readonly payload: CloudosdPayload }) {
  const cacheSummary = payload.cloudosd_cache.summary ?? {};
  const cards = [
    {
      href: "/react/cloudosd?view=builder",
      title: "1. Build a desktop run",
      body: "Single-VM launch, preflight, selected artifact, Proxmox placement, Windows catalog, and heartbeat gate.",
      value: String(payload.active_runs.length),
      note: "active runs"
    },
    {
      href: "/react/cloudosd?view=cache",
      title: "2. Warm deployment cache",
      body: "Feature image cache plus latest quality update packages. Ready entries are served only after verification.",
      value: `${numberText(cacheSummary.ready)}/${numberText(cacheSummary.total)}`,
      note: "ready cache entries"
    },
    {
      href: "/react/cloudosd?view=artifacts",
      title: "3. Manage PE artifacts",
      body: "Build, inspect, publish, and verify OSDCloud PE ISO/WIM artifacts before launch.",
      value: String(payload.ready_artifacts.length),
      note: "ready artifacts"
    }
  ] as const;
  return (
    <Panel
      title="Operator Flow"
      action={<a className="utility-button" href="/react/provision">Batch provision</a>}
    >
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
  readonly payload: CloudosdPayload;
  readonly actionMessage: string;
  readonly onArchiveAction: (endpoint: string) => void;
}) {
  const defaults = payload.catalog.defaults ?? {};
  return (
    <>
      <section className="metric-strip" aria-label="OSDCloud status metrics">
        <Metric label="Module pin" value={textValue(defaults.osdcloud_module_version)} />
        <Metric label="Ready artifacts" value={String(payload.ready_artifacts.length)} />
        <Metric label="Minimum RAM" value={`${numberText(defaults.minimum_vm_memory_mb)} MB`} />
        <Metric label="Active runs" value={String(payload.active_runs.length)} />
        <Metric label="Analytics" value="Blocked" />
      </section>
      <Panel
        title="OSDCloud Run History"
        action={(
          <div className="button-row">
            <a className="utility-button" href="/react/cloudosd">Default history</a>
            <a className="utility-button" href="/react/cloudosd?archived=1">Show archived</a>
            <button type="button" onClick={() => { onArchiveAction("/api/cloudosd/runs/archive-stale-failed"); }}>Archive stale failed</button>
            <button type="button" onClick={() => { onArchiveAction("/api/cloudosd/runs/archive-completed-old"); }}>Hide completed old</button>
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
  readonly runs: readonly CloudosdRun[];
  readonly archiveAction?: (endpoint: string) => void;
}) {
  return (
    <article className="cloudosd-strip-card">
      <h3>{title}</h3>
      <div className="cloudosd-run-list">
        {runs.length ? runs.map((run) => (
          <div className="cloudosd-run-line" key={textValue(run.run_id)}>
            <a href={`/react/cloudosd/runs/${encodeURIComponent(textValue(run.run_id))}`}>{textValue(run.requested_vm_name, "run")}</a>
            {archiveAction ? (
              <button type="button" onClick={() => { archiveAction(`/api/cloudosd/runs/${encodeURIComponent(textValue(run.run_id))}/archive`); }}>Archive</button>
            ) : <span>{textValue(run.state)}</span>}
          </div>
        )) : <span className="empty">None.</span>}
      </div>
    </article>
  );
}

function RunsTable({ payload, archiveAction }: {
  readonly payload: CloudosdPayload;
  readonly archiveAction: (endpoint: string) => void;
}) {
  if (!payload.runs.length) {
    return <p className="empty">No OSDCloud runs yet.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="jobs-table cloudosd-table" aria-label="OSDCloud runs">
        <thead>
          <tr>
            <th scope="col">State</th>
            <th scope="col">Requested</th>
            <th scope="col">Final PVE</th>
            <th scope="col">Heartbeat</th>
            <th scope="col">VMID</th>
            <th scope="col">Artifact</th>
            <th scope="col">OS</th>
            <th scope="col">Hash</th>
            <th scope="col">Autopilot</th>
            <th scope="col">Assignment</th>
            <th scope="col">Enrollment</th>
            <th scope="col">Run</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {payload.runs.map((run) => {
            const artifact = artifactForRun(run, payload.artifacts);
            const evidence = run.intune_evidence ?? {};
            const runId = textValue(run.run_id);
            return (
              <tr key={runId}>
                <td><StatusBadge value={textValue(run.state)} /></td>
                <td>{textValue(run.requested_vm_name)}</td>
                <td>{textValue(run.pve_vm_name)}</td>
                <td>{textValue(run.heartbeat_computer_name)}</td>
                <td>{textValue(run.vmid)}</td>
                <td><code>{textValue(artifact?.build_sha ?? run.artifact_id)}</code></td>
                <td>{[run.os_version, run.os_edition, run.os_activation].filter(Boolean).join(" ") || "-"}</td>
                <td>{textValue(evidence.hash?.status)}</td>
                <td>{textValue(evidence.autopilot?.status)}</td>
                <td>{textValue(evidence.assignment?.status)}</td>
                <td>{textValue(evidence.enrollment?.status)}</td>
                <td><a href={`/react/cloudosd/runs/${encodeURIComponent(runId)}`}>Open</a></td>
                <td>
                  <button
                    type="button"
                    onClick={() => { archiveAction(`/api/cloudosd/runs/${encodeURIComponent(runId)}/${run.archived ? "unarchive" : "archive"}`); }}
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

function formData(form: HTMLFormElement): Record<string, unknown> {
  const data: Record<string, unknown> = {};
  new FormData(form).forEach((value, key) => {
    data[key] = value;
  });
  const payload: Record<string, unknown> = {};
  Object.entries(data).forEach(([key, value]) => {
    if (value !== "") {
      payload[key] = value;
    }
  });
  for (const key of ["vm_cores", "vm_memory_mb", "vm_disk_size_gb", "vmid"]) {
    if (payload[key] !== undefined) {
      payload[key] = Number(payload[key]);
    }
  }
  payload.architecture = "amd64";
  payload.tpm_enabled = form.elements.namedItem("tpm_enabled") instanceof HTMLInputElement
    ? (form.elements.namedItem("tpm_enabled") as HTMLInputElement).checked
    : false;
  payload.secure_boot = form.elements.namedItem("secure_boot") instanceof HTMLInputElement
    ? (form.elements.namedItem("secure_boot") as HTMLInputElement).checked
    : false;
  payload.firmware_updates_enabled = form.elements.namedItem("firmware_updates_enabled") instanceof HTMLInputElement
    ? (form.elements.namedItem("firmware_updates_enabled") as HTMLInputElement).checked
    : false;
  payload.analytics_enabled = form.elements.namedItem("analytics_enabled") instanceof HTMLInputElement
    ? (form.elements.namedItem("analytics_enabled") as HTMLInputElement).checked
    : false;
  payload.outbound_policy = { mode: textValue(payload.outbound_policy_mode, "blocked") };
  payload.outbound_policy_mode = undefined;
  return payload;
}

function Builder({ payload }: { readonly payload: CloudosdPayload }) {
  const [preflight, setPreflight] = useState<PreflightResult | null>(null);
  const [status, setStatus] = useState("Waiting for input.");
  const [error, setError] = useState("");
  const [actionState, setActionState] = useState<ActionState>("idle");
  const defaults = payload.catalog.defaults ?? {};
  const target = payload.proxmox_options;
  const selectedArtifact = payload.ready_artifacts[0];

  const runPreflight = useCallback(async () => {
    const form = document.querySelector<HTMLFormElement>("[data-cloudosd-run-form]");
    if (!form) {
      return;
    }
    setActionState("working");
    setStatus("Checking");
    setError("");
    try {
      const result = await postJson<PreflightResult>("/api/cloudosd/preflight", formData(form));
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
    const form = document.querySelector<HTMLFormElement>("[data-cloudosd-run-form]");
    if (!form || !preflight?.launch_allowed) {
      return;
    }
    setActionState("working");
    setError("");
    try {
      const run = await postJson<{ readonly run_id: string }>("/api/cloudosd/runs", formData(form));
      await postJson(`/api/cloudosd/runs/${encodeURIComponent(run.run_id)}/provision`);
      window.location.assign(`/react/cloudosd/runs/${encodeURIComponent(run.run_id)}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Launch failed");
      setActionState("failed");
    }
  }, [preflight]);

  return (
    <div className="cloudosd-layout">
      <div className="cloudosd-stack">
        <Panel title="Single-VM Deployment">
          <form className="cloudosd-builder-form" data-cloudosd-run-form>
            <h3>Artifact</h3>
            <div className="cloudosd-dense-grid">
              <Field label="Ready artifact" help="Ready artifacts include hashes and a Proxmox ISO volid.">
                <select name="artifact_id" aria-label="Ready artifact" defaultValue={selectedArtifact?.id ?? ""} disabled={!payload.ready_artifacts.length} required>
                  {payload.ready_artifacts.map((artifact) => <option key={textValue(artifact.id)} value={textValue(artifact.id)}>{artifactLabel(artifact)}</option>)}
                </select>
              </Field>
              <MetricValue label="Selected build" value={textValue(selectedArtifact?.build_sha)} note={textValue(selectedArtifact?.proxmox_volid)} />
            </div>
            <h3>Proxmox</h3>
            <div className="cloudosd-dense-grid">
              <SelectField label="Node" name="node" defaultValue={target.defaults?.node ?? ""} options={optionsFrom(target.nodes, target.defaults?.node)} />
              <SelectField label="ISO storage" name="iso_storage" defaultValue={target.defaults?.iso_storage ?? ""} options={optionsFrom(target.storages?.iso, target.defaults?.iso_storage)} />
              <SelectField label="VM disk storage" name="storage" defaultValue={target.defaults?.disk_storage ?? ""} options={optionsFrom(target.storages?.disk, target.defaults?.disk_storage)} />
              <NetworkTargetField options={target} defaultValue={target.defaults?.bridge ?? ""} />
              <Field label="Requested VM name"><input name="vm_name" aria-label="Requested VM name" defaultValue="CLOUDOSD-001" required /></Field>
              <Field label="Optional VMID"><input name="vmid" aria-label="Optional VMID" type="number" min="1" placeholder="auto" /></Field>
              <Field label="vCPU"><input name="vm_cores" aria-label="vCPU" type="number" min="1" defaultValue={numberText(defaults.vm_cores, "4")} /></Field>
              <Field label="Memory MB"><input name="vm_memory_mb" aria-label="Memory MB" type="number" min={numberText(defaults.minimum_vm_memory_mb, "4096")} step="512" defaultValue={numberText(defaults.vm_memory_mb, "8192")} /></Field>
              <Field label="Disk GB"><input name="vm_disk_size_gb" aria-label="Disk GB" type="number" min={numberText(defaults.minimum_vm_disk_size_gb, "64")} defaultValue={numberText(defaults.vm_disk_size_gb, "96")} /></Field>
            </div>
            <h3>Windows</h3>
            <div className="cloudosd-dense-grid">
              <SelectField label="Version" name="os_version" defaultValue={textValue(defaults.os_version, "")} options={optionsFrom(payload.catalog.os_versions, textValue(defaults.os_version, ""))} />
              <SelectField label="Edition" name="os_edition" defaultValue={textValue(defaults.os_edition, "")} options={optionsFrom(payload.catalog.os_editions, textValue(defaults.os_edition, ""))} />
              <SelectField label="Activation" name="os_activation" defaultValue={textValue(defaults.os_activation, "")} options={optionsFrom(payload.catalog.os_activations, textValue(defaults.os_activation, ""))} />
              <SelectField label="Language" name="os_language" defaultValue={textValue(defaults.os_language, "")} options={optionsFrom(payload.catalog.os_languages, textValue(defaults.os_language, ""))} />
            </div>
            <h3>Policy</h3>
            <div className="cloudosd-toggle-set">
              <label><input type="checkbox" name="tpm_enabled" defaultChecked /> TPM</label>
              <label><input type="checkbox" name="secure_boot" defaultChecked /> Secure Boot</label>
              <label><input type="checkbox" name="firmware_updates_enabled" /> Firmware updates</label>
              <label><input type="checkbox" name="analytics_enabled" /> Allow analytics</label>
            </div>
            <SelectField label="Driver pack policy" name="driver_pack_policy" defaultValue="None" options={["None", "OSDCloud"]} />
            <SelectField label="Outbound policy" name="outbound_policy_mode" defaultValue="blocked" options={["blocked", "limited"]} />
          </form>
        </Panel>
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
          <MetricTermText label="Build" value={selectedArtifact ? `${textValue(selectedArtifact.build_sha)} / ${textValue(selectedArtifact.osdcloud_module_version)}` : "-"} />
          <MetricTermText label="Windows name" value={textValue(preflight?.normalized_computer_name)} />
          <MetricTermText label="Blocking count" value={String(preflight?.blocking_checks?.length ?? "-")} />
        </dl>
        <button type="button" disabled={!preflight?.launch_allowed || actionState === "working"} onClick={() => { void launch(); }}>Launch OSDCloud VM</button>
      </aside>
    </div>
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
        {checks.length ? checks.map((check) => <li key={`${textValue(check.label)}:${textValue(check.detail)}`}>{textValue(check.label)}: {textValue(check.detail)}</li>) : <li>{empty}</li>}
      </ul>
    </div>
  );
}

function Cache({ payload, actionMessage, onCacheAction }: {
  readonly payload: CloudosdPayload;
  readonly actionMessage: string;
  readonly onCacheAction: (endpoint: string) => void;
}) {
  const storage = payload.cloudosd_cache.storage ?? {};
  const summary = payload.cloudosd_cache.summary ?? {};
  const entries = payload.cloudosd_cache.entries ?? [];
  return (
    <Panel
      title="OSDCloud Cache"
      action={(
        <div className="button-row">
          <button type="button" onClick={() => { onCacheAction("/api/cloudosd/cache/catalog/refresh"); }}>Refresh catalog</button>
          <button type="button" onClick={() => { onCacheAction("/api/cloudosd/cache/warm-all-windows11"); }}>Warm all Windows 11</button>
        </div>
      )}
    >
      {actionMessage ? <p className="notice">{actionMessage}</p> : null}
      <section className="metric-strip" aria-label="OSDCloud cache summary">
        <Metric label="Cache root" value={textValue(storage.root, "/app/cache/cloudosd")} />
        <Metric label="Storage" value={storage.ready ? "Ready" : "Not ready"} />
        <Metric label="Ready entries" value={`${numberText(summary.ready)}/${numberText(summary.total)}`} />
        <Metric label="Warming" value={numberText(summary.warming)} />
        <Metric label="Failed" value={numberText(summary.failed)} />
      </section>
      {entries.length ? (
        <div className="table-wrap">
          <table className="jobs-table cloudosd-table" aria-label="OSDCloud cache entries">
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Type</th>
                <th scope="col">Windows</th>
                <th scope="col">File</th>
                <th scope="col">Size/hash</th>
                <th scope="col">Verified</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => {
                const id = textValue(entry.id);
                const warmEndpoint = entry.entry_type === "feature_image"
                  ? `/api/cloudosd/cache/feature-images/${encodeURIComponent(id)}/warm`
                  : `/api/cloudosd/cache/quality-updates/${encodeURIComponent(id)}/warm`;
                return (
                  <tr key={id}>
                    <td><StatusBadge value={textValue(entry.status)} /></td>
                    <td>{textValue(entry.entry_type).replaceAll("_", " ")}</td>
                    <td>{textValue(entry.windows_version)}<br /><small>{textValue(entry.architecture)}</small></td>
                    <td><code>{textValue(entry.file_name)}</code></td>
                    <td>{textValue(entry.size_bytes ?? entry.expected_size_bytes)}<br /><code>{textValue(entry.sha256 ?? entry.expected_sha256 ?? entry.sha1 ?? entry.expected_sha1)}</code></td>
                    <td>{textValue(entry.verified_at)}</td>
                    <td>
                      <div className="button-row">
                        <button type="button" onClick={() => { onCacheAction(warmEndpoint); }}>Warm {id}</button>
                        <button type="button" onClick={() => { onCacheAction(`/api/cloudosd/cache/${encodeURIComponent(id)}/verify`); }}>Verify {id}</button>
                        <button type="button" onClick={() => { onCacheAction(`/api/cloudosd/cache/${encodeURIComponent(id)}/delete`); }}>Delete {id}</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : <p className="empty">No OSDCloud cache entries yet.</p>}
    </Panel>
  );
}

function Artifacts({ payload }: { readonly payload: CloudosdPayload }) {
  const [status, setStatus] = useState("");
  const moduleVersion = textValue(payload.catalog.defaults?.osdcloud_module_version, "26.4.17.1");

  const submitBuild = useCallback(async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const body: Record<string, unknown> = {};
    new FormData(event.currentTarget).forEach((value, key) => {
      body[key] = value;
    });
    setStatus("Queueing");
    try {
      const result = await postJson<{ readonly job_id?: string }>("/api/cloudosd/artifacts/build", body);
      setStatus(textValue(result.job_id, "Queued"));
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Build failed");
    }
  }, []);

  return (
    <Panel title="Artifacts">
      <form className="cloudosd-artifact-form" data-testid="cloudosd-build-form" onSubmit={(event) => { void submitBuild(event); }}>
        <input name="remote" aria-label="Remote host" defaultValue="Adam.Gell@10.211.55.6" />
        <input name="remote_root" aria-label="Remote root" defaultValue="F:\\BuildRoot" />
        <input name="architecture" aria-label="Architecture" defaultValue="amd64" readOnly />
        <input name="osdcloud_module_version" aria-label="OSDCloud module" defaultValue={moduleVersion} />
        <button type="submit">Build Artifact</button>
        {status ? <span>{status}</span> : null}
      </form>
      {payload.artifacts.length ? (
        <div className="table-wrap">
          <table className="jobs-table cloudosd-table" aria-label="OSDCloud artifacts">
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Architecture</th>
                <th scope="col">OSDCloud</th>
                <th scope="col">Build SHA</th>
                <th scope="col">ISO path</th>
                <th scope="col">WIM path</th>
                <th scope="col">Proxmox volid</th>
              </tr>
            </thead>
            <tbody>
              {payload.artifacts.map((artifact) => (
                <tr key={textValue(artifact.id)}>
                  <td><StatusBadge value={textValue(artifact.readiness)} /></td>
                  <td><code>{textValue(artifact.architecture)}</code></td>
                  <td><code>{textValue(artifact.osdcloud_module_version)}</code></td>
                  <td><code>{textValue(artifact.build_sha)}</code></td>
                  <td><code>{textValue(artifact.iso_path)}</code></td>
                  <td><code>{textValue(artifact.wim_path)}</code></td>
                  <td><code>{textValue(artifact.proxmox_volid, "not uploaded")}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="empty">No OSDCloud artifacts yet.</p>}
    </Panel>
  );
}

export function CloudosdPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<CloudosdPayload>(emptyPayload);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionMessage, setActionMessage] = useState("");

  const requestedView = useMemo(() => normalizeView(new URLSearchParams(window.location.search).get("view")), []);

  const load = useCallback(async () => {
    try {
      const search = window.location.search || (requestedView === "overview" ? "" : `?view=${requestedView}`);
      const nextPayload = normalizePayload(await fetchJson<unknown>(`/api/cloudosd/page${search}`));
      setPayload({ ...nextPayload, view: nextPayload.view });
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load OSDCloud");
    } finally {
      setLoading(false);
    }
  }, [requestedView]);

  usePolling(load);

  const postAction = useCallback(async (endpoint: string) => {
    setActionMessage("Working");
    try {
      const result = await postJson<{ readonly job_id?: string; readonly job_ids?: readonly string[] }>(endpoint);
      if (result.job_id) {
        setActionMessage(`Queued ${result.job_id}`);
      } else if (result.job_ids?.length) {
        setActionMessage(`Queued ${String(result.job_ids.length)} jobs`);
      } else {
        setActionMessage("Done");
      }
      await load();
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : "Action failed");
    }
  }, [load]);

  return (
    <PageFrame bootstrap={bootstrap} title="OSDCloud Desktop" section="Deploy" path="/react/cloudosd">
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading OSDCloud</span>
          <div className="load-strip__track" role="progressbar" aria-label="OSDCloud loading"><span /></div>
        </div>
      ) : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <div className="cloudosd-cockpit">
        <Subnav view={payload.view} />
        <OperatorFlow payload={payload} />
        {payload.view === "overview" ? <Overview payload={payload} actionMessage={actionMessage} onArchiveAction={(endpoint) => { void postAction(endpoint); }} /> : null}
        {payload.view === "builder" ? <Builder payload={payload} /> : null}
        {payload.view === "cache" ? <Cache payload={payload} actionMessage={actionMessage} onCacheAction={(endpoint) => { void postAction(endpoint); }} /> : null}
        {payload.view === "artifacts" ? <Artifacts payload={payload} /> : null}
      </div>
    </PageFrame>
  );
}
